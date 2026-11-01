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

import logging
from dataclasses import dataclass

from sqlmodel import Session

from database import engine
from home import repository as home_repository
from home.service import REMOVED_SOURCE_NAME
from models import PautaSummary
from summary import format_repository
from summary import pauta_repository as repository
from summary.config import PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE, PAUTA_SUMMARY_MAX_TOTAL_CHARS
from summary import coverage
from summary.engine import complete, exclusive
from summary.formats import clean_output, system_prompt

logger = logging.getLogger(__name__)


class EmptyPautaError(Exception):
    pass


class PautaChangedError(Exception):
    """A pauta atual foi arquivada ou descartada enquanto o resumo era gerado."""


class ArchiveNotFoundError(Exception):
    pass


class FormatUnavailableError(Exception):
    """O formato pedido não existe ou está desativado."""


@dataclass
class _Entry:
    title: str
    source: str
    text: str  # texto original (limitado no prompt) — entrada da 1ª rodada
    summary: str  # resumo neutro do pipeline — conferência e 2ª rodada


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
            summary=a.ai_summary or "",
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
        entries.append(
            _Entry(
                title=item["title"],
                source=item["source"],
                text=text or "",
                summary=item.get("summary", ""),
            )
        )
    return entries


async def _complete_coverage(text: str, entries: list[_Entry], label: str) -> tuple[str, dict | None]:
    """Confere quais notícias o texto não citou e, se faltar alguma, pede à
    IA um trecho curto para cada uma (2ª rodada) e as acrescenta em
    "Também na pauta:". Devolve o texto final e o registro de cobertura
    (None se a conferência não pôde ser feita — o texto da 1ª rodada vale
    assim mesmo)."""
    items = [coverage.CoverageItem(e.title, e.summary or e.text[:600]) for e in entries]
    try:
        missing = await coverage.find_missing(text, items)
    except Exception:
        logger.exception("Resumo (%s): conferência de cobertura falhou; seguindo sem ela", label)
        return text, None

    record = {
        "total": len(entries),
        "completed": [{"n": i + 1, "title": entries[i].title} for i in missing],
    }
    if not missing:
        return text, record

    missing_items = [items[i] for i in missing]
    result = await complete(
        coverage.COMPLETION_PROMPT,
        coverage.completion_input(missing_items),
        f"{label} complemento={len(missing)}",
    )
    fragments = coverage.parse_completion(clean_output(result.text), missing_items)
    return coverage.append_also(text, fragments), record


async def generate(archive_id: int | None, format_key: str) -> PautaSummary:
    """Gera e grava uma versão nova do resumo da pauta atual
    (`archive_id=None`) ou de uma pauta arquivada, no formato `format_key`.
    Com "garantir todas as notícias" ligado no formato, confere a cobertura
    e completa as que faltarem numa 2ª rodada."""
    with Session(engine) as session:
        fmt = format_repository.get_by_key(session, format_key)
        if fmt is None or not fmt.active:
            raise FormatUnavailableError("Formato de resumo inexistente ou desativado.")
        label, prompt, ensure_full = fmt.label, fmt.prompt, fmt.ensure_full_coverage
        if archive_id is None:
            entries, article_ids = _current_entries(session)
        else:
            entries, article_ids = _archived_entries(session, archive_id), []
    if not entries:
        raise EmptyPautaError("A pauta está vazia — não há nada para resumir.")

    log_label = (
        f"pauta {'atual' if archive_id is None else f'arquivada id={archive_id}'} "
        f"formato={format_key} artigos={len(entries)}"
    )
    async with exclusive():
        result = await complete(system_prompt(prompt), _build_prompt(entries), log_label)
        text, coverage_record = clean_output(result.text), None
        if ensure_full:
            text, coverage_record = await _complete_coverage(text, entries, log_label)

    with Session(engine) as session:
        if archive_id is None and not home_repository.get_pauta_articles(session):
            raise PautaChangedError(
                "A pauta foi arquivada ou descartada enquanto o resumo era gerado."
            )
        return repository.add_summary(
            session, archive_id, format_key, label, prompt, text, article_ids, coverage_record
        )
