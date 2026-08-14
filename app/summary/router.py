"""Rota HTTP do resumo de notícias por período. Nenhuma lógica de negócio
mora aqui — só validação de entrada e delegação para summary.service."""

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session

from database import engine
from summary.schemas import SummaryResponse
from summary.service import SummaryUnavailableError, build_period_summary

router = APIRouter(prefix="/api/summary", tags=["Resumo"])

_ALLOWED_DAYS = (1, 2, 3)


@router.get(
    "",
    response_model=SummaryResponse,
    summary="Resumo de notícias por período (POC)",
    description=(
        "Gera uma síntese em texto corrido dos artigos PROCESSED de uma "
        "janela curta e fixa (hoje / últimos 2 dias / últimos 3 dias), a "
        "partir dos títulos e resumos já gerados pelo pipeline — sem busca "
        "semântica, sem map-reduce, sem clustering por tópico (POC, ver "
        "docs/poc-resumo-por-periodo.md). Retorna também a lista de "
        "artigos (título + link) usados como fonte."
    ),
    response_description="O texto de síntese, a janela interpretada e as fontes usadas.",
)
async def summarize_period(
    days: int = Query(
        ...,
        description="Tamanho da janela em dias: 1 (hoje), 2 ou 3.",
    ),
) -> SummaryResponse:
    if days not in _ALLOWED_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"days deve ser um de {_ALLOWED_DAYS}",
        )
    with Session(engine) as session:
        try:
            return await build_period_summary(session, days)
        except SummaryUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
