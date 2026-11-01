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
from summary import pauta_repository as summary_repository
from summary.modes import mode_options

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

    # Fontes com mais notícias a ler primeiro (desempate: mais notícias na
    # janela, depois nome). O JavaScript refaz essa ordem conforme a leitura.
    window_items = [item for g in groups for item in g["items"]]
    source_counts = Counter(item["source"] for item in window_items)
    unread_counts = Counter(item["source"] for item in window_items if not item["is_read"])
    sources = sorted(
        source_counts,
        key=lambda name: (-unread_counts[name], -source_counts[name], name.casefold()),
    )
    # Fontes ativas sem nada na janela também aparecem (sem filtro), para
    # uma fonte quebrada ou parada não sumir da tela sem ninguém notar.
    quiet_sources = sorted(repository.get_active_source_names(session) - set(sources), key=str.casefold)

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
        "quiet_sources": quiet_sources,
        "source_totals": source_counts,
        "pauta": pauta,
        "window_hours": HOME_WINDOW_HOURS,
        "window_start_iso": since.replace(tzinfo=timezone.utc).isoformat(),
        "today_label": f"{to_display_timezone(now):%d/%m/%Y}",
        "summary_modes": mode_options(),
    }


def _pauta_items(session: Session) -> list[dict[str, Any]]:
    """A pauta atual no formato da impressão — o mesmo guardado ao arquivar."""
    source_names = repository.get_source_names(session)
    return [
        {
            "title": a.ai_title or "(sem título)",
            "summary": format_summary(a),
            "url": a.original_url,
            "source": source_names.get(a.source_id, REMOVED_SOURCE_NAME),
            "date": f"{to_display_timezone(a.created_at):%d/%m/%Y %H:%M}",
        }
        for a in repository.get_pauta_articles(session)
    ]


def _chosen_summary_text(session: Session, archive_id: int | None) -> str:
    chosen = summary_repository.get_chosen(session, archive_id)
    return chosen.text if chosen else ""


def build_pauta_print(session: Session, now: datetime) -> dict[str, Any]:
    return {
        "items": _pauta_items(session),
        "subtitle": f"gerada em {to_display_timezone(now):%d/%m/%Y %H:%M}",
        "summary_text": _chosen_summary_text(session, None),
        "archive_id": None,
        "article_ids": [a.id for a in repository.get_pauta_articles(session)],
        "summary_modes": mode_options(),
    }


class EmptyPautaError(Exception):
    pass


def archive_pauta(session: Session, now: datetime) -> int:
    """Arquiva a pauta atual e a esvazia. Retorna o id do arquivo."""
    items = _pauta_items(session)
    if not items:
        raise EmptyPautaError("A pauta está vazia — não há nada para arquivar.")
    return repository.archive_and_clear_pauta(session, items, now).id


def _archive_label(archived_at: datetime) -> str:
    return f"{to_display_timezone(archived_at):%d/%m/%Y %H:%M}"


def build_pauta_archives(session: Session) -> dict[str, Any]:
    archives = repository.list_pauta_archives(session)
    with_summary = summary_repository.archive_ids_with_summary(session)
    return {
        "archives": [
            {
                "id": a.id,
                "label": _archive_label(a.archived_at),
                "count": len(a.items),
                "titles": [item["title"] for item in a.items],
                "has_summary": a.id in with_summary,
            }
            for a in archives
        ]
    }


def build_archived_pauta_print(session: Session, archive_id: int) -> dict[str, Any] | None:
    archive = repository.get_pauta_archive(session, archive_id)
    if archive is None:
        return None
    return {
        "items": archive.items,
        "subtitle": f"arquivada em {_archive_label(archive.archived_at)}",
        "back_url": "/pautas",
        "summary_text": _chosen_summary_text(session, archive_id),
        "archive_id": archive_id,
        "article_ids": [],
        "summary_modes": mode_options(),
    }
