"""Gerenciamento de engine e sessões do banco de dados."""

import logging
import os
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

from embeddings.repository import attach_vec_extension, ensure_vec_table

logger = logging.getLogger(__name__)

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/occam.db")

_connect_args: dict[str, bool] = (
    {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(DATABASE_URL, echo=False, connect_args=_connect_args)

# Precisa rodar logo após criar o engine e antes de qualquer conexão real
# ser aberta (inclusive as da migração abaixo): o listener só é aplicado a
# conexões físicas novas do pool, então registrar depois de uma conexão já
# ter sido usada deixaria essa conexão sem a extensão carregada.
attach_vec_extension(engine)


def _migrate_article_source_id_nullable() -> None:
    """Migração única: `article.source_id` passou de NOT NULL para nullable,
    para permitir excluir uma fonte sem perder o histórico de artigos já
    coletados (ver models.py). SQLite não suporta alterar uma constraint
    NOT NULL existente via ALTER TABLE, então recriamos a tabela 'article'
    preservando todos os dados sempre que o schema em disco ainda estiver
    no formato antigo. Não faz nada em uma instalação nova (tabela ainda
    não existe) nem se a migração já foi aplicada antes."""
    with engine.begin() as conn:
        columns_info = conn.exec_driver_sql("PRAGMA table_info(article)").fetchall()
        if not columns_info:
            return  # instalação nova — create_all cria já no formato certo

        source_id_column = next((c for c in columns_info if c[1] == "source_id"), None)
        if source_id_column is None or source_id_column[3] == 0:
            return  # coluna já nullable — migração já aplicada

        logger.info("Migrando tabela 'article': source_id passa a aceitar NULL")
        column_names = ", ".join(c[1] for c in columns_info)
        conn.exec_driver_sql("ALTER TABLE article RENAME TO article_old")

        # SQLite mantém os índices antigos (mesmo nome) apontando para
        # article_old após o RENAME; como nomes de índice são únicos no
        # banco todo (não por tabela), eles precisam ser removidos antes
        # de recriar 'article', senão o create() abaixo colide com eles.
        old_indexes = conn.exec_driver_sql(
            "PRAGMA index_list(article_old)"
        ).fetchall()
        for index in old_indexes:
            index_name = index[1]
            if not index_name.startswith("sqlite_autoindex"):
                conn.exec_driver_sql(f"DROP INDEX {index_name}")

        # checkfirst=False: sabemos que 'article' não existe neste ponto
        # (acabamos de renomeá-la); com checkfirst=True o SQLAlchemy usa
        # cache de reflexão que não enxerga os DROP INDEX acima executados
        # via SQL puro, e acaba pulando a criação dos índices em silêncio.
        SQLModel.metadata.tables["article"].create(conn, checkfirst=False)
        conn.exec_driver_sql(
            f"INSERT INTO article ({column_names}) "
            f"SELECT {column_names} FROM article_old"
        )
        conn.exec_driver_sql("DROP TABLE article_old")
        logger.info("Migração de 'article' concluída")


def create_db_and_tables() -> None:
    _migrate_article_source_id_nullable()
    SQLModel.metadata.create_all(engine)
    ensure_vec_table(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
