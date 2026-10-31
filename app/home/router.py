"""Rotas da tela inicial: a página, a impressão da pauta e as APIs de
leitura/pauta usadas pelo JavaScript de index.html. Lógica de montagem
em home.service; consultas em home.repository."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session

import pipeline_state
from database import engine
from home import repository, service
from home.config import HOME_WINDOW_HOURS, MAX_READ_BATCH
from models import Article

router = APIRouter(tags=["Tela inicial"])
templates = Jinja2Templates(directory="templates")


class ReadPayload(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=MAX_READ_BATCH)
    read: bool = Field(default=True, description="False desmarca (volta a não lida).")


class PautaPayload(BaseModel):
    in_pauta: bool


@router.get(
    "/",
    response_class=HTMLResponse,
    tags=["Feed"],
    summary="Página inicial: caixa de entrada de notícias",
    description=(
        f"Renderiza todos os artigos `PROCESSED` das últimas {HOME_WINDOW_HOURS} horas "
        "(`HOME_WINDOW_HOURS`), agrupados por varredura, com estado de lido/não lido e "
        "pauta guardados no banco. Filtros por fonte e \"só não lidas\" rodam no navegador."
    ),
    response_description="Página HTML da tela inicial.",
)
def homepage(request: Request) -> Response:
    with Session(engine) as session:
        context = service.build_home(session, datetime.now(timezone.utc))
    return templates.TemplateResponse(request=request, name="index.html", context=context)


@router.get(
    "/pauta/imprimir",
    response_class=HTMLResponse,
    summary="Página de impressão da pauta",
    description=(
        "Versão para impressão da pauta, com título, resumo, fonte e link de cada "
        "notícia — o \"Baixar PDF\" da tela inicial abre esta página para o navegador "
        "salvar como PDF."
    ),
    response_description="Página HTML da pauta pronta para imprimir.",
)
def pauta_print(request: Request) -> Response:
    with Session(engine) as session:
        context = service.build_pauta_print(session, datetime.now(timezone.utc))
    return templates.TemplateResponse(request=request, name="pauta_print.html", context=context)


@router.post(
    "/api/articles/read",
    summary="Marcar artigos como lidos (ou não lidos)",
    description=(
        "Marca em lote os artigos de `ids` como lidos (`read=true`) ou não lidos "
        f"(`read=false`). Até {MAX_READ_BATCH} ids por chamada; ids inexistentes são "
        "ignorados."
    ),
    response_description="Quantidade de artigos alterados.",
)
def mark_read(payload: ReadPayload) -> dict[str, int]:
    read_at = datetime.now(timezone.utc) if payload.read else None
    with Session(engine) as session:
        updated = repository.set_read(session, payload.ids, read_at)
    return {"updated": updated}


@router.put(
    "/api/articles/{article_id}/pauta",
    summary="Adicionar ou remover um artigo da pauta",
    response_description="O estado do artigo na pauta após a alteração.",
)
def set_pauta(article_id: int, payload: PautaPayload) -> dict[str, int | bool]:
    with Session(engine) as session:
        article = session.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Artigo não encontrado")
        # Readicionar quem já está na pauta mantém a posição original.
        if payload.in_pauta and article.in_pauta_at is None:
            repository.set_in_pauta(session, article, datetime.now(timezone.utc))
        elif not payload.in_pauta:
            repository.set_in_pauta(session, article, None)
    return {"id": article_id, "in_pauta": payload.in_pauta}


@router.delete(
    "/api/pauta",
    summary="Esvaziar a pauta",
    response_description="Quantidade de artigos retirados da pauta.",
)
def clear_pauta() -> dict[str, int]:
    with Session(engine) as session:
        removed = repository.clear_pauta(session)
    return {"removed": removed}


@router.post(
    "/api/pauta/archive",
    status_code=201,
    summary="Arquivar a pauta atual",
    description=(
        "Guarda uma cópia da pauta atual (título, resumo, fonte e link de cada notícia) "
        "em `pauta_archive` e esvazia a pauta. Responde 409 se a pauta estiver vazia. "
        "As pautas arquivadas ficam em `/pautas`."
    ),
    response_description="O id da pauta arquivada.",
)
def archive_pauta() -> dict[str, int]:
    with Session(engine) as session:
        try:
            archive_id = service.archive_pauta(session, datetime.now(timezone.utc))
        except service.EmptyPautaError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": archive_id}


@router.get(
    "/pautas",
    response_class=HTMLResponse,
    summary="Lista de pautas arquivadas",
    response_description="Página HTML com as pautas arquivadas, mais recentes primeiro.",
)
def pauta_archives(request: Request) -> Response:
    with Session(engine) as session:
        context = service.build_pauta_archives(session)
    return templates.TemplateResponse(request=request, name="pautas.html", context=context)


@router.get(
    "/pautas/{archive_id}",
    response_class=HTMLResponse,
    summary="Pauta arquivada, pronta para imprimir",
    response_description="Página HTML da pauta arquivada, no mesmo formato de /pauta/imprimir.",
)
def archived_pauta(request: Request, archive_id: int) -> Response:
    with Session(engine) as session:
        context = service.build_archived_pauta_print(session, archive_id)
    if context is None:
        raise HTTPException(status_code=404, detail="Pauta arquivada não encontrada")
    return templates.TemplateResponse(request=request, name="pauta_print.html", context=context)


@router.get(
    "/api/home/status",
    summary="Situação da varredura e contagem de artigos para a tela inicial",
    description=(
        "Consultado periodicamente pela tela inicial: diz se há uma execução em "
        "andamento e quantos artigos `PROCESSED` existem a partir de `since` (o início "
        "da janela da página aberta). A diferença para o que a página já mostra é o "
        "número de notícias novas a oferecer, sem recarregar a lista sozinha."
    ),
)
def home_status(
    since: datetime = Query(..., description="Início da janela da página, ISO 8601."),
) -> dict[str, bool | int]:
    if since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    with Session(engine) as session:
        processed = repository.count_processed_since(session, since)
    return {"pipeline_running": pipeline_state.is_running(), "processed_since": processed}
