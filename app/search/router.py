"""Rota HTTP da busca híbrida. Nenhuma lógica de negócio mora aqui — só
validação de entrada (via FastAPI/Query) e delegação para search.service."""

from fastapi import APIRouter, Query
from sqlmodel import Session

from database import engine
from search.schemas import SearchResponse
from search.service import run_semantic_search

router = APIRouter(prefix="/api/search", tags=["Busca"])


@router.get(
    "",
    response_model=SearchResponse,
    summary="Busca híbrida (semântica + filtro de data em linguagem natural)",
    description=(
        "Recebe uma busca em linguagem natural (ex.: 'terremoto na china' ou "
        "'o que saiu sobre o Telegram nos últimos 30 dias') e retorna os artigos "
        "mais relevantes, em duas etapas:\n\n"
        "1. Uma IA extrai um possível filtro de data embutido na frase (datas "
        "relativas como 'últimos 30 dias' são resolvidas para datas absolutas) "
        "e o restante do texto (`cleaned_query`) é usado na etapa seguinte.\n"
        "2. `cleaned_query` é convertido em embedding e comparado por "
        "similaridade de cosseno com os artigos `PROCESSED` dentro da janela "
        "de data (se houver), usando o índice vetorial sqlite-vec.\n\n"
        "Retorna vazio (sem erro) se nenhum artigo elegível for encontrado — "
        "inclusive se a extração de data resultar numa janela sem nenhum "
        "artigo, ou se o backfill de embeddings ainda não tiver rodado."
    ),
    response_description="A busca interpretada (com o filtro de data, se algum "
    "foi identificado) e os artigos ordenados por relevância.",
)
async def search_articles(
    q: str = Query(
        ...,
        min_length=1,
        max_length=500,
        description="Busca em linguagem natural.",
    ),
    top_k: int = Query(
        default=10, gt=0, le=50, description="Máximo de resultados retornados."
    ),
) -> SearchResponse:
    with Session(engine) as session:
        return await run_semantic_search(session, q, top_k)
