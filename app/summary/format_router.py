"""API dos formatos de resumo da pauta, usada pela seção "Formatos de
resumo" do /admin: listar, criar, editar (nome, prompt, ativo), reordenar,
restaurar o padrão de fábrica e apagar. Consultas em
summary.format_repository."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from database import engine
from models import SummaryFormat
from summary import format_repository as repository
from summary.formats import DEFAULT_FORMATS, FIXED_RULES
from summary.schemas import SummaryFormatItem, SummaryFormatList
from timezone_utils import as_aware_utc

router = APIRouter(prefix="/api/summary-formats", tags=["Resumo"])


class FormatCreatePayload(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=20000)


class FormatUpdatePayload(FormatCreatePayload):
    active: bool = True


class MovePayload(BaseModel):
    direction: Literal["up", "down"]


def to_item(fmt: SummaryFormat) -> SummaryFormatItem:
    default = DEFAULT_FORMATS.get(fmt.key)
    return SummaryFormatItem(
        id=fmt.id,
        key=fmt.key,
        label=fmt.label,
        prompt=fmt.prompt,
        position=fmt.position,
        active=fmt.active,
        builtin=default is not None,
        is_default=default is not None
        and default["label"] == fmt.label
        and default["prompt"].strip() == fmt.prompt.strip(),
        updated_at=as_aware_utc(fmt.updated_at),
    )


def _list(session: Session) -> SummaryFormatList:
    return SummaryFormatList(
        formats=[to_item(f) for f in repository.list_formats(session)],
        fixed_rules=FIXED_RULES,
    )


def _get_or_404(session: Session, format_id: int) -> SummaryFormat:
    fmt = repository.get_format(session, format_id)
    if fmt is None:
        raise HTTPException(status_code=404, detail="Formato não encontrado")
    return fmt


@router.get(
    "",
    response_model=SummaryFormatList,
    summary="Formatos de resumo da pauta",
    description="Todos os formatos (ativos e inativos), na ordem do seletor, e as "
    "regras fixas do sistema anexadas a todo prompt.",
)
def list_formats() -> SummaryFormatList:
    with Session(engine) as session:
        return _list(session)


@router.post("", status_code=201, response_model=SummaryFormatList, summary="Criar formato")
def create_format(payload: FormatCreatePayload) -> SummaryFormatList:
    with Session(engine) as session:
        repository.create_format(session, payload.label.strip(), payload.prompt.strip())
        return _list(session)


@router.put("/{format_id}", response_model=SummaryFormatList, summary="Editar formato")
def update_format(format_id: int, payload: FormatUpdatePayload) -> SummaryFormatList:
    with Session(engine) as session:
        fmt = _get_or_404(session, format_id)
        repository.update_format(
            session, fmt, payload.label.strip(), payload.prompt.strip(), payload.active
        )
        return _list(session)


@router.post(
    "/{format_id}/restore",
    response_model=SummaryFormatList,
    summary="Restaurar nome e prompt de fábrica",
    description="Só para formatos de fábrica (409 nos criados no /admin).",
)
def restore_format(format_id: int) -> SummaryFormatList:
    with Session(engine) as session:
        fmt = _get_or_404(session, format_id)
        if fmt.key not in DEFAULT_FORMATS:
            raise HTTPException(status_code=409, detail="Este formato não tem padrão de fábrica")
        repository.restore_default(session, fmt)
        return _list(session)


@router.post("/{format_id}/move", response_model=SummaryFormatList, summary="Mover no seletor")
def move_format(format_id: int, payload: MovePayload) -> SummaryFormatList:
    with Session(engine) as session:
        fmt = _get_or_404(session, format_id)
        repository.move(session, fmt, -1 if payload.direction == "up" else 1)
        return _list(session)


@router.delete(
    "/{format_id}",
    response_model=SummaryFormatList,
    summary="Apagar formato",
    description="Só formatos criados no /admin; os de fábrica podem ser desativados, "
    "não apagados (409). As versões de resumo já geradas guardam o nome e o prompt "
    "usados, então continuam legíveis.",
)
def delete_format(format_id: int) -> SummaryFormatList:
    with Session(engine) as session:
        fmt = _get_or_404(session, format_id)
        if fmt.key in DEFAULT_FORMATS:
            raise HTTPException(
                status_code=409,
                detail="Formatos de fábrica não podem ser apagados — desative-o.",
            )
        repository.delete_format(session, fmt)
        return _list(session)
