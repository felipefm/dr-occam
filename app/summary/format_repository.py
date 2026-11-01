"""Consultas e escritas dos formatos de resumo (`summary_format`)."""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlmodel import Session, func, select

from models import SummaryFormat
from summary.formats import DEFAULT_FORMATS, PREVIOUS_DEFAULT_PROMPTS

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_default_formats(session: Session) -> None:
    """Garante que os formatos de fábrica existem (instalação nova, ou um
    formato de fábrica novo numa atualização) e atualiza o prompt dos que
    ainda estão com um prompt de fábrica antigo, sem edição
    (PREVIOUS_DEFAULT_PROMPTS). O que foi editado no /admin fica como está."""
    existing = {f.key: f for f in session.exec(select(SummaryFormat)).all()}
    position = _next_position(session)
    for key, default in DEFAULT_FORMATS.items():
        fmt = existing.get(key)
        if fmt is not None:
            previous = {p.strip() for p in PREVIOUS_DEFAULT_PROMPTS.get(key, [])}
            if fmt.prompt.strip() in previous:
                logger.info("Formato '%s': prompt de fábrica atualizado (não havia edição)", key)
                fmt.prompt, fmt.updated_at = default["prompt"], _now()
                session.add(fmt)
            continue
        session.add(
            SummaryFormat(key=key, label=default["label"], prompt=default["prompt"], position=position)
        )
        position += 1
    session.commit()


def list_formats(session: Session, only_active: bool = False) -> list[SummaryFormat]:
    statement = select(SummaryFormat).order_by(SummaryFormat.position, SummaryFormat.id)
    if only_active:
        statement = statement.where(SummaryFormat.active)
    return list(session.exec(statement).all())


def get_format(session: Session, format_id: int) -> SummaryFormat | None:
    return session.get(SummaryFormat, format_id)


def get_by_key(session: Session, key: str) -> SummaryFormat | None:
    return session.exec(select(SummaryFormat).where(SummaryFormat.key == key)).first()


def _next_position(session: Session) -> int:
    current = session.exec(select(func.max(SummaryFormat.position))).one()
    return 0 if current is None else current + 1


def _slug(label: str) -> str:
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_label.lower()).strip("-")[:48] or "formato"


def create_format(
    session: Session, label: str, prompt: str, ensure_full_coverage: bool = True
) -> SummaryFormat:
    """Formato novo, no fim da lista. A chave sai do nome e não muda mais,
    mesmo que o formato seja renomeado depois."""
    base = _slug(label)
    key, n = base, 2
    while get_by_key(session, key) is not None:
        key, n = f"{base}-{n}", n + 1
    fmt = SummaryFormat(
        key=key,
        label=label,
        prompt=prompt,
        position=_next_position(session),
        ensure_full_coverage=ensure_full_coverage,
    )
    session.add(fmt)
    session.commit()
    session.refresh(fmt)
    return fmt


def update_format(
    session: Session,
    fmt: SummaryFormat,
    label: str,
    prompt: str,
    active: bool,
    ensure_full_coverage: bool,
) -> SummaryFormat:
    fmt.label, fmt.prompt, fmt.active, fmt.updated_at = label, prompt, active, _now()
    fmt.ensure_full_coverage = ensure_full_coverage
    session.add(fmt)
    session.commit()
    session.refresh(fmt)
    return fmt


def restore_default(session: Session, fmt: SummaryFormat) -> SummaryFormat:
    default = DEFAULT_FORMATS[fmt.key]
    return update_format(
        session, fmt, default["label"], default["prompt"], fmt.active, fmt.ensure_full_coverage
    )


def move(session: Session, fmt: SummaryFormat, direction: int) -> None:
    """Troca de lugar com o vizinho de cima (-1) ou de baixo (+1)."""
    formats = list_formats(session)
    i = next(i for i, f in enumerate(formats) if f.id == fmt.id)
    j = i + direction
    if not 0 <= j < len(formats):
        return
    formats[i], formats[j] = formats[j], formats[i]
    # Renumera tudo: posições antigas podem estar repetidas ou com buracos.
    for position, f in enumerate(formats):
        f.position = position
        session.add(f)
    session.commit()


def delete_format(session: Session, fmt: SummaryFormat) -> None:
    session.delete(fmt)
    session.commit()
