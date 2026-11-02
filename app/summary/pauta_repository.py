"""Consultas e escritas das versões de resumo (`pauta_summary`), de uma
pauta ou de um resumo consolidado.

Escopo de uma versão: `consolidation_id` preenchido = consolidado;
senão `archive_id` preenchido = pauta arquivada; os dois NULL = a pauta
atual (aberta). Mover as
versões da pauta atual para um arquivo, ou apagá-las ao descartar a pauta,
fica em home.repository, na mesma transação do arquivamento/descarte."""

from collections.abc import Sequence

from sqlalchemy import and_
from sqlmodel import Session, col, select

from models import Article, PautaSummary


def _of_pauta(archive_id: int | None, consolidation_id: int | None = None):
    if consolidation_id is not None:
        return PautaSummary.consolidation_id == consolidation_id
    if archive_id is None:
        return and_(
            col(PautaSummary.archive_id).is_(None), col(PautaSummary.consolidation_id).is_(None)
        )
    return PautaSummary.archive_id == archive_id


def list_summaries(
    session: Session, archive_id: int | None, consolidation_id: int | None = None
) -> list[PautaSummary]:
    """Versões do escopo, da mais antiga para a mais nova."""
    return list(
        session.exec(
            select(PautaSummary)
            .where(_of_pauta(archive_id, consolidation_id))
            .order_by(PautaSummary.created_at.asc(), PautaSummary.id.asc())
        ).all()
    )


def get_chosen(
    session: Session, archive_id: int | None, consolidation_id: int | None = None
) -> PautaSummary | None:
    return session.exec(
        select(PautaSummary)
        .where(_of_pauta(archive_id, consolidation_id))
        .where(PautaSummary.chosen)
    ).first()


def chosen_by_archive(session: Session, archive_ids: Sequence[int]) -> dict[int, PautaSummary]:
    """Versão escolhida de cada pauta arquivada da lista (as sem resumo
    escolhido não aparecem no dicionário)."""
    if not archive_ids:
        return {}
    rows = session.exec(
        select(PautaSummary)
        .where(col(PautaSummary.archive_id).in_(list(archive_ids)))
        .where(PautaSummary.chosen)
    ).all()
    return {s.archive_id: s for s in rows}


def consolidation_ids_with_summary(session: Session) -> set[int]:
    """Consolidados que já têm um resumo escolhido."""
    return set(
        session.exec(
            select(PautaSummary.consolidation_id)
            .where(col(PautaSummary.consolidation_id).is_not(None))
            .where(PautaSummary.chosen)
        ).all()
    )


def archive_ids_with_summary(session: Session) -> set[int]:
    """Pautas arquivadas que já têm um resumo escolhido."""
    return set(
        session.exec(
            select(PautaSummary.archive_id)
            .where(col(PautaSummary.archive_id).is_not(None))
            .where(PautaSummary.chosen)
        ).all()
    )


def add_summary(
    session: Session,
    archive_id: int | None,
    mode: str,
    mode_label: str,
    prompt: str,
    text: str,
    article_ids: list[int],
    coverage: dict | None = None,
    consolidation_id: int | None = None,
) -> PautaSummary:
    """Grava uma versão nova. A primeira versão de uma pauta já entra como
    escolhida; as seguintes só passam a valer quando o usuário escolhe."""
    summary = PautaSummary(
        archive_id=archive_id,
        consolidation_id=consolidation_id,
        mode=mode,
        mode_label=mode_label,
        prompt=prompt,
        text=text,
        coverage=coverage,
        article_ids=article_ids,
        chosen=get_chosen(session, archive_id, consolidation_id) is None,
    )
    session.add(summary)
    session.commit()
    session.refresh(summary)
    return summary


def choose(session: Session, summary: PautaSummary) -> None:
    for other in list_summaries(session, summary.archive_id, summary.consolidation_id):
        other.chosen = other.id == summary.id
        session.add(other)
    session.commit()


def delete_summary(session: Session, summary: PautaSummary) -> None:
    """Apaga a versão. Se era a escolhida, a mais nova que sobrar assume."""
    archive_id, consolidation_id = summary.archive_id, summary.consolidation_id
    was_chosen = summary.chosen
    session.delete(summary)
    session.flush()
    if was_chosen:
        remaining = list_summaries(session, archive_id, consolidation_id)
        if remaining:
            remaining[-1].chosen = True
            session.add(remaining[-1])
    session.commit()


def get_articles_by_urls(session: Session, urls: Sequence[str]) -> dict[str, Article]:
    """Artigos ainda no banco para os links de uma pauta arquivada (os que
    a limpeza de artigos antigos já apagou simplesmente não voltam)."""
    if not urls:
        return {}
    articles = session.exec(select(Article).where(col(Article.original_url).in_(list(urls)))).all()
    return {a.original_url: a for a in articles}
