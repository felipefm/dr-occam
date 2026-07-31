"""Acesso a dados dos embeddings de artigos.

O vetor em si mora numa tabela virtual do sqlite-vec (`vec_articles`), que o
SQLModel não sabe mapear como uma classe — por isso é manipulada aqui via SQL
cru, no mesmo estilo já usado em `database.py` para a migração de `article`.
A proveniência (qual modelo/dimensão gerou o vetor) mora numa tabela comum
(`ArticleEmbedding`, definida em models.py), lida/escrita normalmente via
SQLModel.

Nenhuma chamada a provedor de IA acontece neste módulo — só leitura/escrita.
"""

import logging
import sqlite3
from collections.abc import Sequence

import sqlite_vec
from sqlalchemy import Engine, event
from sqlmodel import Session, select

from embeddings.config import EMBEDDING_DIMENSIONS
from models import Article, ArticleEmbedding, ArticleStatus

logger = logging.getLogger(__name__)

VEC_TABLE_NAME = "vec_articles"


_VEC_EXTENSION_ATTACHED_FLAG = "_occam_vec_extension_attached"


def attach_vec_extension(engine: Engine) -> None:
    """Registra o carregamento da extensão sqlite-vec em toda nova conexão
    física do pool do SQLAlchemy. Precisa ser chamado antes de qualquer
    CREATE VIRTUAL TABLE ou consulta envolvendo `vec_articles` — inclusive
    antes de `ensure_vec_table`.

    Idempotente: chamar mais de uma vez para o mesmo engine não duplica o
    listener (relevante se esta função passar a ser chamada em cada ciclo
    do scheduler em vez de só uma vez no processo)."""
    if getattr(engine, _VEC_EXTENSION_ATTACHED_FLAG, False):
        return

    @event.listens_for(engine, "connect")
    def _load_vec_extension(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)
        dbapi_connection.enable_load_extension(False)

    setattr(engine, _VEC_EXTENSION_ATTACHED_FLAG, True)


def ensure_vec_table(engine: Engine) -> None:
    """Cria a tabela virtual de vetores se ainda não existir. Idempotente.

    A dimensão e a métrica de distância ficam fixas no schema da tabela:
    mudar EMBEDDING_DIMENSIONS depois de criada exige DROP + recriação
    (e reprocessar o backfill), já que o sqlite-vec não suporta ALTER aqui.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE_NAME} USING vec0("
            f"article_id INTEGER PRIMARY KEY, "
            f"embedding FLOAT[{EMBEDDING_DIMENSIONS}] distance_metric=cosine"
            f")"
        )


def get_article_ids_missing_embedding(session: Session, limit: int) -> list[int]:
    """IDs de artigos PROCESSED (únicos com ai_title/ai_summary) que ainda
    não têm linha em article_embedding — candidatos ao backfill."""
    already_embedded = select(ArticleEmbedding.article_id)
    statement = (
        select(Article.id)
        .where(Article.status == ArticleStatus.PROCESSED)
        .where(Article.id.not_in(already_embedded))
        .order_by(Article.created_at.asc())
        .limit(limit)
    )
    return [aid for aid in session.exec(statement).all() if aid is not None]


def save_embedding(
    session: Session, article_id: int, vector: Sequence[float], model: str
) -> None:
    """Grava o vetor na tabela virtual e a proveniência na tabela relacional
    na mesma transação da sessão recebida (o chamador decide quando commitar
    — ver embeddings/backfill.py)."""
    connection = session.connection()
    connection.exec_driver_sql(
        f"INSERT OR REPLACE INTO {VEC_TABLE_NAME} (article_id, embedding) VALUES (?, ?)",
        (article_id, sqlite_vec.serialize_float32(list(vector))),
    )

    existing = session.exec(
        select(ArticleEmbedding).where(ArticleEmbedding.article_id == article_id)
    ).first()
    if existing is None:
        existing = ArticleEmbedding(
            article_id=article_id, model=model, dimensions=len(vector)
        )
    else:
        existing.model = model
        existing.dimensions = len(vector)
    session.add(existing)
    session.commit()
