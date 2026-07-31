"""Schemas Pydantic da busca híbrida: request/response da rota e o
resultado intermediário da extração de intenção de data."""

from datetime import date, datetime

from pydantic import BaseModel, Field


class DateIntent(BaseModel):
    """Resultado da extração de intenção de data feita pela IA a partir da
    busca em linguagem natural. `cleaned_query` é a busca sem a parte
    temporal (ex.: "TELEGRAM" em vez de "TELEGRAM nos últimos 30 dias"),
    usada para gerar o embedding sem ruído textual que não ajuda a
    similaridade semântica."""

    cleaned_query: str
    start_date: date | None = None
    end_date: date | None = None


class SearchResultItem(BaseModel):
    title: str
    summary: str
    url: str
    published_at: datetime
    relevance_percentage: float = Field(
        ge=0, le=100, description="Similaridade de cosseno com a busca, de 0 a 100%."
    )


class SearchResponse(BaseModel):
    query: str
    interpreted_start_date: date | None = Field(
        default=None, description="Data inicial do filtro que a IA extraiu da busca, se houver."
    )
    interpreted_end_date: date | None = Field(
        default=None, description="Data final do filtro que a IA extraiu da busca, se houver."
    )
    results: list[SearchResultItem]
    suggestions: list[str] = Field(
        default_factory=list,
        description="Sugestões de busca mais específicas, geradas pela IA a partir de artigos "
        "de baixa relevância encontrados. Só é preenchido quando `results` vem vazio.",
    )
