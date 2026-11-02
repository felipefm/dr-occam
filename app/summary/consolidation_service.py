"""Resumo consolidado de várias pautas arquivadas (ex.: uma semana).

É o map-reduce que a POC de resumo por período não conseguiu fazer com
centenas de notícias (docs/poc-resumo-por-periodo.md), mas com a 1ª etapa
já feita e com curadoria: a entrada da IA são os resumos escolhidos de
cada pauta, agrupados por dia, mais os títulos originais das notícias
como referência contra erros herdados dos resumos diários. Pauta sem
resumo escolhido entra pelas próprias notícias (título + resumo do
pipeline). Ver docs/resumo-consolidado.md.

A cobertura, aqui, é por tema: cada parágrafo do resumo escolhido de uma
pauta (menos o "Também na pauta:") e cada notícia de pauta sem resumo
precisa aparecer no texto; o que faltar entra em "Também no período:".
"""

import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete
from sqlmodel import Session, col, select

from article_format import to_display_timezone
from database import engine
from models import Consolidation, PautaArchive, PautaSummary
from summary import coverage, format_repository
from summary import pauta_repository
from summary.engine import complete, exclusive
from summary.formats import clean_output, system_prompt

# Teto do texto de entrada (~30 mil tokens), abaixo do contexto de 51.200
# tokens do modelo local medido em 2026-11-01. Se passar, os títulos de
# referência saem primeiro; se ainda passar, o período é grande demais.
CONSOLIDATED_MAX_TOTAL_CHARS: int = int(os.getenv("CONSOLIDATED_MAX_TOTAL_CHARS", "120000"))
MIN_PAUTAS = 2


class ConsolidationNotFoundError(Exception):
    pass


class InvalidSelectionError(Exception):
    pass


class PeriodTooLargeError(Exception):
    pass


class FormatUnavailableError(Exception):
    pass


@dataclass
class PautaInPeriod:
    archive: PautaArchive
    time_label: str
    summary: PautaSummary | None  # versão escolhida, se houver


@dataclass
class Day:
    day: date
    label: str  # "31/10/2026"
    pautas: list[PautaInPeriod] = field(default_factory=list)

    @property
    def news_count(self) -> int:
        return sum(len(p.archive.items) for p in self.pautas)


# --- Consolidados: criar, listar, apagar ---


def create_or_get(session: Session, archive_ids: list[int]) -> Consolidation:
    """Consolidado das pautas escolhidas. A mesma seleção devolve o
    consolidado que já existe, com as versões já geradas, em vez de criar
    um duplicado."""
    ids = sorted(set(archive_ids))
    archives = session.exec(select(PautaArchive).where(col(PautaArchive.id).in_(ids))).all()
    if len(archives) != len(ids):
        raise InvalidSelectionError("Alguma das pautas selecionadas não existe mais.")
    if len(archives) < MIN_PAUTAS:
        raise InvalidSelectionError(f"Selecione pelo menos {MIN_PAUTAS} pautas arquivadas.")
    ordered = [a.id for a in sorted(archives, key=lambda a: a.archived_at)]
    for existing in session.exec(select(Consolidation)).all():
        if sorted(existing.archive_ids) == ids:
            return existing
    consolidation = Consolidation(archive_ids=ordered)
    session.add(consolidation)
    session.commit()
    session.refresh(consolidation)
    return consolidation


def get(session: Session, consolidation_id: int) -> Consolidation:
    consolidation = session.get(Consolidation, consolidation_id)
    if consolidation is None:
        raise ConsolidationNotFoundError
    return consolidation


def delete_consolidation(session: Session, consolidation_id: int) -> None:
    """Apaga o consolidado e as versões dele. As pautas arquivadas e os
    resumos delas não são tocados."""
    consolidation = get(session, consolidation_id)
    session.execute(delete(PautaSummary).where(PautaSummary.consolidation_id == consolidation_id))
    session.delete(consolidation)
    session.commit()


def days_of(session: Session, consolidation: Consolidation) -> list[Day]:
    """As pautas do consolidado agrupadas por dia (data de arquivamento no
    horário de Brasília), em ordem cronológica, cada uma com o resumo
    escolhido dela (se houver)."""
    archives = session.exec(
        select(PautaArchive).where(col(PautaArchive.id).in_(consolidation.archive_ids))
    ).all()
    chosen = pauta_repository.chosen_by_archive(session, [a.id for a in archives])
    days: "OrderedDict[date, Day]" = OrderedDict()
    for archive in sorted(archives, key=lambda a: a.archived_at):
        local = to_display_timezone(archive.archived_at)
        day = days.setdefault(local.date(), Day(local.date(), f"{local:%d/%m/%Y}"))
        day.pautas.append(PautaInPeriod(archive, f"{local:%H:%M}", chosen.get(archive.id)))
    return list(days.values())


def period_label(days: list[Day]) -> str:
    if not days:
        return ""
    first, last = days[0].day, days[-1].day
    if first == last:
        return f"{first:%d/%m/%Y}"
    if first.year == last.year:
        return f"{first:%d/%m} a {last:%d/%m/%Y}"
    return f"{first:%d/%m/%Y} a {last:%d/%m/%Y}"


def list_consolidations(session: Session) -> list[dict]:
    with_summary = pauta_repository.consolidation_ids_with_summary(session)
    result = []
    for c in session.exec(select(Consolidation).order_by(Consolidation.created_at.desc())).all():
        days = days_of(session, c)
        result.append(
            {
                "id": c.id,
                "label": period_label(days),
                "pautas": sum(len(d.pautas) for d in days),
                "days": len(days),
                "news": sum(d.news_count for d in days),
                "has_summary": c.id in with_summary,
            }
        )
    return result


# --- Entrada da IA e unidades de cobertura ---


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def build_prompt(days: list[Day], with_titles: bool = True) -> str:
    blocks = []
    for day in days:
        parts = [f"=== Dia {day.label} ==="]
        for p in day.pautas:
            if p.summary is not None:
                parts.append(f"Pauta arquivada às {p.time_label} — resumo escolhido:\n{p.summary.text}")
                if with_titles:
                    titles = "\n".join(f"- {item['title']}" for item in p.archive.items)
                    parts.append(f"Títulos originais das notícias desta pauta:\n{titles}")
            else:
                news = "\n".join(
                    f"- {item['title']}: {item.get('summary', '')[:400]}" for item in p.archive.items
                )
                parts.append(f"Pauta arquivada às {p.time_label} — sem resumo; notícias:\n{news}")
        blocks.append("\n\n".join(parts))
    return "\n\n\n".join(blocks)


def _theme_label(paragraph: str) -> str:
    head = paragraph.split(":", 1)[0]
    if ":" in paragraph and len(head) <= 40:
        return head.strip()
    return paragraph[:70].rstrip() + ("…" if len(paragraph) > 70 else "")


def coverage_units(days: list[Day]) -> tuple[list[coverage.CoverageItem], list[str]]:
    """Temas que o consolidado precisa citar e o nome de cada um na tela:
    cada parágrafo do resumo escolhido (menos o "Também na pauta:", que o
    próprio resumo diário já tratou como assunto menor) e, nas pautas sem
    resumo, cada notícia."""
    items, labels = [], []
    for day in days:
        short = day.label[:5]
        for p in day.pautas:
            if p.summary is not None:
                for paragraph in _paragraphs(p.summary.text):
                    if paragraph.startswith(coverage.ALSO_PREFIX):
                        continue
                    first_sentence = re.split(r"(?<=[.!?])\s+", paragraph, maxsplit=1)[0][:200]
                    items.append(coverage.CoverageItem(first_sentence, paragraph))
                    labels.append(f"{short} · {_theme_label(paragraph)}")
            else:
                for item in p.archive.items:
                    items.append(coverage.CoverageItem(item["title"], item.get("summary", "")))
                    labels.append(f"{short} · {item['title']}")
    return items, labels


# --- Geração ---


async def generate(consolidation_id: int, format_key: str) -> PautaSummary:
    with Session(engine) as session:
        consolidation = get(session, consolidation_id)
        fmt = format_repository.get_by_key(session, format_key)
        if fmt is None or not fmt.active or fmt.kind != "consolidado":
            raise FormatUnavailableError("Formato de resumo consolidado inexistente ou desativado.")
        label, prompt, ensure_full = fmt.label, fmt.prompt, fmt.ensure_full_coverage
        days = days_of(session, consolidation)
        # Lê tudo o que a geração usa antes de fechar a sessão (a chamada ao
        # LLM pode levar minutos).
        user_prompt = build_prompt(days)
        if len(user_prompt) > CONSOLIDATED_MAX_TOTAL_CHARS:
            user_prompt = build_prompt(days, with_titles=False)
        if len(user_prompt) > CONSOLIDATED_MAX_TOTAL_CHARS:
            raise PeriodTooLargeError(
                "O período é grande demais para a IA local de uma vez só. "
                "Selecione menos pautas (ou gere os resumos diários que faltam, "
                "que ocupam menos espaço que as notícias)."
            )
        units, unit_labels = coverage_units(days)

    log_label = (
        f"consolidado id={consolidation_id} formato={format_key} dias={len(days)} "
        f"pautas={sum(len(d.pautas) for d in days)}"
    )
    async with exclusive():
        result = await complete(system_prompt(prompt), user_prompt, log_label)
        text, coverage_record = clean_output(result.text), None
        if ensure_full and units:
            text, coverage_record = await coverage.ensure_coverage(
                text, units, unit_labels, "temas", coverage.ALSO_PREFIX_PERIOD, log_label
            )

    with Session(engine) as session:
        get(session, consolidation_id)  # pode ter sido apagado durante a geração
        return pauta_repository.add_summary(
            session,
            None,
            format_key,
            label,
            prompt,
            text,
            [],
            coverage_record,
            consolidation_id=consolidation_id,
        )


# --- Página e envio ---


def build_page(session: Session, consolidation_id: int) -> dict:
    consolidation = get(session, consolidation_id)
    days = days_of(session, consolidation)
    chosen = pauta_repository.get_chosen(session, None, consolidation_id)
    return {
        "consolidation_id": consolidation_id,
        "period": period_label(days),
        "days": [
            {
                "label": d.label,
                "news_count": d.news_count,
                "pautas": [
                    {
                        "id": p.archive.id,
                        "time": p.time_label,
                        "has_summary": p.summary is not None,
                        "news": p.archive.items,
                    }
                    for p in d.pautas
                ],
            }
            for d in days
        ],
        "pauta_count": sum(len(d.pautas) for d in days),
        "news_count": sum(d.news_count for d in days),
        "without_summary": sum(1 for d in days for p in d.pautas if p.summary is None),
        "summary_text": chosen.text if chosen else "",
    }
