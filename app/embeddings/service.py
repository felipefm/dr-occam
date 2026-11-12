"""Geração de embeddings via LiteLLM (OpenAI ou qualquer provedor compatível
configurado em embeddings/config.py). Nenhum acesso a banco mora aqui.
"""

import logging

import litellm

from embeddings.config import (
    EMBEDDING_API_BASE,
    EMBEDDING_API_KEY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_DOCUMENT_PREFIX,
    EMBEDDING_MODEL,
    EMBEDDING_TIMEOUT_SECONDS,
)
from llm_server import resolve_api_base
from models import Article

logger = logging.getLogger(__name__)

# Título + resumo neutros somados raramente passam disso; o corte é só uma
# proteção de custo/latência caso um resumo saia anormalmente longo.
MAX_EMBEDDING_INPUT_CHARS = 8000


def build_embedding_text(article: Article) -> str:
    """Texto usado para gerar o embedding: título + resumo neutros da IA
    (não o `original_content` bruto). É o texto canônico e já limpo que o
    artigo exibe no feed — evita embutir HTML residual ou boilerplate do
    scraping, e mantém o vetor alinhado com o que o usuário de fato lê.

    Retorna "" (sem prefixo) se o artigo não tem título/resumo — `embed_article`
    trata isso como erro do chamador; não faz sentido prefixar um texto vazio.
    """
    title = article.ai_title or ""
    summary = article.ai_summary or ""
    text = f"{title}\n\n{summary}".strip()
    if not text:
        return ""
    return f"{EMBEDDING_DOCUMENT_PREFIX}{text[:MAX_EMBEDDING_INPUT_CHARS]}"


def _provider_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {}
    # O endereço do servidor local pode ter sido trocado no /admin (llm_server.py).
    api_base = resolve_api_base(EMBEDDING_API_BASE)
    if api_base:
        kwargs["api_base"] = api_base
    if EMBEDDING_API_KEY:
        kwargs["api_key"] = EMBEDDING_API_KEY
    if "text-embedding-3" in EMBEDDING_MODEL:
        # Só os modelos v3 da OpenAI suportam reduzir a dimensão nativamente;
        # outros provedores/modelos ignoram esse kwarg ou devem ser
        # configurados com EMBEDDING_DIMENSIONS já igual à saída nativa deles.
        kwargs["dimensions"] = EMBEDDING_DIMENSIONS
    return kwargs


async def generate_embeddings(texts: list[str]) -> list[list[float]]:
    """Vários textos numa única chamada ao provedor, na mesma ordem. Usado
    pela conferência de cobertura do resumo da pauta (summary/coverage.py),
    que compara dezenas de frases curtas de uma vez."""
    response = await litellm.aembedding(
        model=EMBEDDING_MODEL, input=texts, timeout=EMBEDDING_TIMEOUT_SECONDS, **_provider_kwargs()
    )
    return [item["embedding"] for item in response.data]


async def generate_embedding(text: str) -> tuple[list[float], str]:
    """Chama o provedor de embeddings configurado para um texto qualquer.

    Função de baixo nível reaproveitada tanto para embedar artigos
    (`embed_article`, abaixo) quanto para embedar a query de busca
    (search/service.py) — mesmo modelo, mesma configuração, para que a
    query e os artigos caiam no mesmo espaço vetorial.
    """
    response = await litellm.aembedding(
        model=EMBEDDING_MODEL, input=[text], timeout=EMBEDDING_TIMEOUT_SECONDS, **_provider_kwargs()
    )
    vector: list[float] = response.data[0]["embedding"]
    return vector, EMBEDDING_MODEL


async def embed_article(article: Article) -> tuple[list[float], str]:
    """Gera o vetor de embedding do artigo. Retorna (vetor, nome do modelo).

    Levanta ValueError se o artigo ainda não tem ai_title/ai_summary (ou
    seja, não está PROCESSED) — chamar isso antes do processamento LLM é
    erro do chamador, não uma falha transitória a ser retentada.
    """
    text = build_embedding_text(article)
    if not text:
        raise ValueError(
            f"Artigo {article.id} não tem ai_title/ai_summary — gere o resumo "
            "via llm_processor antes de embedar"
        )
    return await generate_embedding(text)
