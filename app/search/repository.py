"""Acesso a dados da busca híbrida: filtro de data em `article` (SQL comum,
via SQLModel) e KNN por cosseno em `vec_articles` (sqlite-vec, SQL cru).

Nenhuma lógica de IA mora aqui — geração de embedding (embeddings/service.py)
e extração de intenção de data (search/intent_service.py) ficam em outros
módulos; este arquivo só lê o que já foi calculado.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlite_vec
from sqlmodel import Session, select

from embeddings.repository import VEC_TABLE_NAME
from models import Article, ArticleStatus

# Margem de segurança abaixo do limite padrão de host parameters do SQLite
# (SQLITE_MAX_VARIABLE_NUMBER, tipicamente 999 em builds mais antigos).
SQLITE_MAX_IN_PARAMS = 900


def get_candidate_article_ids(
    session: Session, start_at: datetime | None, end_at: datetime | None
) -> set[int]:
    """IDs de artigos PROCESSED (únicos elegíveis: são os únicos com
    embedding gerado pelo backfill) dentro da janela de data informada.
    `None` de um dos lados = sem limite naquele lado; ambos `None` = todos
    os PROCESSED valem (sem filtro de data)."""
    statement = select(Article.id).where(Article.status == ArticleStatus.PROCESSED)
    if start_at is not None:
        statement = statement.where(Article.created_at >= start_at)
    if end_at is not None:
        statement = statement.where(Article.created_at <= end_at)
    return {aid for aid in session.exec(statement).all() if aid is not None}


def find_similar_articles(
    session: Session,
    query_vector: Sequence[float],
    candidate_ids: set[int],
    top_k: int,
) -> list[tuple[int, float]]:
    """KNN por cosseno em `vec_articles`, restrito a `candidate_ids` (já
    filtrado por status/data pelo chamador). Retorna até `top_k` pares
    (article_id, distance) ordenados por distância crescente (mais
    similar primeiro; distância cosseno 0 = idêntico, até 2 = oposto)."""
    if not candidate_ids:
        return []

    connection = session.connection()
    packed_query = sqlite_vec.serialize_float32(list(query_vector))

    if len(candidate_ids) <= SQLITE_MAX_IN_PARAMS:
        # Caminho comum: restringe a busca vetorial só ao conjunto elegível,
        # evitando trazer candidatos fora da janela de data.
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = connection.exec_driver_sql(
            f"SELECT article_id, distance FROM {VEC_TABLE_NAME} "
            f"WHERE article_id IN ({placeholders}) AND embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (*candidate_ids, packed_query, top_k),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    # candidate_ids grande demais para um IN(...) com bind params seguro:
    # busca um k maior sem restrição de ID e filtra em Python. Só ocorre com
    # uma janela de data muito ampla (ou nenhuma) numa base já com centenas
    # de artigos processados.
    oversampled_k = min(len(candidate_ids), top_k * 20, 5000)
    rows = connection.exec_driver_sql(
        f"SELECT article_id, distance FROM {VEC_TABLE_NAME} "
        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (packed_query, oversampled_k),
    ).fetchall()
    return [(row[0], row[1]) for row in rows if row[0] in candidate_ids][:top_k]


def get_articles_by_id(session: Session, article_ids: Sequence[int]) -> dict[int, Article]:
    """Busca os artigos por ID, retornando um dict para o chamador montar
    a resposta na ordem de relevância (que não é a ordem do IN(...))."""
    if not article_ids:
        return {}
    statement = select(Article).where(Article.id.in_(article_ids))
    return {article.id: article for article in session.exec(statement).all()}
