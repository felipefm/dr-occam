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

Fila standby (repescagem de timeout):
    Um artigo que dá timeout em toda a cascata no lote principal não é
    marcado como falha na hora — é reservado numa lista em memória
    (`standby_queue`) e tentado de novo, uma única vez, depois que o lote
    principal terminar (reaproveitando o mesmo semáforo de concorrência).
    Isso existe porque, com LLM local e textos longos, um timeout muitas
    vezes é só a máquina ocupada processando o artigo anterior — vale uma
    segunda chance no fim da fila antes de gastar um dos `LLM_MAX_RETRIES`
    do artigo. Se a repescagem também der timeout, aí sim o artigo é
    marcado `ArticleStatus.DEAD` imediatamente (sem esperar os ciclos de
    `LLM_MAX_RETRIES`), pra não travar os próximos ciclos de ingestão.
    Erros que não são timeout (JSON malformado, autenticação etc.) nunca
    entram na standby_queue — seguem direto pelo fluxo de falha normal.
"""

import asyncio
import json
import logging
import os
import re

import litellm
from sqlmodel import Session, func, select

from database import engine
from llm_cascade import LLM_CASCADE
from models import Article, ArticleStatus, LLMProcessingLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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


async def _process_article(
    semaphore: asyncio.Semaphore, article_id: int, standby_queue: list[int] | None
) -> None:
    """Processa um artigo PENDING. `standby_queue`, quando não-None, é a
    lista de repescagem do lote principal: um timeout aqui só reserva o
    artigo nela (sem tocar o banco) em vez de marcar falha. `None` sinaliza
    que esta é a própria repescagem — um timeout aqui já vai direto para
    ArticleStatus.DEAD (ver docstring do módulo)."""
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
    force_dead = False  # timeout na repescagem pula a paciência de LLM_MAX_RETRIES
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
        except litellm.Timeout as exc:
            if standby_queue is not None:
                logger.warning(
                    "Artigo %s: timeout em toda a cascata (%s) — reservado para "
                    "repescagem no fim do lote em vez de marcar falha agora",
                    article_id, cascade_models,
                )
                standby_queue.append(article_id)
                return
            failed = True
            force_dead = True
            status_code = 408
            logger.error(
                "Artigo %s: timeout também na repescagem (%s) — marcando falha "
                "definitiva: %s",
                article_id, cascade_models, exc,
            )
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
                if force_dead or article.retry_count >= MAX_RETRIES
                else ArticleStatus.PENDING
            )

        session.add(article)
        session.commit()


async def _run_llm_batch(article_ids: list[int]) -> dict[str, int]:
    """Processa um único lote (já selecionado) de artigos PENDING. Retorna
    contagem por desfecho deste lote."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    standby_queue: list[int] = []
    await asyncio.gather(
        *(_process_article(semaphore, aid, standby_queue) for aid in article_ids)
    )

    if standby_queue:
        retry_ids = list(standby_queue)
        logger.info(
            "Repescagem: %d artigo(s) com timeout no lote principal — tentando de novo",
            len(retry_ids),
        )
        await asyncio.gather(
            *(_process_article(semaphore, aid, None) for aid in retry_ids)
        )

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

    return {
        "processed": processed,
        "dead": dead,
        "pending_retry": len(article_ids) - processed - dead,
    }


async def run_llm_processing() -> dict[str, int]:
    """Processa TODOS os artigos PENDING existentes, em lotes sucessivos de
    até BATCH_SIZE, até a fila esvaziar. Retorna a contagem agregada de todos
    os lotes desta execução.

    Circuito de segurança: se um lote inteiro não gerar nenhum PROCESSED nem
    DEAD (ex.: todos os modelos da cascata fora do ar), interrompe os lotes
    seguintes desta execução em vez de repetir os mesmos artigos em loop
    apertado — eles continuam PENDING e serão retomados no próximo gatilho.
    """
    logger.info("Iniciando ciclo de processamento LLM")
    if not LLM_CASCADE:
        raise RuntimeError(
            "Nenhum modelo de IA configurado. Defina ao menos LLM_MODEL_1 no .env."
        )

    total_result = {"processed": 0, "dead": 0, "pending_retry": 0}
    batches = 0

    while True:
        with Session(engine) as session:
            total_pending = session.exec(
                select(func.count())
                .select_from(Article)
                .where(Article.status == ArticleStatus.PENDING)
            ).one()

            statement = (
                select(Article.id)
                .where(Article.status == ArticleStatus.PENDING)
                .order_by(Article.created_at.asc())
                .limit(BATCH_SIZE)
            )
            article_ids = [aid for aid in session.exec(statement).all() if aid is not None]

        logger.info(
            "Total de artigos PENDING na fila: %d — processando lote de %d (máximo por lote=%d)",
            total_pending, len(article_ids), BATCH_SIZE,
        )
        if not article_ids:
            break

        batches += 1
        batch_result = await _run_llm_batch(article_ids)
        logger.info("Lote %d de processamento LLM concluído: %s", batches, batch_result)

        total_result["processed"] += batch_result["processed"]
        total_result["dead"] += batch_result["dead"]
        total_result["pending_retry"] += batch_result["pending_retry"]

        if batch_result["processed"] == 0 and batch_result["dead"] == 0:
            logger.warning(
                "Lote %d não avançou nenhum artigo (0 processados, 0 mortos) — "
                "interrompendo esta execução para não repetir em loop; "
                "os artigos restantes seguem PENDING para o próximo gatilho",
                batches,
            )
            break

    logger.info(
        "Ciclo de processamento LLM concluído (%d lote(s)): %s", batches, total_result
    )
    return total_result


if __name__ == "__main__":
    summary = asyncio.run(run_llm_processing())
    logger.info("Processamento LLM concluído: %s", summary)
