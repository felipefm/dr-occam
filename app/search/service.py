"""Orquestra a busca híbrida: extrai intenção de data da query em linguagem
natural (IA), gera o embedding da query "limpa" (IA), busca os artigos mais
similares dentro da janela de data resultante (sqlite-vec) e converte
distância cosseno em percentual de relevância.

Nenhuma chamada a IA ou consulta SQL é feita diretamente aqui — este módulo
só combina o que os outros módulos (intent_service, embeddings.service,
search.repository) já sabem fazer.
"""

import logging
from datetime import date, datetime, time, timezone

from sqlmodel import Session

from embeddings.config import EMBEDDING_QUERY_PREFIX
from embeddings.service import generate_embedding
from models import Article
from search.config import MIN_RELEVANCE_PERCENTAGE
from search.intent_service import extract_date_intent
from search.repository import find_similar_articles, get_articles_by_id, get_candidate_article_ids
from search.schemas import SearchResponse, SearchResultItem
from search.suggestion_service import suggest_query_refinements
from timezone_utils import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)


def _local_date_to_utc_naive(d: date, end_of_day: bool) -> datetime:
    """Converte uma data de calendário no fuso de exibição (America/Sao_Paulo)
    para o instante UTC correspondente (início ou fim do dia local), e
    remove o tzinfo antes de retornar.

    O tzinfo precisa ser removido porque `Article.created_at` é uma coluna
    `DateTime` sem timezone: o SQLite não guarda fuso, então os valores já
    salvos ficam naive (ver `_to_display_timezone` em main.py, que trata
    exatamente essa mesma característica na leitura). Comparar um datetime
    aware com uma coluna naive geraria uma comparação lexicográfica
    inconsistente (sufixo "+00:00" vs. nenhum sufixo).
    """
    local_moment = datetime.combine(
        d, time.max if end_of_day else time.min, tzinfo=DISPLAY_TIMEZONE
    )
    return local_moment.astimezone(timezone.utc).replace(tzinfo=None)


def _cosine_distance_to_relevance_percentage(distance: float) -> float:
    """Converte a distância cosseno (0 = idêntico, até 2 = oposto) num
    percentual de relevância de 0 a 100. Similaridade negativa (distância
    > 1, vetores quase opostos) é truncada em 0% — não faz sentido de
    produto exibir relevância negativa."""
    similarity = 1.0 - distance
    return round(max(0.0, min(1.0, similarity)) * 100, 2)


def _as_aware_utc(dt: datetime) -> datetime:
    """`Article.created_at` volta do SQLite naive (sem tzinfo), embora
    represente sempre um instante UTC (ver `_local_date_to_utc_naive` acima
    e `_to_display_timezone` em main.py, que trata a mesma característica).
    Sem isso, o JSON serializaria o datetime sem sufixo de fuso e o
    front-end interpretaria como hora local do navegador, exibindo a data
    errada."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _build_result_item(article: Article, distance: float) -> SearchResultItem:
    return SearchResultItem(
        title=article.ai_title or "(sem título)",
        summary=article.ai_summary or "",
        url=article.original_url,
        published_at=_as_aware_utc(article.created_at),
        relevance_percentage=_cosine_distance_to_relevance_percentage(distance),
    )


async def run_semantic_search(session: Session, raw_query: str, top_k: int) -> SearchResponse:
    intent = await extract_date_intent(raw_query)

    start_at = (
        _local_date_to_utc_naive(intent.start_date, end_of_day=False)
        if intent.start_date
        else None
    )
    end_at = (
        _local_date_to_utc_naive(intent.end_date, end_of_day=True)
        if intent.end_date
        else None
    )

    candidate_ids = get_candidate_article_ids(session, start_at, end_at)
    if not candidate_ids:
        suggestions = await suggest_query_refinements(
            raw_query, [], intent.start_date, intent.end_date
        )
        return SearchResponse(
            query=raw_query,
            interpreted_start_date=intent.start_date,
            interpreted_end_date=intent.end_date,
            results=[],
            suggestions=suggestions,
        )

    query_vector, _ = await generate_embedding(f"{EMBEDDING_QUERY_PREFIX}{intent.cleaned_query}")
    ranked = find_similar_articles(session, query_vector, candidate_ids, top_k)

    articles_by_id = get_articles_by_id(session, [article_id for article_id, _ in ranked])
    all_candidates = [
        _build_result_item(articles_by_id[article_id], distance)
        for article_id, distance in ranked
        if article_id in articles_by_id
    ]

    # Descarta candidatos abaixo do piso de relevância: buscas vagas (ex.: uma
    # única palavra genérica) produzem similaridade de cosseno artificialmente
    # parecida entre tudo, sem discriminar o que é de fato relevante — ver
    # search/config.py.
    results = [c for c in all_candidates if c.relevance_percentage >= MIN_RELEVANCE_PERCENTAGE]

    suggestions: list[str] = []
    if not results:
        weak_titles = [c.title for c in all_candidates]
        suggestions = await suggest_query_refinements(
            raw_query, weak_titles, intent.start_date, intent.end_date
        )

    return SearchResponse(
        query=raw_query,
        interpreted_start_date=intent.start_date,
        interpreted_end_date=intent.end_date,
        results=results,
        suggestions=suggestions,
    )
