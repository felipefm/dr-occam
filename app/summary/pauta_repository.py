"""Consultas e escritas das versões de resumo de pauta (`pauta_summary`).

`archive_id=None` sempre significa "a pauta atual (aberta)". Mover as
versões da pauta atual para um arquivo, ou apagá-las ao descartar a pauta,
fica em home.repository, na mesma transação do arquivamento/descarte."""

from collections.abc import Sequence

from sqlmodel import Session, col, select

from models import Article, PautaSummary


def _of_pauta(archive_id: int | None):
    if archive_id is None:
        return col(PautaSummary.archive_id).is_(None)
    return PautaSummary.archive_id == archive_id


def list_summaries(session: Session, archive_id: int | None) -> list[PautaSummary]:
    """Versões da pauta, da mais antiga para a mais nova."""
    return list(
        session.exec(
            select(PautaSummary)
            .where(_of_pauta(archive_id))
            .order_by(PautaSummary.created_at.asc(), PautaSummary.id.asc())
        ).all()
    )


def get_chosen(session: Session, archive_id: int | None) -> PautaSummary | None:
    return session.exec(
        select(PautaSummary).where(_of_pauta(archive_id)).where(PautaSummary.chosen)
    ).first()


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
) -> PautaSummary:
    """Grava uma versão nova. A primeira versão de uma pauta já entra como
    escolhida; as seguintes só passam a valer quando o usuário escolhe."""
    summary = PautaSummary(
        archive_id=archive_id,
        mode=mode,
        mode_label=mode_label,
        prompt=prompt,
        text=text,
        article_ids=article_ids,
        chosen=get_chosen(session, archive_id) is None,
    )
    session.add(summary)
    session.commit()
    session.refresh(summary)
    return summary


def choose(session: Session, summary: PautaSummary) -> None:
    for other in list_summaries(session, summary.archive_id):
        other.chosen = other.id == summary.id
        session.add(other)
    session.commit()


def delete_summary(session: Session, summary: PautaSummary) -> None:
    """Apaga a versão. Se era a escolhida, a mais nova que sobrar assume."""
    archive_id, was_chosen = summary.archive_id, summary.chosen
    session.delete(summary)
    session.flush()
    if was_chosen:
        remaining = list_summaries(session, archive_id)
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
