"""Backfill de embeddings: gera o vetor de todo artigo PROCESSED que ainda
não tem embedding salvo, em lotes sucessivos, até a fila esvaziar.

Rodar manualmente (ainda não integrado ao pipeline automático — etapa
futura, depois que a arquitetura de busca estiver confirmada):

    python -m embeddings.backfill

Variáveis de ambiente: ver embeddings/config.py.
"""

import asyncio
import logging

from sqlmodel import Session

from database import engine
from embeddings.config import EMBEDDING_BATCH_SIZE, EMBEDDING_MAX_CONCURRENCY
from embeddings.repository import (
    attach_vec_extension,
    ensure_vec_table,
    get_article_ids_missing_embedding,
    save_embedding,
)
from embeddings.service import embed_article
from models import Article

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def _embed_one(semaphore: asyncio.Semaphore, article_id: int) -> bool:
    """Processa um único artigo. Retorna True em sucesso, False em falha
    (a falha fica só no log — o artigo continua elegível e será retentado
    na próxima execução do backfill, sem contador de tentativas por ora)."""
    async with semaphore:
        with Session(engine) as session:
            article = session.get(Article, article_id)
            if article is None:
                return False

            try:
                vector, model = await embed_article(article)
            except Exception:
                logger.exception("Artigo %s: falha ao gerar embedding", article_id)
                return False

            save_embedding(session, article_id, vector, model)
            logger.info(
                "Artigo %s: embedding salvo (modelo=%s, dimensões=%d)",
                article_id, model, len(vector),
            )
            return True


async def _run_embedding_batch(article_ids: list[int]) -> dict[str, int]:
    semaphore = asyncio.Semaphore(EMBEDDING_MAX_CONCURRENCY)
    results = await asyncio.gather(*(_embed_one(semaphore, aid) for aid in article_ids))
    embedded = sum(results)
    return {"embedded": embedded, "failed": len(results) - embedded}


async def run_embedding_backfill() -> dict[str, int]:
    """Processa TODOS os artigos PROCESSED sem embedding, em lotes de até
    EMBEDDING_BATCH_SIZE, até a fila esvaziar. Retorna a contagem agregada.

    Circuito de segurança: se um lote inteiro falhar (0 embeddings salvos),
    interrompe os lotes seguintes desta execução em vez de repetir os
    mesmos artigos em loop apertado — eles seguem elegíveis para a próxima
    execução do script.
    """
    attach_vec_extension(engine)
    ensure_vec_table(engine)

    total = {"embedded": 0, "failed": 0}
    batches = 0

    while True:
        with Session(engine) as session:
            article_ids = get_article_ids_missing_embedding(session, EMBEDDING_BATCH_SIZE)

        if not article_ids:
            break

        batches += 1
        logger.info(
            "Backfill de embeddings: lote %d — processando %d artigo(s)",
            batches, len(article_ids),
        )
        batch_result = await _run_embedding_batch(article_ids)
        total["embedded"] += batch_result["embedded"]
        total["failed"] += batch_result["failed"]

        if batch_result["embedded"] == 0:
            logger.warning(
                "Lote %d não avançou nenhum embedding — interrompendo esta "
                "execução para não repetir em loop; os artigos restantes "
                "seguem elegíveis para a próxima execução",
                batches,
            )
            break

    logger.info("Backfill de embeddings concluído (%d lote(s)): %s", batches, total)
    return total


if __name__ == "__main__":
    asyncio.run(run_embedding_backfill())
