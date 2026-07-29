"""Dr. Occam — API FastAPI: gatilho do pipeline, feed RSS e leitura rápida."""

import collections
import logging
import re
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, AsyncIterator
from urllib.parse import quote
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete
from sqlalchemy import or_
from sqlmodel import Field, Session, SQLModel, select

import scheduler as scheduler_service
from database import create_db_and_tables, engine
from llm_processor import run_llm_processing
from models import Article, ArticleStatus, LLMProcessingLog, ScheduleConfig, Source, SourceType
from pipeline import run_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

LOG_BUFFER_SIZE = 500
_log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_SIZE)


class _BufferLogHandler(logging.Handler):
    """Mantém as últimas LOG_BUFFER_SIZE linhas de log em memória para o
    modal de logs do /admin — evita depender de `docker logs` no terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append(self.format(record))


_buffer_handler = _BufferLogHandler()
_buffer_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.getLogger().addHandler(_buffer_handler)

TRUNCATION_WARNING = (
    "[Aviso: O texto original era demasiadamente longo e foi truncado "
    "para proteção de contexto da IA]"
)
FEED_LIMIT = 100
HOMEPAGE_LIMIT = 50
DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    scheduler_service.start_scheduler()
    yield
    scheduler_service.shutdown_scheduler()


TAGS_METADATA = [
    {
        "name": "Pipeline",
        "description": "Disparo e acompanhamento do pipeline de ingestão + processamento por IA.",
    },
    {
        "name": "Feed",
        "description": "Conteúdo público consumido por leitores humanos (feed RSS e página inicial).",
    },
    {
        "name": "Admin",
        "description": "Painel de administração (`/admin`) para gerenciar as fontes de notícias.",
    },
    {
        "name": "Sources",
        "description": "CRUD das fontes RSS usadas pela ingestão. Usado pelo painel `/admin`, "
        "mas também acessível diretamente via API.",
    },
    {
        "name": "Manutenção",
        "description": "Limpeza de dados antigos (artigos e logs de processamento por IA) "
        "para conter o crescimento do banco de dados.",
    },
    {
        "name": "Agendamento",
        "description": "Configuração do agendador interno que dispara o pipeline "
        "automaticamente em horários definidos, sem depender do cron do SO.",
    },
]

app = FastAPI(
    title="Dr. Occam",
    description=(
        "API do Dr. Occam, agregador de notícias que coleta artigos de fontes RSS, "
        "resume e neutraliza o texto via IA, e publica o resultado como página web e feed RSS.\n\n"
        "Fluxo típico: `POST /trigger-pipeline` dispara a ingestão + processamento em background; "
        "os artigos processados aparecem em `GET /` e `GET /feed.xml`; as fontes usadas pela "
        "ingestão são gerenciadas pelo painel `GET /admin` e pelos endpoints `/api/sources`."
    ),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
)
templates = Jinja2Templates(directory="templates")


class SourceCreatePayload(SQLModel):
    name: str = Field(max_length=255, description="Nome de exibição da fonte, ex.: 'G1 Mundo'.")
    url: str = Field(
        max_length=2048,
        description="URL do feed RSS da fonte. Deve começar com http:// ou https://.",
    )


class SourceLimitPayload(SQLModel):
    max_daily_articles: int = Field(
        gt=0,
        description=(
            "Número máximo de artigos inéditos que a ingestão coleta desta fonte por "
            "execução do pipeline (não é um teto diário rígido: links já coletados antes "
            "nunca são recoletados, então rodar de novo mais tarde só traz o que for "
            "genuinamente novo no feed)."
        ),
    )


MAX_SCHEDULE_TIMES = 12
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleConfigPayload(SQLModel):
    enabled: bool = Field(
        description=(
            "Se True, o pipeline roda automaticamente nos horários listados em `times`. "
            "Se False, modo manual — o pipeline só é disparado via POST /trigger-pipeline "
            "(inclusive pelo botão 'Executar varredura' do /admin)."
        )
    )
    times: list[str] = Field(
        default_factory=list,
        description=(
            "Horários no formato 'HH:MM' (24h), interpretados no fuso America/Sao_Paulo. "
            "Um horário = execução única diária; vários horários = múltiplas execuções "
            "por dia. Ignorado quando enabled=False. Máximo de "
            f"{MAX_SCHEDULE_TIMES} horários."
        ),
    )


def _validate_schedule_times(times: list[str]) -> list[str]:
    if len(times) > MAX_SCHEDULE_TIMES:
        raise HTTPException(
            status_code=422,
            detail=f"Máximo de {MAX_SCHEDULE_TIMES} horários agendados por dia",
        )

    for t in times:
        if not _TIME_RE.match(t):
            raise HTTPException(
                status_code=422,
                detail=f"Horário inválido: '{t}' — use o formato HH:MM (24h)",
            )

    if len(set(times)) != len(times):
        raise HTTPException(status_code=422, detail="Horários duplicados não são permitidos")

    return sorted(times)


def _format_summary(article: Article) -> str:
    summary = article.ai_summary or ""
    if article.is_truncated:
        summary = f"{summary}\n\n{TRUNCATION_WARNING}"
    return summary


def _rfc822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def _to_display_timezone(dt: datetime) -> datetime:
    """Converte um datetime armazenado em UTC (naive ou aware) para exibição em -03:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TIMEZONE)


def _whatsapp_share_url(title: str, summary: str, url: str) -> str:
    text = f"{title}\n\n{summary}\n\n{url}"
    return f"https://wa.me/?text={quote(text)}"


def _telegram_share_url(title: str, summary: str, url: str) -> str:
    text = f"{title}\n\n{summary}"
    return f"https://t.me/share/url?url={quote(url)}&text={quote(text)}"


@app.post(
    "/trigger-pipeline",
    status_code=202,
    tags=["Pipeline"],
    summary="Disparar o pipeline de ingestão + IA",
    description=(
        "Inicia em background as duas etapas do pipeline, nesta ordem:\n\n"
        "1. **Ingestão**: busca novos itens em todas as fontes ativas (`Source.active=True`) "
        "e grava os artigos com status `PENDING`.\n"
        "2. **Processamento LLM**: envia os artigos `PENDING` para a IA, que gera título e "
        "resumo neutros, e marca cada artigo como `PROCESSED` (ou `DEAD`/`DEDUPLICATED`).\n\n"
        "A resposta retorna imediatamente (HTTP 202); o progresso pode ser acompanhado em "
        "`GET /api/logs` ou no modal de logs do `/admin`."
    ),
    response_description="Confirmação de que o pipeline foi agendado para execução em background.",
)
async def trigger_pipeline(background_tasks: BackgroundTasks) -> dict[str, str]:
    background_tasks.add_task(run_pipeline)
    return {"message": "Pipeline iniciado"}


async def _run_llm_reprocessing() -> None:
    logger.info("Reprocessamento de DEAD: gatilho recebido, iniciando em background")
    try:
        llm_summary = await run_llm_processing()
        logger.info("Reprocessamento de DEAD: processamento LLM concluído: %s", llm_summary)
    except Exception as e:
        logger.error("Reprocessamento de DEAD: falha durante a execução: %s", e)
        traceback.print_exc()


@app.post(
    "/pipeline/reprocess-dead",
    status_code=202,
    tags=["Pipeline"],
    summary="Ressuscitar artigos marcados como falha (DEAD) para reprocessamento",
    description=(
        "Busca até `limit` artigos com status `DEAD` (falha definitiva — todos os modelos "
        "da cascata de IA falharam, inclusive na repescagem da fila standby), volta o status "
        "deles para `PENDING` e zera `retry_count`, dando a cada um o mesmo orçamento de "
        "`LLM_MAX_RETRIES` tentativas de um artigo novo. Em seguida dispara em background o "
        "mesmo ciclo de processamento LLM usado pelo pipeline normal (`run_llm_processing`), "
        "reaproveitando a fila standby, o semáforo de concorrência e a lógica de timeout já "
        "existentes — nenhuma lógica nova de chamada à IA é criada aqui. A resposta retorna "
        "imediatamente (HTTP 202); o progresso pode ser acompanhado em `GET /api/logs`."
    ),
    response_description="Quantidade de artigos ressuscitados e enviados para reprocessamento.",
)
def reprocess_dead_articles(
    background_tasks: BackgroundTasks,
    limit: int = Query(
        default=20, gt=0, le=200,
        description="Máximo de artigos DEAD a ressuscitar nesta chamada.",
    ),
) -> dict[str, Any]:
    with Session(engine) as session:
        dead_articles = session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.DEAD)
            .order_by(Article.created_at.asc())
            .limit(limit)
        ).all()

        for article in dead_articles:
            article.status = ArticleStatus.PENDING
            article.retry_count = 0
            session.add(article)
        session.commit()

        resurrected = len(dead_articles)

    logger.info("Reprocessamento de DEAD: %d artigo(s) ressuscitado(s) para PENDING", resurrected)

    if resurrected:
        background_tasks.add_task(_run_llm_reprocessing)

    return {"message": "Reprocessamento iniciado", "articles_resurrected": resurrected}


@app.get(
    "/feed.xml",
    tags=["Feed"],
    summary="Feed RSS dos artigos processados",
    description=(
        f"Retorna um feed RSS 2.0 (`application/rss+xml`) com os {FEED_LIMIT} artigos mais "
        "recentes já processados pela IA (status `PROCESSED`), para consumo em leitores de "
        "feed. Cada item traz título e resumo neutros gerados pela IA e o link para a "
        "notícia original."
    ),
    response_description="Documento XML no formato RSS 2.0.",
)
def get_feed() -> Response:
    with Session(engine) as session:
        articles = session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.PROCESSED)
            .order_by(Article.created_at.desc())
            .limit(FEED_LIMIT)
        ).all()

        items = "".join(
            "<item>"
            f"<title>{escape(article.ai_title or '(sem título)')}</title>"
            f"<link>{escape(article.original_url)}</link>"
            f'<guid isPermaLink="false">{article.id}</guid>'
            f"<pubDate>{_rfc822(article.created_at)}</pubDate>"
            f"<description>{escape(_format_summary(article))}</description>"
            "</item>"
            for article in articles
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Dr. Occam</title>"
        "<link>/</link>"
        "<description>Agregador de notícias neutro</description>"
        "<language>pt-br</language>"
        f"<lastBuildDate>{_rfc822(datetime.now(timezone.utc))}</lastBuildDate>"
        f"{items}"
        "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")


@app.get(
    "/",
    response_class=HTMLResponse,
    tags=["Feed"],
    summary="Página inicial com o feed de notícias",
    description=(
        f"Renderiza a página web pública com os {HOMEPAGE_LIMIT} artigos mais recentes já "
        "processados pela IA (status `PROCESSED`), com título e resumo neutros, data de "
        "publicação convertida para o fuso de São Paulo, e links de compartilhamento para "
        "WhatsApp e Telegram."
    ),
    response_description="Página HTML do feed de notícias.",
)
def homepage(request: Request) -> Response:
    with Session(engine) as session:
        articles = session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.PROCESSED)
            .order_by(Article.created_at.desc())
            .limit(HOMEPAGE_LIMIT)
        ).all()

        cards: list[dict[str, Any]] = []
        for article in articles:
            title = article.ai_title or "(sem título)"
            summary = _format_summary(article)
            created_at = article.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            cards.append(
                {
                    "title": title,
                    "url": article.original_url,
                    "date": _to_display_timezone(article.created_at).strftime("%d/%m/%Y %H:%M"),
                    # ISO 8601 em UTC — consumido pelo JS do template para decidir,
                    # no navegador, quais cards chegaram depois da última visita
                    # (ver comentário de "occam-last-seen" em index.html).
                    "created_at_iso": created_at.isoformat(),
                    "summary": summary,
                    "whatsapp_url": _whatsapp_share_url(title, summary, article.original_url),
                    "telegram_url": _telegram_share_url(title, summary, article.original_url),
                }
            )

    return templates.TemplateResponse(
        request=request, name="index.html", context={"cards": cards}
    )


@app.get(
    "/admin",
    response_class=HTMLResponse,
    tags=["Admin"],
    summary="Painel de administração de fontes",
    description=(
        "Renderiza o painel HTML de administração: lista todas as fontes RSS cadastradas "
        "(ativas e inativas) com seus respectivos limites diários, e oferece na própria "
        "página os controles para cadastrar, ativar/desativar, excluir e ajustar o limite "
        "de cada fonte — que por baixo chamam os endpoints em `/api/sources`. Também "
        "oferece um botão para disparar o pipeline (`POST /trigger-pipeline`) e um modal "
        "para acompanhar os logs em tempo real (`GET /api/logs`)."
    ),
    response_description="Página HTML do painel de administração.",
)
def admin_page(request: Request) -> Response:
    with Session(engine) as session:
        sources = session.exec(select(Source).order_by(Source.id)).all()
    schedule = scheduler_service.load_schedule_from_db()
    return templates.TemplateResponse(
        request=request, name="admin.html", context={"sources": sources, "schedule": schedule}
    )


@app.get(
    "/api/logs",
    tags=["Admin"],
    summary="Últimas linhas de log da aplicação",
    description=(
        f"Retorna as últimas (até {LOG_BUFFER_SIZE}) linhas de log em memória da aplicação, "
        "mais recentes por último. Usado pelo modal de logs do `/admin` para acompanhar a "
        "execução do pipeline sem precisar de acesso ao terminal/`docker logs`."
    ),
    response_description="Lista de linhas de log, em ordem cronológica.",
)
def get_logs() -> dict[str, list[str]]:
    return {"logs": list(_log_buffer)}


@app.delete(
    "/api/articles",
    tags=["Manutenção"],
    summary="Apagar artigos antigos (qualquer status)",
    description=(
        "Remove permanentemente artigos com `created_at` mais antigo que "
        "`older_than_days`, **independente do status** (`PENDING`/`PROCESSED`/"
        "`DEAD`/`DEDUPLICATED`) — inclusive artigos já `PROCESSED` que "
        "somem do feed/painel de leitura junto. Remove na mesma chamada os "
        "registros de `llm_processing_log` associados a esses artigos, "
        "evitando deixá-los órfãos. Ação irreversível — não há confirmação "
        "adicional além do parâmetro `older_than_days`."
    ),
    response_description="Quantidade de artigos e de logs associados removidos.",
)
def delete_old_articles(
    older_than_days: int = Query(
        ..., gt=0,
        description="Remove artigos com created_at mais antigo que esta quantidade de dias.",
    ),
) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    with Session(engine) as session:
        article_ids = session.exec(
            select(Article.id).where(Article.created_at < cutoff)
        ).all()

        log_ids: list[int] = []
        if article_ids:
            log_ids = session.exec(
                select(LLMProcessingLog.id).where(
                    LLMProcessingLog.article_id.in_(article_ids)
                )
            ).all()
            if log_ids:
                session.exec(sa_delete(LLMProcessingLog).where(LLMProcessingLog.id.in_(log_ids)))
            session.exec(sa_delete(Article).where(Article.id.in_(article_ids)))
            session.commit()

    logger.info(
        "Manutenção: %d artigo(s) e %d log(s) associado(s) removido(s) "
        "(older_than_days=%d)",
        len(article_ids), len(log_ids), older_than_days,
    )
    return {"articles_deleted": len(article_ids), "logs_deleted": len(log_ids)}


@app.delete(
    "/api/llm-logs",
    tags=["Manutenção"],
    summary="Apagar registros antigos de llm_processing_log",
    description=(
        "Remove permanentemente registros de `llm_processing_log` mais "
        "antigos que `older_than_days`. Como a própria linha de log não "
        "guarda um timestamp próprio, a idade é calculada pela data de "
        "criação (`created_at`) do artigo associado. Também remove, na "
        "mesma chamada, qualquer log **órfão** (cujo artigo já não existe "
        "mais no banco). Não afeta os artigos em si, só o histórico de "
        "chamadas à IA. Ação irreversível."
    ),
    response_description="Quantidade de logs removidos.",
)
def delete_old_llm_logs(
    older_than_days: int = Query(
        ..., gt=0,
        description=(
            "Remove logs cujo artigo associado tem created_at mais antigo que "
            "esta quantidade de dias (além de qualquer log órfão)."
        ),
    ),
) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    with Session(engine) as session:
        log_ids = session.exec(
            select(LLMProcessingLog.id)
            .join(Article, LLMProcessingLog.article_id == Article.id, isouter=True)
            .where(or_(Article.id.is_(None), Article.created_at < cutoff))
        ).all()
        if log_ids:
            session.exec(sa_delete(LLMProcessingLog).where(LLMProcessingLog.id.in_(log_ids)))
            session.commit()

    logger.info(
        "Manutenção: %d log(s) de llm_processing_log removido(s) (older_than_days=%d)",
        len(log_ids), older_than_days,
    )
    return {"logs_deleted": len(log_ids)}


@app.post(
    "/api/sources",
    status_code=201,
    tags=["Sources"],
    summary="Cadastrar uma nova fonte RSS",
    description=(
        "Cria uma nova fonte de notícias do tipo RSS, ativa por padrão e com limite diário "
        "padrão de 50 artigos (ajustável depois via `PUT /api/sources/{source_id}/limit`). "
        "A fonte passa a ser incluída na próxima execução do pipeline de ingestão."
    ),
    response_description="A fonte recém-criada, incluindo o `id` gerado.",
)
def create_source(payload: SourceCreatePayload) -> Source:
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="URL deve começar com http:// ou https://")

    with Session(engine) as session:
        source = Source(name=payload.name, url=payload.url, source_type=SourceType.RSS)
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' criada (id=%s, url=%s)", source.name, source.id, source.url
        )
        return source


@app.patch(
    "/api/sources/{source_id}/toggle",
    tags=["Sources"],
    summary="Ativar/desativar uma fonte",
    description=(
        "Inverte o estado `active` da fonte. Fontes inativas são ignoradas pela próxima "
        "execução do pipeline de ingestão, mas seus artigos já coletados permanecem no feed."
    ),
    response_description="A fonte com o novo valor de `active`.",
)
def toggle_source(source_id: int) -> Source:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        source.active = not source.active
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' (id=%s) -> active=%s", source.name, source.id, source.active
        )
        return source


@app.delete(
    "/api/sources/{source_id}",
    status_code=204,
    tags=["Sources"],
    summary="Excluir uma fonte",
    description=(
        "Remove a fonte permanentemente. Os artigos já coletados dessa fonte **não** são "
        "excluídos: apenas perdem o vínculo (`Article.source_id` passa a `null`) e "
        "continuam aparecendo no feed e na página inicial."
    ),
    response_description="Sem conteúdo — exclusão confirmada.",
)
def delete_source(source_id: int) -> Response:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        logger.info("Admin: excluindo fonte '%s' (id=%s)", source.name, source.id)
        session.delete(source)
        session.commit()
    return Response(status_code=204)


@app.put(
    "/api/sources/{source_id}/limit",
    tags=["Sources"],
    summary="Ajustar o limite de artigos coletados por execução de uma fonte",
    description=(
        "Define o número máximo de artigos inéditos que a ingestão coleta dessa fonte por "
        "execução do pipeline (`Source.max_daily_articles`). Usado para conter fontes muito "
        "volumosas sem precisar desativá-las."
    ),
    response_description="A fonte com o novo valor de `max_daily_articles`.",
)
def update_source_limit(source_id: int, payload: SourceLimitPayload) -> Source:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        source.max_daily_articles = payload.max_daily_articles
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' (id=%s) -> max_daily_articles=%d",
            source.name, source.id, source.max_daily_articles,
        )
        return source


@app.get(
    "/api/schedule",
    tags=["Agendamento"],
    summary="Consultar a configuração do agendador automático",
    description=(
        "Retorna o estado atual do agendador interno: se está ligado (`enabled`) e, "
        "se sim, em quais horários (`times`, formato `HH:MM`, fuso America/Sao_Paulo) "
        "o pipeline é disparado automaticamente. Com `enabled=false`, o pipeline só "
        "roda via `POST /trigger-pipeline` (disparo manual)."
    ),
    response_description="Configuração atual do agendador.",
)
def get_schedule() -> ScheduleConfig:
    return scheduler_service.load_schedule_from_db()


@app.put(
    "/api/schedule",
    tags=["Agendamento"],
    summary="Atualizar a configuração do agendador automático",
    description=(
        "Substitui a configuração do agendador interno (persistida em banco, sobrevive "
        "a reinícios do container) e reconfigura os jobs em tempo real — não é preciso "
        "reiniciar a aplicação. Três modos possíveis, decididos pelos campos enviados:\n\n"
        "- **Desativado** (`enabled=false`): pipeline só roda via disparo manual.\n"
        "- **Execução única diária** (`enabled=true`, `times` com 1 horário): ex. "
        "`[\"06:00\"]`.\n"
        "- **Múltiplas execuções por dia** (`enabled=true`, `times` com vários horários): "
        "ex. `[\"08:00\", \"14:00\", \"20:00\"]`.\n\n"
        "Os horários usam o fuso America/Sao_Paulo e não podem se repetir "
        f"(máximo de {MAX_SCHEDULE_TIMES})."
    ),
    response_description="A configuração salva, já refletindo os jobs recém-aplicados.",
)
def update_schedule(payload: ScheduleConfigPayload) -> ScheduleConfig:
    times = _validate_schedule_times(payload.times)
    if payload.enabled and not times:
        raise HTTPException(
            status_code=422,
            detail="Informe ao menos um horário para ativar o agendamento automático",
        )

    with Session(engine) as session:
        config = session.exec(select(ScheduleConfig)).first()
        if config is None:
            config = ScheduleConfig()

        config.enabled = payload.enabled
        config.times = times
        config.updated_at = datetime.now(timezone.utc)
        session.add(config)
        session.commit()
        session.refresh(config)

    scheduler_service.apply_schedule(config)
    logger.info(
        "Agendador: configuração atualizada via /admin (enabled=%s, times=%s)",
        config.enabled, config.times,
    )
    return config
