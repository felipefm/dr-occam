"""Esquema do banco de dados do Dr. Occam."""

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, Text, Uuid
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, Enum):
    RSS = "RSS"
    HTML_SCRAPE = "HTML_SCRAPE"


class ArticleStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    DEAD = "DEAD"
    DEDUPLICATED = "DEDUPLICATED"
    # Legado: soft delete automático por cota diária, removido (causava
    # artigos PENDING sendo arquivados antes do LLM processá-los). Mantido
    # no enum só para não quebrar a leitura de linhas antigas já gravadas
    # com esse status; nenhum código novo produz ARCHIVED.
    ARCHIVED = "ARCHIVED"


class Source(SQLModel, table=True):
    __tablename__ = "source"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=255, index=True)
    url: str = Field(nullable=False, max_length=2048)
    source_type: SourceType = Field(
        sa_column=Column(SAEnum(SourceType, name="source_type_enum"), nullable=False)
    )
    active: bool = Field(default=True, nullable=False)
    max_daily_articles: int = Field(default=50, nullable=False)

    articles: list["Article"] = Relationship(back_populates="source")


class Article(SQLModel, table=True):
    __tablename__ = "article"

    id: int | None = Field(default=None, primary_key=True)
    # Nullable: ao excluir uma fonte, os artigos já coletados dela devem
    # continuar no banco/feed (histórico preservado), só perdem o vínculo —
    # ver DELETE /api/sources/{id} em main.py e a migração em database.py.
    source_id: int | None = Field(default=None, foreign_key="source.id", index=True)
    original_url: str = Field(
        nullable=False, unique=True, index=True, max_length=2048
    )
    original_content: str = Field(sa_column=Column(Text, nullable=False))
    status: ArticleStatus = Field(
        default=ArticleStatus.PENDING,
        sa_column=Column(
            SAEnum(ArticleStatus, name="article_status_enum"), nullable=False
        ),
    )
    ai_title: str | None = Field(default=None, max_length=512)
    ai_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    cluster_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(Uuid, nullable=True, index=True)
    )
    retry_count: int = Field(default=0, nullable=False)
    is_truncated: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow, nullable=False, index=True
    )
    # Estado de leitura da tela inicial ("caixa de entrada"): NULL = não lida.
    # Fica no banco, e não no navegador, para celular e computador verem o
    # mesmo estado (ver home/).
    read_at: datetime | None = Field(default=None, nullable=True)
    # NULL = fora da pauta; o instante em que entrou ordena a lista da pauta.
    in_pauta_at: datetime | None = Field(default=None, nullable=True, index=True)

    source: Source | None = Relationship(back_populates="articles")
    processing_logs: list["LLMProcessingLog"] = Relationship(back_populates="article")


class PautaArchive(SQLModel, table=True):
    """Pauta arquivada: uma cópia (título, resumo, fonte, link) de cada
    notícia que estava na pauta no momento do arquivamento. É uma cópia, e
    não uma referência a `article`, para sobreviver à limpeza de artigos
    antigos (DELETE /api/articles)."""

    __tablename__ = "pauta_archive"

    id: int | None = Field(default=None, primary_key=True)
    archived_at: datetime = Field(default_factory=_utcnow, nullable=False, index=True)
    items: list[dict] = Field(sa_column=Column(JSON, nullable=False))


class PautaSummary(SQLModel, table=True):
    """Uma versão do resumo por IA de uma pauta. Uma pauta pode ter várias
    versões (gerar de novo, ou em outro modo) e uma delas é a escolhida —
    a que vai no envio e na impressão.

    `archive_id` NULL = versão da pauta atual (aberta). Ao arquivar a pauta,
    as versões dela passam a apontar para o arquivo, na mesma transação.
    `article_ids` guarda de quais artigos a versão foi feita, para a tela
    avisar quando a pauta atual mudou depois disso (vazio em versões geradas
    a partir de uma pauta já arquivada)."""

    __tablename__ = "pauta_summary"

    id: int | None = Field(default=None, primary_key=True)
    archive_id: int | None = Field(
        default=None, foreign_key="pauta_archive.id", nullable=True, index=True
    )
    # Versão de um resumo consolidado (várias pautas arquivadas) em vez de
    # uma pauta: `consolidation_id` preenchido e `archive_id` NULL. A pauta
    # atual é a que tem os dois NULL.
    consolidation_id: int | None = Field(
        default=None, foreign_key="consolidation.id", nullable=True, index=True
    )
    # `mode` é a chave do formato (summary_format.key). `mode_label` e
    # `prompt` são cópias do nome e do prompt editável no momento da geração:
    # o formato pode ser editado ou apagado depois, e a versão continua
    # dizendo de onde veio (NULL só em versões anteriores a essa cópia).
    mode: str = Field(nullable=False, max_length=64)
    mode_label: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    text: str = Field(sa_column=Column(Text, nullable=False))
    # Resultado da conferência de cobertura (summary/coverage.py):
    # {"total": N, "completed": [{"n": 3, "title": "..."}, ...]} — notícias
    # que a IA deixou de fora e o sistema completou em "Também na pauta:".
    # NULL = não conferida (formato sem "garantir todas", versão antiga ou
    # falha do provedor de embeddings).
    coverage: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    article_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    chosen: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)


class Consolidation(SQLModel, table=True):
    """Resumo consolidado de várias pautas arquivadas (ex.: uma semana). A
    entrada da IA são os resumos escolhidos de cada pauta (ou as notícias,
    quando a pauta não tem resumo), agrupados por dia — ver
    summary/consolidation_service.py. As versões ficam em `pauta_summary`
    com `consolidation_id`."""

    __tablename__ = "consolidation"

    id: int | None = Field(default=None, primary_key=True)
    # Ids de pauta_archive, em ordem cronológica de arquivamento.
    archive_ids: list[int] = Field(sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow, nullable=False, index=True)


class SummaryFormat(SQLModel, table=True):
    """Formato do resumo da pauta, editável no /admin: o nome que aparece no
    seletor e o prompt de sistema (a parte editável — as regras fixas do
    sistema são anexadas pelo código, ver summary/formats.py).

    `key` é estável (não muda ao renomear) e é o que as versões gravam em
    `pauta_summary.mode`. Os formatos de fábrica têm a mesma `key` de
    `summary.formats.DEFAULT_FORMATS`, o que habilita "Restaurar padrão"."""

    __tablename__ = "summary_format"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(nullable=False, unique=True, index=True, max_length=64)
    label: str = Field(nullable=False, max_length=120)
    prompt: str = Field(sa_column=Column(Text, nullable=False))
    position: int = Field(default=0, nullable=False)
    active: bool = Field(default=True, nullable=False)
    # Conferir se todas as notícias foram citadas e completar as que
    # faltarem numa segunda rodada (summary/coverage.py).
    ensure_full_coverage: bool = Field(default=True, nullable=False)
    # "pauta" (resumo de uma pauta) ou "consolidado" (resumo de várias
    # pautas arquivadas): cada quadro de resumo só oferece os do seu tipo.
    kind: str = Field(default="pauta", nullable=False, max_length=20)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class ScheduleConfig(SQLModel, table=True):
    """Configuração persistida do agendador interno do pipeline (ver
    scheduler.py). Linha única na tabela — sempre lida/gravada por inteiro,
    nunca por múltiplas linhas concorrentes."""

    __tablename__ = "schedule_config"

    id: int | None = Field(default=None, primary_key=True)
    enabled: bool = Field(default=False, nullable=False)
    # Horários no formato "HH:MM" (24h), interpretados no fuso
    # America/Sao_Paulo pelo agendador. Lista vazia + enabled=False = modo
    # manual (só dispara via POST /trigger-pipeline).
    times: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class LLMServerConfig(SQLModel, table=True):
    """Endereço e modelo de chat do servidor de IA local salvos no /admin
    (ver llm_server.py). Linha única; campo nulo (ou sem linha) = vale o do
    ambiente (LLM_API_BASE_1 / LLM_MODEL_1)."""

    __tablename__ = "llm_server_config"

    id: int | None = Field(default=None, primary_key=True)
    api_base: str | None = Field(default=None, max_length=500)
    # Id como o servidor lista em /v1/models, sem o prefixo "openai/".
    model: str | None = Field(default=None, max_length=200)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class ArticleEmbedding(SQLModel, table=True):
    """Proveniência do embedding de um artigo (qual modelo/dimensão gerou o
    vetor e quando). O vetor em si NÃO fica aqui: fica na tabela virtual
    `vec_articles` (extensão sqlite-vec), que o SQLModel não sabe mapear —
    ver embeddings/repository.py. Esta tabela é o índice relacional que diz
    quais artigos já têm vetor gerado."""

    __tablename__ = "article_embedding"

    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(
        foreign_key="article.id", nullable=False, unique=True, index=True
    )
    model: str = Field(nullable=False, max_length=100)
    dimensions: int = Field(nullable=False)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)


class LLMProcessingLog(SQLModel, table=True):
    __tablename__ = "llm_processing_log"

    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(foreign_key="article.id", nullable=False, index=True)
    llm_provider: str = Field(nullable=False, max_length=100)
    prompt_version: str = Field(nullable=False, max_length=50)
    prompt_tokens: int = Field(nullable=False)
    completion_tokens: int = Field(nullable=False)
    status_code: int = Field(nullable=False)

    article: Article = Relationship(back_populates="processing_logs")
