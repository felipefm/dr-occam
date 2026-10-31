"""Monta os dados da tela inicial: artigos da janela agrupados por
varredura, lista de fontes e pauta. Sem HTTP aqui — só leitura e
transformação, para o router renderizar."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session

from article_format import format_summary, to_display_timezone
from home import repository
from home.config import HOME_WINDOW_HOURS, SCAN_GAP_MINUTES
from models import Article

REMOVED_SOURCE_NAME = "Fonte removida"


def window_start(now: datetime) -> datetime:
    """Início da janela da tela inicial, em UTC naive (mesmo formato de
    `Article.created_at` no SQLite — ver timezone_utils.py)."""
    return (now - timedelta(hours=HOME_WINDOW_HOURS)).astimezone(timezone.utc).replace(tzinfo=None)


def group_by_scan(articles: list[Article], gap: timedelta) -> list[list[Article]]:
    """Separa artigos (mais recentes primeiro) em varreduras: um intervalo
    maior que `gap` entre dois artigos consecutivos começa um grupo novo."""
    groups: list[list[Article]] = []
    previous: datetime | None = None
    for article in articles:
        if previous is None or previous - article.created_at > gap:
            groups.append([])
        groups[-1].append(article)
        previous = article.created_at
    return groups


def _scan_label(started_at: datetime, now: datetime) -> str:
    local = to_display_timezone(started_at)
    today = to_display_timezone(now).date()
    if local.date() == today:
        return f"Varredura das {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"Varredura de ontem, {local:%H:%M}"
    return f"Varredura de {local:%d/%m}, {local:%H:%M}"


def _article_view(article: Article, source_names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": article.id,
        "title": article.ai_title or "(sem título)",
        "summary": format_summary(article),
        "url": article.original_url,
        "source": source_names.get(article.source_id, REMOVED_SOURCE_NAME),
        "time": f"{to_display_timezone(article.created_at):%H:%M}",
        "is_read": article.read_at is not None,
        "in_pauta": article.in_pauta_at is not None,
    }


def build_home(session: Session, now: datetime) -> dict[str, Any]:
    since = window_start(now)
    articles = repository.get_window_articles(session, since)
    source_names = repository.get_source_names(session)

    groups = [
        {
            # A lista vem do mais recente para o mais antigo: o último item
            # do grupo é o primeiro artigo gravado naquela varredura.
            "label": _scan_label(scan[-1].created_at, now),
            "items": [_article_view(a, source_names) for a in scan],
        }
        for scan in group_by_scan(articles, timedelta(minutes=SCAN_GAP_MINUTES))
    ]

    # Fontes com mais artigos na janela primeiro — são as que mais
    # "empurram" as outras para baixo, então ficam à mão no filtro.
    source_counts = Counter(item["source"] for g in groups for item in g["items"])
    sources = [name for name, _ in source_counts.most_common()]

    pauta = [
        {
            "id": a.id,
            "title": a.ai_title or "(sem título)",
            "summary": a.ai_summary or "",
            "url": a.original_url,
            "source": source_names.get(a.source_id, REMOVED_SOURCE_NAME),
        }
        for a in repository.get_pauta_articles(session)
    ]

    return {
        "groups": groups,
        "sources": sources,
        "pauta": pauta,
        "window_hours": HOME_WINDOW_HOURS,
        "window_start_iso": since.replace(tzinfo=timezone.utc).isoformat(),
        "today_label": f"{to_display_timezone(now):%d/%m/%Y}",
    }


def build_pauta_print(session: Session, now: datetime) -> dict[str, Any]:
    source_names = repository.get_source_names(session)
    return {
        "items": [
            {
                "title": a.ai_title or "(sem título)",
                "summary": format_summary(a),
                "url": a.original_url,
                "source": source_names.get(a.source_id, REMOVED_SOURCE_NAME),
                "date": f"{to_display_timezone(a.created_at):%d/%m/%Y %H:%M}",
            }
            for a in repository.get_pauta_articles(session)
        ],
        "generated_at": f"{to_display_timezone(now):%d/%m/%Y %H:%M}",
    }
