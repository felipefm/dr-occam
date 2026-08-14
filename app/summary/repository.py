"""Acesso a dados do resumo por período: reaproveita o filtro de data +
status PROCESSED já usado pela busca (`search.repository`), sem embedding/
KNN — o resumo pega (quase) todos os artigos da janela, não os mais
similares a uma query."""

from collections.abc import Sequence

from sqlmodel import Session, select

from models import Article


def get_articles_for_summary(
    session: Session, candidate_ids: Sequence[int], max_articles: int
) -> tuple[list[Article], bool]:
    """Artigos da janela em ordem cronológica (mais antigo primeiro, para o
    LLM ler na ordem dos fatos), até `max_articles`. O segundo valor indica
    se houve corte (mais candidatos do que o teto permite) — nesse caso,
    fica com os `max_articles` mais recentes dentro da janela."""
    if not candidate_ids:
        return [], False

    ids = list(candidate_ids)
    truncated = len(ids) > max_articles

    statement = select(Article).where(Article.id.in_(ids)).order_by(Article.created_at.desc())
    if truncated:
        statement = statement.limit(max_articles)
    articles = list(session.exec(statement).all())
    articles.reverse()  # de volta à ordem cronológica (mais antigo primeiro)
    return articles, truncated
