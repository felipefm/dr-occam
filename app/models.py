"""Esquema do banco de dados do Dr. Occam."""

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, Text, Uuid
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, Relationship, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, Enum):
    RSS = "RSS"
    HTML_SCRAPE = "HTML_SCRAPE"


class ArticleStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    DEAD = "DEAD"
    DEDUPLICATED = "DEDUPLICATED"


class Source(SQLModel, table=True):
    __tablename__ = "source"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=255, index=True)
    url: str = Field(nullable=False, max_length=2048)
    source_type: SourceType = Field(
        sa_column=Column(SAEnum(SourceType, name="source_type_enum"), nullable=False)
    )
    active: bool = Field(default=True, nullable=False)
    max_daily_articles: int = Field(default=50, nullable=False)

    articles: list["Article"] = Relationship(back_populates="source")


class Article(SQLModel, table=True):
    __tablename__ = "article"

    id: int | None = Field(default=None, primary_key=True)
    # Nullable: ao excluir uma fonte, os artigos já coletados dela devem
    # continuar no banco/feed (histórico preservado), só perdem o vínculo —
    # ver DELETE /api/sources/{id} em main.py e a migração em database.py.
    source_id: int | None = Field(default=None, foreign_key="source.id", index=True)
    original_url: str = Field(
        nullable=False, unique=True, index=True, max_length=2048
    )
    original_content: str = Field(sa_column=Column(Text, nullable=False))
    status: ArticleStatus = Field(
        default=ArticleStatus.PENDING,
        sa_column=Column(
            SAEnum(ArticleStatus, name="article_status_enum"), nullable=False
        ),
    )
    ai_title: str | None = Field(default=None, max_length=512)
    ai_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    cluster_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(Uuid, nullable=True, index=True)
    )
    retry_count: int = Field(default=0, nullable=False)
    is_truncated: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow, nullable=False, index=True
    )

    source: Source | None = Relationship(back_populates="articles")
    processing_logs: list["LLMProcessingLog"] = Relationship(back_populates="article")


class LLMProcessingLog(SQLModel, table=True):
    __tablename__ = "llm_processing_log"

    id: int | None = Field(default=None, primary_key=True)
    article_id: int = Field(foreign_key="article.id", nullable=False, index=True)
    llm_provider: str = Field(nullable=False, max_length=100)
    prompt_version: str = Field(nullable=False, max_length=50)
    prompt_tokens: int = Field(nullable=False)
    completion_tokens: int = Field(nullable=False)
    status_code: int = Field(nullable=False)

    article: Article = Relationship(back_populates="processing_logs")
