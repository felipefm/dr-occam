"""Rotas do resumo por IA da pauta (atual e arquivadas): gerar versões,
listar, escolher a que vai no envio/impressão e apagar. Lógica em
summary.pauta_service; consultas em summary.pauta_repository."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

import pipeline_state
from database import engine
from home import repository as home_repository
from models import PautaSummary
from summary import engine as summary_engine
from summary import pauta_repository as repository
from summary import pauta_service as service
from summary.modes import DEFAULT_MODE, MODES
from summary.schemas import PautaSummaryCreated, PautaSummaryItem, PautaSummaryList
from timezone_utils import as_aware_utc

router = APIRouter(tags=["Resumo"])

Mode = Literal["briefing", "corrido"]


class GeneratePayload(BaseModel):
    mode: Mode = Field(default=DEFAULT_MODE, description="Formato do texto.")


def to_item(summary: PautaSummary) -> PautaSummaryItem:
    return PautaSummaryItem(
        id=summary.id,
        mode=summary.mode,
        mode_label=MODES.get(summary.mode, {}).get("label", summary.mode),
        text=summary.text,
        article_ids=summary.article_ids or [],
        chosen=summary.chosen,
        created_at=as_aware_utc(summary.created_at),
    )


def _list(archive_id: int | None) -> PautaSummaryList:
    with Session(engine) as session:
        if archive_id is not None and home_repository.get_pauta_archive(session, archive_id) is None:
            raise HTTPException(status_code=404, detail="Pauta arquivada não encontrada")
        summaries = repository.list_summaries(session, archive_id)
        return PautaSummaryList(
            summaries=[to_item(s) for s in summaries],
            busy=summary_engine.is_busy(),
        )


async def _generate(archive_id: int | None, mode: str) -> PautaSummaryCreated:
    try:
        summary = await service.generate(archive_id, mode)
    except service.ArchiveNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Pauta arquivada não encontrada") from exc
    except (service.EmptyPautaError, service.PautaChangedError, summary_engine.SummaryBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except summary_engine.SummaryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PautaSummaryCreated(summary=to_item(summary), pipeline_running=pipeline_state.is_running())


_GENERATE_DESCRIPTION = (
    "Gera uma versão nova do resumo por IA da pauta e a grava. A primeira versão "
    "já fica escolhida (é a que vai no envio e na impressão); as seguintes só "
    "quando escolhidas. Só um resumo é gerado por vez no servidor: 409 se já houver "
    "outro em andamento ou se a pauta estiver vazia; 503 se a IA falhar."
)


@router.get(
    "/api/pauta/summaries",
    response_model=PautaSummaryList,
    summary="Versões do resumo da pauta atual",
)
def list_current() -> PautaSummaryList:
    return _list(None)


@router.post(
    "/api/pauta/summaries",
    status_code=201,
    response_model=PautaSummaryCreated,
    summary="Gerar resumo da pauta atual",
    description=_GENERATE_DESCRIPTION,
)
async def generate_current(payload: GeneratePayload) -> PautaSummaryCreated:
    return await _generate(None, payload.mode)


@router.get(
    "/api/pautas/{archive_id}/summaries",
    response_model=PautaSummaryList,
    summary="Versões do resumo de uma pauta arquivada",
)
def list_archived(archive_id: int) -> PautaSummaryList:
    return _list(archive_id)


@router.post(
    "/api/pautas/{archive_id}/summaries",
    status_code=201,
    response_model=PautaSummaryCreated,
    summary="Gerar resumo de uma pauta arquivada",
    description=_GENERATE_DESCRIPTION,
)
async def generate_archived(archive_id: int, payload: GeneratePayload) -> PautaSummaryCreated:
    return await _generate(archive_id, payload.mode)


def _get_or_404(session: Session, summary_id: int) -> PautaSummary:
    summary = session.get(PautaSummary, summary_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Versão do resumo não encontrada")
    return summary


@router.post(
    "/api/pauta-summaries/{summary_id}/choose",
    response_model=PautaSummaryList,
    summary="Escolher a versão do resumo que vai no envio e na impressão",
    response_description="As versões da mesma pauta, já com a escolha aplicada.",
)
def choose(summary_id: int) -> PautaSummaryList:
    with Session(engine) as session:
        summary = _get_or_404(session, summary_id)
        archive_id = summary.archive_id
        repository.choose(session, summary)
    return _list(archive_id)


@router.delete(
    "/api/pauta-summaries/{summary_id}",
    response_model=PautaSummaryList,
    summary="Apagar uma versão do resumo",
    description="Se era a escolhida, a versão mais nova que sobrar passa a ser a escolhida.",
    response_description="As versões que sobraram da mesma pauta.",
)
def delete(summary_id: int) -> PautaSummaryList:
    with Session(engine) as session:
        summary = _get_or_404(session, summary_id)
        archive_id = summary.archive_id
        repository.delete_summary(session, summary)
    return _list(archive_id)
