"""Consultas e escritas da tela inicial: artigos da janela, estado de
leitura e pauta."""

from datetime import datetime

from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from models import Article, ArticleStatus, PautaArchive, PautaSummary, Source


def get_window_articles(session: Session, since: datetime) -> list[Article]:
    """Artigos PROCESSED criados a partir de `since` (UTC naive), mais
    recentes primeiro."""
    return list(
        session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.PROCESSED)
            .where(Article.created_at >= since)
            .order_by(Article.created_at.desc())
        ).all()
    )


def get_pauta_articles(session: Session) -> list[Article]:
    """Artigos na pauta, na ordem em que entraram — inclusive os que já
    saíram da janela da tela inicial."""
    return list(
        session.exec(
            select(Article)
            .where(Article.in_pauta_at.is_not(None))
            .order_by(Article.in_pauta_at.asc())
        ).all()
    )


def get_source_names(session: Session) -> dict[int, str]:
    return {s.id: s.name for s in session.exec(select(Source)).all()}


def get_active_source_names(session: Session) -> set[str]:
    return set(session.exec(select(Source.name).where(Source.active.is_(True))).all())


def count_processed_since(session: Session, since: datetime) -> int:
    return session.exec(
        select(func.count())
        .select_from(Article)
        .where(Article.status == ArticleStatus.PROCESSED)
        .where(Article.created_at >= since)
    ).one()


def set_read(session: Session, article_ids: list[int], read_at: datetime | None) -> int:
    """Marca (ou desmarca, com `read_at=None`) os artigos como lidos. Ao
    marcar, preserva o `read_at` de quem já estava lido."""
    stmt = update(Article).where(Article.id.in_(article_ids))
    if read_at is not None:
        stmt = stmt.where(Article.read_at.is_(None))
    result = session.execute(stmt.values(read_at=read_at))
    session.commit()
    return result.rowcount


def set_in_pauta(session: Session, article: Article, in_pauta_at: datetime | None) -> None:
    article.in_pauta_at = in_pauta_at
    session.add(article)
    session.commit()


def clear_pauta(session: Session) -> int:
    """Esvazia a pauta atual e descarta os resumos dela, que não servem
    para a próxima pauta."""
    result = session.execute(
        update(Article).where(Article.in_pauta_at.is_not(None)).values(in_pauta_at=None)
    )
    session.execute(delete(PautaSummary).where(PautaSummary.archive_id.is_(None)))
    session.commit()
    return result.rowcount


def archive_and_clear_pauta(session: Session, items: list[dict], archived_at: datetime) -> PautaArchive:
    """Grava a cópia da pauta e a esvazia na mesma transação — nunca fica
    uma pauta arquivada que continua aberta, nem uma esvaziada sem cópia.
    Os resumos da pauta atual vão junto para o arquivo."""
    archive = PautaArchive(archived_at=archived_at, items=items)
    session.add(archive)
    session.flush()
    session.execute(
        update(PautaSummary)
        .where(PautaSummary.archive_id.is_(None))
        .values(archive_id=archive.id)
    )
    session.execute(
        update(Article).where(Article.in_pauta_at.is_not(None)).values(in_pauta_at=None)
    )
    session.commit()
    session.refresh(archive)
    return archive


def list_pauta_archives(session: Session) -> list[PautaArchive]:
    return list(
        session.exec(select(PautaArchive).order_by(PautaArchive.archived_at.desc())).all()
    )


def get_pauta_archive(session: Session, archive_id: int) -> PautaArchive | None:
    return session.get(PautaArchive, archive_id)
