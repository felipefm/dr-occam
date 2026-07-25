"""Dr. Occam — API FastAPI: gatilho do pipeline, feed RSS e leitura rápida."""

import collections
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, AsyncIterator
from urllib.parse import quote
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Field, Session, SQLModel, select

from database import create_db_and_tables, engine
from ingestion import run_ingestion
from llm_processor import run_llm_processing
from models import Article, ArticleStatus, Source, SourceType

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

LOG_BUFFER_SIZE = 500
_log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_SIZE)


class _BufferLogHandler(logging.Handler):
    """Mantém as últimas LOG_BUFFER_SIZE linhas de log em memória para o
    modal de logs do /admin — evita depender de `docker logs` no terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append(self.format(record))


_buffer_handler = _BufferLogHandler()
_buffer_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.getLogger().addHandler(_buffer_handler)

TRUNCATION_WARNING = (
    "[Aviso: O texto original era demasiadamente longo e foi truncado "
    "para proteção de contexto da IA]"
)
FEED_LIMIT = 100
HOMEPAGE_LIMIT = 50
DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    yield


app = FastAPI(title="Dr. Occam", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


class SourceCreatePayload(SQLModel):
    name: str = Field(max_length=255)
    url: str = Field(max_length=2048)


class SourceLimitPayload(SQLModel):
    max_daily_articles: int = Field(gt=0)


def _format_summary(article: Article) -> str:
    summary = article.ai_summary or ""
    if article.is_truncated:
        summary = f"{summary}\n\n{TRUNCATION_WARNING}"
    return summary


def _rfc822(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def _to_display_timezone(dt: datetime) -> datetime:
    """Converte um datetime armazenado em UTC (naive ou aware) para exibição em -03:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TIMEZONE)


def _whatsapp_share_url(title: str, summary: str, url: str) -> str:
    text = f"{title}\n\n{summary}\n\n{url}"
    return f"https://wa.me/?text={quote(text)}"


def _telegram_share_url(title: str, summary: str, url: str) -> str:
    text = f"{title}\n\n{summary}"
    return f"https://t.me/share/url?url={quote(url)}&text={quote(text)}"


async def _run_pipeline() -> None:
    logger.info("Pipeline: gatilho recebido, iniciando execução em background")
    try:
        logger.info("Pipeline: iniciando etapa de ingestão")
        ingestion_summary = await run_ingestion()
        logger.info("Pipeline: ingestão concluída: %s", ingestion_summary)

        logger.info("Pipeline: iniciando etapa de processamento LLM")
        llm_summary = await run_llm_processing()
        logger.info("Pipeline: processamento LLM concluído: %s", llm_summary)

        logger.info("Pipeline: execução finalizada com sucesso")
    except Exception as e:
        logger.error("Pipeline: falha durante a execução: %s", e)
        traceback.print_exc()


@app.post("/trigger-pipeline", status_code=202)
async def trigger_pipeline(background_tasks: BackgroundTasks) -> dict[str, str]:
    background_tasks.add_task(_run_pipeline)
    return {"message": "Pipeline iniciado"}


@app.get("/feed.xml")
def get_feed() -> Response:
    with Session(engine) as session:
        articles = session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.PROCESSED)
            .order_by(Article.created_at.desc())
            .limit(FEED_LIMIT)
        ).all()

        items = "".join(
            "<item>"
            f"<title>{escape(article.ai_title or '(sem título)')}</title>"
            f"<link>{escape(article.original_url)}</link>"
            f'<guid isPermaLink="false">{article.id}</guid>'
            f"<pubDate>{_rfc822(article.created_at)}</pubDate>"
            f"<description>{escape(_format_summary(article))}</description>"
            "</item>"
            for article in articles
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Dr. Occam</title>"
        "<link>/</link>"
        "<description>Agregador de notícias neutro</description>"
        "<language>pt-br</language>"
        f"<lastBuildDate>{_rfc822(datetime.now(timezone.utc))}</lastBuildDate>"
        f"{items}"
        "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")


@app.get("/", response_class=HTMLResponse)
def homepage(request: Request) -> Response:
    with Session(engine) as session:
        articles = session.exec(
            select(Article)
            .where(Article.status == ArticleStatus.PROCESSED)
            .order_by(Article.created_at.desc())
            .limit(HOMEPAGE_LIMIT)
        ).all()

        cards: list[dict[str, Any]] = []
        for article in articles:
            title = article.ai_title or "(sem título)"
            summary = _format_summary(article)
            cards.append(
                {
                    "title": title,
                    "url": article.original_url,
                    "date": _to_display_timezone(article.created_at).strftime("%d/%m/%Y %H:%M"),
                    "summary": summary,
                    "whatsapp_url": _whatsapp_share_url(title, summary, article.original_url),
                    "telegram_url": _telegram_share_url(title, summary, article.original_url),
                }
            )

    return templates.TemplateResponse(
        request=request, name="index.html", context={"cards": cards}
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> Response:
    with Session(engine) as session:
        sources = session.exec(select(Source).order_by(Source.id)).all()
    return templates.TemplateResponse(
        request=request, name="admin.html", context={"sources": sources}
    )


@app.get("/api/logs")
def get_logs() -> dict[str, list[str]]:
    return {"logs": list(_log_buffer)}


@app.post("/api/sources", status_code=201)
def create_source(payload: SourceCreatePayload) -> Source:
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="URL deve começar com http:// ou https://")

    with Session(engine) as session:
        source = Source(name=payload.name, url=payload.url, source_type=SourceType.RSS)
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' criada (id=%s, url=%s)", source.name, source.id, source.url
        )
        return source


@app.patch("/api/sources/{source_id}/toggle")
def toggle_source(source_id: int) -> Source:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        source.active = not source.active
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' (id=%s) -> active=%s", source.name, source.id, source.active
        )
        return source


@app.delete("/api/sources/{source_id}", status_code=204)
def delete_source(source_id: int) -> Response:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        logger.info("Admin: excluindo fonte '%s' (id=%s)", source.name, source.id)
        session.delete(source)
        session.commit()
    return Response(status_code=204)


@app.put("/api/sources/{source_id}/limit")
def update_source_limit(source_id: int, payload: SourceLimitPayload) -> Source:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")

        source.max_daily_articles = payload.max_daily_articles
        session.add(source)
        session.commit()
        session.refresh(source)
        logger.info(
            "Admin: fonte '%s' (id=%s) -> max_daily_articles=%d",
            source.name, source.id, source.max_daily_articles,
        )
        return source
