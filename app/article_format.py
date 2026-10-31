"""Formatação de artigos para exibição, compartilhada pelo feed RSS
(main.py) e pela tela inicial (home/)."""

from datetime import datetime, timezone

from models import Article
from timezone_utils import DISPLAY_TIMEZONE

TRUNCATION_WARNING = (
    "[Aviso: O texto original era demasiadamente longo e foi truncado "
    "para proteção de contexto da IA]"
)


def format_summary(article: Article) -> str:
    summary = article.ai_summary or ""
    if article.is_truncated:
        summary = f"{summary}\n\n{TRUNCATION_WARNING}"
    return summary


def to_display_timezone(dt: datetime) -> datetime:
    """Converte um datetime armazenado em UTC (naive ou aware) para exibição em -03:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TIMEZONE)

