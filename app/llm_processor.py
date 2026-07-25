"""Processamento LLM: gera título e resumo neutros para artigos PENDING e
registra cada tentativa em `llm_processing_log`.

Nota de arquitetura: a deduplicação (preenchimento de `cluster_id`) é uma
etapa futura e separada, que deve rodar ANTES deste módulo (comparando
títulos/conteúdo bruto) para não gastar tokens de LLM com notícias que
serão descartadas por duplicidade. Este módulo nunca escreve `cluster_id`.

Cascata de IAs (Sistema de Prioridade/Fallback):
    O sistema lê uma sequência numerada de modelos a partir do ambiente:
        LLM_MODEL_1 / LLM_API_BASE_1 / LLM_API_KEY_1
        LLM_MODEL_2 / LLM_API_BASE_2 / LLM_API_KEY_2
        ... (LLM_MODEL_N até a numeração parar de existir)
    O primeiro (N=1) é o modelo primário e os demais viram a lista nativa
    `fallbacks` do litellm: se a chamada ao primário falhar (erro de
    conexão, timeout, rate limit, etc.), o litellm tenta o próximo da lista
    automaticamente, sem que isso conte como falha de processamento do
    artigo — só é tratado como falha se TODOS os modelos da cascata falharem.
    `LLM_API_BASE_N`/`LLM_API_KEY_N` são opcionais por modelo (ex.: um LM
    Studio na rede local usa API base customizada e uma chave qualquer;
    um provider padrão como Gemini normalmente só precisa da API key).

Variáveis de ambiente:
    LLM_MODEL_N, LLM_API_BASE_N, LLM_API_KEY_N: ver cascata acima.
    LLM_TIMEOUT_SECONDS: timeout por tentativa de modelo, em segundos
        (default 15) — importante para o modelo local nem sempre estar
        ligado: sem isso, uma máquina desligada travaria a chamada em vez
        de cair rápido para o próximo da cascata.
    MAX_CONTENT_LENGTH: tamanho máximo, em caracteres, do texto enviado ao
        LLM antes de truncar (default 10000).
    LLM_MAX_RETRIES: tentativas com falha antes de marcar o artigo como
        DEAD (default 5).
    LLM_BATCH_SIZE: quantidade máxima de artigos PENDING processados por
        execução (default 20).
    LLM_MAX_CONCURRENCY: chamadas concorrentes ao provider LLM (default 3).
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

import litellm
from sqlmodel import Session, func, select

from database import engine
from models import Article, ArticleStatus, LLMProcessingLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _load_llm_cascade() -> list[dict[str, Any]]:
    """Lê LLM_MODEL_N / LLM_API_BASE_N / LLM_API_KEY_N em sequência a
    partir de N=1 até a numeração parar de existir."""
    entries: list[dict[str, Any]] = []
    n = 1
    while True:
        model = os.getenv(f"LLM_MODEL_{n}")
        if not model:
            break
        entry: dict[str, Any] = {"model": model}
        api_base = os.getenv(f"LLM_API_BASE_{n}")
        api_key = os.getenv(f"LLM_API_KEY_{n}")
        if api_base:
            entry["api_base"] = api_base
        if api_key:
            entry["api_key"] = api_key
        entries.append(entry)
        n += 1
    return entries


LLM_CASCADE: list[dict[str, Any]] = _load_llm_cascade()
PROMPT_VERSION = "occam-neutral-v1"

LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "15"))
MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", "10000"))
MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "5"))
BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "20"))
MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "3"))

SYSTEM_PROMPT = (
    "Você é um editor de notícias neutro e imparcial do agregador Dr. Occam. "
    "Dado o texto de uma notícia, produza um título e um resumo estritamente "
    "factuais, sem adjetivos sensacionalistas, sem opinião e sem viés "
    "político ou ideológico. Responda SEMPRE em português do Brasil, "
    "traduzindo o conteúdo caso o texto original esteja em outro idioma. "
    "Responda APENAS com um JSON válido no formato "
    '{"title": "...", "summary": "..."}, sem markdown e sem texto adicional. '
    "NUNCA use aspas duplas dentro dos valores de title e summary. Se "
    "precisar destacar algo, use aspas simples. Não inclua blocos de "
    "markdown (como ```json) na resposta."
)

# Fallback para quando o JSON vem malformado (tipicamente aspas duplas não
# escapadas dentro de title/summary, apesar da instrução no prompt). Como o
# formato é fixo e conhecido (só as chaves "title" e "summary"), extrai os
# valores por regex tratando aspas internas como texto literal em vez de
# delimitador — não é um parser de JSON genérico, é só um resgate pontual
# para esse formato específico.
_JSON_FALLBACK_RE = re.compile(
    r'"title"\s*:\s*"(?P<title>.*?)"\s*,\s*"summary"\s*:\s*"(?P<summary>.*)"\s*\}',
    re.DOTALL,
)


def _truncate_content(content: str) -> tuple[str, bool]:
    if len(content) <= MAX_CONTENT_LENGTH:
        return content, False
    return content[:MAX_CONTENT_LENGTH], True


def _extract_json_block(raw: str) -> str:
    """Extrai estritamente o conteúdo entre a primeira '{' e a última '}',
    descartando blocos de markdown ou qualquer texto ao redor do JSON."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Nenhum objeto JSON encontrado na resposta da IA")
    return raw[start : end + 1]


def _parse_completion(raw: str) -> tuple[str, str]:
    block = _extract_json_block(raw)

    try:
        data = json.loads(block)
        title, summary = data["title"], data["summary"]
    except (json.JSONDecodeError, KeyError) as exc:
        match = _JSON_FALLBACK_RE.search(block)
        if not match:
            raise ValueError(f"JSON malformado e sem padrão reconhecível: {exc}") from exc
        logger.warning(
            "JSON malformado (provável aspas internas não escapadas) — "
            "recuperado via regex de fallback. Erro original: %s", exc,
        )
        title, summary = match.group("title"), match.group("summary")

    if not isinstance(title, str) or not isinstance(summary, str):
        raise ValueError("Campos 'title'/'summary' devem ser strings")
    if not title.strip() or not summary.strip():
        raise ValueError("Campos 'title'/'summary' vieram vazios")
    return title.strip(), summary.strip()


async def _process_article(semaphore: asyncio.Semaphore, article_id: int) -> None:
    with Session(engine) as session:
        article = session.get(Article, article_id)
        if article is None or article.status != ArticleStatus.PENDING:
            return
        content, truncated = _truncate_content(article.original_content)

    status_code = 200
    prompt_tokens = 0
    completion_tokens = 0
    title: str | None = None
    summary: str | None = None
    failed = False
    responding_model = LLM_CASCADE[0]["model"]

    primary, *fallbacks = LLM_CASCADE
    primary_kwargs = {k: v for k, v in primary.items() if k != "model"}
    cascade_models = [entry["model"] for entry in LLM_CASCADE]

    async with semaphore:
        logger.info(
            "Artigo %s: chamando IA — modelo primário='%s' (cascata completa: %s)",
            article_id, primary["model"], cascade_models,
        )
        raw: str | None = None
        try:
            response = await litellm.acompletion(
                model=primary["model"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=0.2,
                timeout=LLM_TIMEOUT_SECONDS,
                fallbacks=fallbacks or None,
                **primary_kwargs,
            )
            responding_model = getattr(response, "model", None) or responding_model
            if responding_model != primary["model"]:
                logger.warning(
                    "Artigo %s: fallback ativado — '%s' não respondeu a tempo "
                    "(timeout/erro de conexão); resposta veio de '%s'",
                    article_id, primary["model"], responding_model,
                )
            else:
                logger.info(
                    "Artigo %s: modelo primário '%s' respondeu com sucesso",
                    article_id, responding_model,
                )

            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            # A cascata (fallbacks) já tentou todos os modelos configurados
            # silenciosamente; chegar aqui significa que todos falharam.
            failed = True
            status_code = int(getattr(exc, "status_code", 500) or 500)
            logger.error(
                "Artigo %s: todos os modelos da cascata falharam (%s): %s",
                article_id, cascade_models, exc,
            )

        if raw is not None:
            try:
                title, summary = _parse_completion(raw)
                logger.info("Artigo %s: JSON retornado pela IA parseado com sucesso", article_id)
            except (ValueError, KeyError) as exc:
                failed = True
                logger.warning(
                    "Artigo %s: falha ao parsear JSON retornado pela IA: %s | texto bruto: %r",
                    article_id, exc, raw[:2000],
                )

    with Session(engine) as session:
        article = session.get(Article, article_id)
        if article is None:
            return

        session.add(
            LLMProcessingLog(
                article_id=article_id,
                llm_provider=responding_model,
                prompt_version=PROMPT_VERSION,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status_code=status_code,
            )
        )

        article.is_truncated = truncated
        if not failed and title and summary:
            article.ai_title = title
            article.ai_summary = summary
            article.status = ArticleStatus.PROCESSED
        else:
            article.retry_count += 1
            article.status = (
                ArticleStatus.DEAD
                if article.retry_count >= MAX_RETRIES
                else ArticleStatus.PENDING
            )

        session.add(article)
        session.commit()


async def run_llm_processing() -> dict[str, int]:
    """Processa um lote de artigos PENDING. Retorna contagem por desfecho."""
    logger.info("Iniciando ciclo de processamento LLM")
    if not LLM_CASCADE:
        raise RuntimeError(
            "Nenhum modelo de IA configurado. Defina ao menos LLM_MODEL_1 no .env."
        )

    with Session(engine) as session:
        statement = (
            select(Article.id)
            .where(Article.status == ArticleStatus.PENDING)
            .order_by(Article.created_at.asc())
            .limit(BATCH_SIZE)
        )
        article_ids = [aid for aid in session.exec(statement).all() if aid is not None]

    logger.info(
        "Encontrados %d artigos PENDING para processar (lote máximo=%d)",
        len(article_ids), BATCH_SIZE,
    )
    if not article_ids:
        logger.info("Nenhum artigo PENDING — encerrando ciclo de processamento LLM")
        return {"processed": 0, "dead": 0, "pending_retry": 0}

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    await asyncio.gather(*(_process_article(semaphore, aid) for aid in article_ids))

    with Session(engine) as session:
        processed = session.exec(
            select(func.count())
            .select_from(Article)
            .where(Article.id.in_(article_ids), Article.status == ArticleStatus.PROCESSED)
        ).one()
        dead = session.exec(
            select(func.count())
            .select_from(Article)
            .where(Article.id.in_(article_ids), Article.status == ArticleStatus.DEAD)
        ).one()

    result = {
        "processed": processed,
        "dead": dead,
        "pending_retry": len(article_ids) - processed - dead,
    }
    logger.info("Ciclo de processamento LLM concluído: %s", result)
    return result


if __name__ == "__main__":
    summary = asyncio.run(run_llm_processing())
    logger.info("Processamento LLM concluído: %s", summary)
