"""Rotas do resumo consolidado de várias pautas arquivadas: criar o
consolidado a partir da seleção em /pautas, a página dele, gerar versões e
apagar. Escolher/apagar versões usa as rotas comuns de
summary.pauta_router (/api/pauta-summaries/...). Lógica em
summary.consolidation_service."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session

import pipeline_state
from database import engine
from home.service import format_options
from summary import consolidation_service as service
from summary import engine as summary_engine
from summary.pauta_router import GeneratePayload, list_scope, to_item
from summary.schemas import PautaSummaryCreated, PautaSummaryList

router = APIRouter(tags=["Resumo"])
templates = Jinja2Templates(directory="templates")


class ConsolidationPayload(BaseModel):
    archive_ids: list[int] = Field(..., min_length=1, max_length=500)


def _get_or_404(session: Session, consolidation_id: int) -> None:
    try:
        service.get(session, consolidation_id)
    except service.ConsolidationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consolidado não encontrado") from exc


@router.post(
    "/api/consolidations",
    status_code=201,
    summary="Criar consolidado a partir de pautas arquivadas",
    description="Agrupa as pautas arquivadas escolhidas (pelo menos 2). A mesma seleção "
    "devolve o consolidado que já existe. Responde 422 se alguma pauta não existir.",
    response_description="O id do consolidado e o endereço da página dele.",
)
def create_consolidation(payload: ConsolidationPayload) -> dict:
    with Session(engine) as session:
        try:
            consolidation = service.create_or_get(session, payload.archive_ids)
        except service.InvalidSelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": consolidation.id, "url": f"/consolidados/{consolidation.id}"}


@router.delete(
    "/api/consolidations/{consolidation_id}",
    summary="Apagar consolidado",
    description="Apaga o consolidado e as versões de resumo dele. As pautas arquivadas "
    "e os resumos delas não são tocados.",
)
def delete_consolidation(consolidation_id: int) -> dict:
    with Session(engine) as session:
        _get_or_404(session, consolidation_id)
        service.delete_consolidation(session, consolidation_id)
    return {"deleted": consolidation_id}


@router.get(
    "/api/consolidations/{consolidation_id}/summaries",
    response_model=PautaSummaryList,
    summary="Versões do resumo de um consolidado",
)
def list_summaries(consolidation_id: int) -> PautaSummaryList:
    with Session(engine) as session:
        _get_or_404(session, consolidation_id)
    return list_scope(None, consolidation_id)


@router.post(
    "/api/consolidations/{consolidation_id}/summaries",
    status_code=201,
    response_model=PautaSummaryCreated,
    summary="Gerar resumo consolidado",
    description="Gera uma versão nova a partir dos resumos escolhidos das pautas (ou das "
    "notícias, nas pautas sem resumo), agrupados por dia. 409 se já houver outro resumo "
    "em andamento ou se o período for grande demais; 422 se o formato não for do tipo "
    "consolidado; 503 se a IA falhar.",
)
async def generate(consolidation_id: int, payload: GeneratePayload) -> PautaSummaryCreated:
    try:
        summary = await service.generate(consolidation_id, payload.mode)
    except service.ConsolidationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Consolidado não encontrado") from exc
    except service.FormatUnavailableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (service.PeriodTooLargeError, summary_engine.SummaryBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except summary_engine.SummaryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PautaSummaryCreated(summary=to_item(summary, {}), pipeline_running=pipeline_state.is_running())


@router.get(
    "/consolidados/{consolidation_id}",
    response_class=HTMLResponse,
    summary="Página do resumo consolidado",
    description="Quadro de versões do resumo, envio, impressão e, como apêndice, todas as "
    "notícias de cada dia com os links.",
)
def consolidation_page(request: Request, consolidation_id: int) -> Response:
    with Session(engine) as session:
        try:
            context = service.build_page(session, consolidation_id)
        except service.ConsolidationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Consolidado não encontrado") from exc
        context["summary_modes"] = format_options(session, "consolidado")
    return templates.TemplateResponse(request=request, name="consolidado.html", context=context)
