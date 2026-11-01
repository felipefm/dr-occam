"""Resumo por IA de uma pauta (atual ou arquivada).

Usa o motor de síntese (summary/engine.py). A pauta é curta e escolhida a
dedo, então cada notícia entra com o texto original (limitado), e não só
com o resumo do pipeline. Para
uma pauta arquivada, o texto original vem do artigo se ele ainda estiver
no banco; se a limpeza de artigos antigos já o apagou, entra o resumo
copiado no arquivo.

A sessão do banco não fica aberta durante a chamada ao LLM (que pode
levar minutos): lê a pauta, fecha, gera, e abre outra sessão para gravar.
"""

from dataclasses import dataclass

from sqlmodel import Session

from database import engine
from home import repository as home_repository
from home.service import REMOVED_SOURCE_NAME
from models import PautaSummary
from summary import pauta_repository as repository
from summary.config import PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE, PAUTA_SUMMARY_MAX_TOTAL_CHARS
from summary.engine import synthesize
from summary.modes import MODES


class EmptyPautaError(Exception):
    pass


class PautaChangedError(Exception):
    """A pauta atual foi arquivada ou descartada enquanto o resumo era gerado."""


class ArchiveNotFoundError(Exception):
    pass


@dataclass
class _Entry:
    title: str
    source: str
    text: str


def _build_prompt(entries: list[_Entry]) -> str:
    # Com uma pauta grande, o limite por notícia encolhe para o total caber.
    per_article = min(
        PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE, PAUTA_SUMMARY_MAX_TOTAL_CHARS // len(entries)
    )
    blocks = []
    for i, entry in enumerate(entries, start=1):
        text = entry.text.strip()
        if len(text) > per_article:
            text = text[:per_article].rstrip() + " […]"
        blocks.append(
            f"Notícia {i}\nTítulo: {entry.title}\nFonte: {entry.source}\nTexto: {text}"
        )
    return "\n\n".join(blocks)


def _current_entries(session: Session) -> tuple[list[_Entry], list[int]]:
    articles = home_repository.get_pauta_articles(session)
    source_names = home_repository.get_source_names(session)
    entries = [
        _Entry(
            title=a.ai_title or "(sem título)",
            source=source_names.get(a.source_id, REMOVED_SOURCE_NAME),
            text=a.original_content or a.ai_summary or "",
        )
        for a in articles
    ]
    return entries, [a.id for a in articles]


def _archived_entries(session: Session, archive_id: int) -> list[_Entry]:
    archive = home_repository.get_pauta_archive(session, archive_id)
    if archive is None:
        raise ArchiveNotFoundError
    by_url = repository.get_articles_by_urls(session, [item["url"] for item in archive.items])
    entries = []
    for item in archive.items:
        article = by_url.get(item["url"])
        text = article.original_content if article else item.get("summary", "")
        entries.append(_Entry(title=item["title"], source=item["source"], text=text or ""))
    return entries


async def generate(archive_id: int | None, mode: str) -> PautaSummary:
    """Gera e grava uma versão nova do resumo da pauta atual
    (`archive_id=None`) ou de uma pauta arquivada."""
    with Session(engine) as session:
        if archive_id is None:
            entries, article_ids = _current_entries(session)
        else:
            entries, article_ids = _archived_entries(session, archive_id), []
    if not entries:
        raise EmptyPautaError("A pauta está vazia — não há nada para resumir.")

    result = await synthesize(
        MODES[mode]["prompt"],
        _build_prompt(entries),
        f"pauta {'atual' if archive_id is None else f'arquivada id={archive_id}'} "
        f"modo={mode} artigos={len(entries)}",
    )

    with Session(engine) as session:
        if archive_id is None and not home_repository.get_pauta_articles(session):
            raise PautaChangedError(
                "A pauta foi arquivada ou descartada enquanto o resumo era gerado."
            )
        return repository.add_summary(session, archive_id, mode, result.text, article_ids)
