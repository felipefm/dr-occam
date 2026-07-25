"""Módulo de ingestão: coleta feeds RSS ativos, filtra e extrai o conteúdo
das notícias nativamente com trafilatura, persistindo os novos artigos
como PENDING.

Variáveis de ambiente esperadas:
    NEGATIVE_KEYWORDS: lista de palavras-chave separadas por vírgula
        (ex.: "cassino,aposta,horoscopo"). Notícias cujo título ou URL
        contenham qualquer uma delas são descartadas antes da extração.

Nota de arquitetura: a extração de conteúdo roda in-process via
trafilatura (extract sobre HTML baixado pelo httpx.AsyncClient
compartilhado), sem depender de um microsserviço de scraping externo.
Isso evita o custo de CPU/RAM de abrir instâncias de navegador (ex.:
Browserless) para cada notícia. Como consequência, os artigos de uma
mesma fonte são raspados estritamente em sequência (um de cada vez, com
uma pausa entre eles) para não sobrecarregar o processo.

Resiliência da extração: quando o trafilatura não consegue baixar ou
extrair a página (bloqueio anti-bot, paywall, 401 etc.), caímos de volta
para o texto já entregue no próprio feed RSS (tag <content:encoded> ou
<description>), comum em instâncias do RSSHub. Ver _EXTRACTORS.
Requisições HTTP (feed e página do artigo) passam por _get_with_retry,
que reexecuta com backoff exponencial apenas falhas transitórias
(429/502/503/504 ou erro de conexão) — útil para 503 esporádico do
RSSHub local.
"""

import asyncio
import html
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from database import engine
from http_headers import random_profile
from models import Article, ArticleStatus, Source, SourceType

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

NEGATIVE_KEYWORDS: list[str] = [
    keyword.strip().lower()
    for keyword in os.getenv("NEGATIVE_KEYWORDS", "").split(",")
    if keyword.strip()
]

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
SCRAPE_DELAY_SECONDS = 2
MAX_CONCURRENT_FEEDS = 5

# Status HTTP tratados como falha transitória (vale tentar de novo) — ex.:
# RSSHub local sob carga devolvendo 503. Erros como 401/403/404 não entram
# aqui de propósito: são bloqueios/ausências persistentes, insistir só
# atrasa o pipeline sem chance de sucesso.
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.5

# Tamanho mínimo para o texto vindo do <description>/<content:encoded> do
# feed ser aceito como conteúdo do artigo no fallback. Abaixo disso é só
# um teaser/resumo curto, não vale salvar como se fosse o artigo completo.
# 80 porque os resumos nativos da Reuters no feed costumam ter 150-180
# caracteres — um limiar de 200 descartava boa parte deles no fallback.
MIN_FALLBACK_CONTENT_LENGTH = 80

# Links de itens de RSS do Google News não são mais a URL do artigo original:
# são um redirecionamento para news.google.com que só resolve para o site de
# origem via JavaScript no navegador (não é um HTTP 30x normal). Pra extrair
# conteúdo real dessas fontes, replicamos a chamada interna que o próprio
# Google News usa (não documentada — pode quebrar se o Google mudar o
# formato; nesse caso o fallback abaixo apenas devolve a URL original, que
# falhará na extração como já acontecia antes).
_GOOGLE_NEWS_REDIRECT_RE = re.compile(r"^https://news\.google\.com/rss/articles/")
_GN_DECODE_ID_RE = re.compile(r'data-n-a-id="([^"]+)"')
_GN_DECODE_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_GN_DECODE_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_DECODE_RESULT_RE = re.compile(r'"garturlres","(https?://[^"]+)"')
_GN_BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


async def _resolve_google_news_url(client: httpx.AsyncClient, url: str) -> str:
    """Resolve um link de redirecionamento do Google News para a URL real do
    artigo no site de origem. Devolve a URL original, inalterada, para
    qualquer link que não seja do Google News ou se a decodificação falhar
    por qualquer motivo."""
    if not _GOOGLE_NEWS_REDIRECT_RE.match(url):
        return url

    try:
        page = await client.get(url)
        page.raise_for_status()
        html = page.text
        art_id = _GN_DECODE_ID_RE.search(html).group(1)  # type: ignore[union-attr]
        ts = _GN_DECODE_TS_RE.search(html).group(1)  # type: ignore[union-attr]
        sg = _GN_DECODE_SG_RE.search(html).group(1)  # type: ignore[union-attr]

        payload = (
            '[[["Fbv4je","[\\"garturlreq\\",[[\\"X\\",\\"X\\",'
            '[\\"X\\",\\"X\\"],null,null,1,1,\\"US:en\\",null,1,'
            'null,null,null,null,null,0,1],\\"X\\",\\"X\\",1,[1,1,1],'
            f'1,1,null,0,0,null,0],\\"{art_id}\\",{ts},\\"{sg}\\"]",null,"generic"]]]'
        )
        response = await client.post(
            _GN_BATCHEXECUTE_URL,
            data={"f.req": payload},
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        response.raise_for_status()
        match = _GN_DECODE_RESULT_RE.search(response.text)
        if not match:
            raise ValueError("resposta do batchexecute sem 'garturlres'")
        resolved = match.group(1)
        logger.info("Link do Google News resolvido: '%s' -> '%s'", url, resolved)
        return resolved
    except Exception as exc:
        logger.warning("Falha ao decodificar link do Google News '%s': %s", url, exc)
        return url


def _is_blocked_by_keywords(title: str, url: str) -> bool:
    haystack = f"{title} {url}".lower()
    return any(keyword in haystack for keyword in NEGATIVE_KEYWORDS)


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET com backoff exponencial, mas só para falhas transitórias
    (_RETRYABLE_STATUS_CODES ou erro de conexão/timeout). Um 401/403/404
    retorna imediatamente na primeira tentativa — quem chama decide o que
    fazer (ex.: cair no fallback de conteúdo do feed)."""
    attempt = 1
    while True:
        try:
            response = await client.get(url)
        except httpx.TransportError:
            if attempt >= _MAX_ATTEMPTS:
                raise
        else:
            if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= _MAX_ATTEMPTS:
                return response

        delay = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        logger.info(
            "Tentativa %d/%d falhou para '%s'; nova tentativa em %.1fs",
            attempt, _MAX_ATTEMPTS, url, delay,
        )
        await asyncio.sleep(delay)
        attempt += 1


_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html_fragment: str) -> str:
    """Conversão mínima de um fragmento de HTML (ex.: <description> ou
    <content:encoded> de um feed) para texto puro — não precisamos do
    poder completo do trafilatura aqui, é só um trecho curto, não uma
    página inteira."""
    text = _TAG_RE.sub(" ", html_fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _entry_feed_content(entry: feedparser.FeedParserDict) -> str | None:
    """Extrai o texto já embutido no próprio item do feed (RSSHub e vários
    outros geradores de RSS entregam o artigo completo em
    <content:encoded>; feeds mais simples só têm um resumo em
    <description>). Usado como fallback quando a página de origem não
    pode ser raspada diretamente."""
    content_list = getattr(entry, "content", None)
    raw = content_list[0].get("value") if content_list else getattr(entry, "summary", None)
    if not raw:
        return None
    return _html_to_text(raw) or None


async def _fetch_rss_entries(
    client: httpx.AsyncClient, source: Source
) -> list[tuple[str, str, str | None]]:
    """Busca o feed RSS e retorna (url, título, conteúdo-do-feed) de cada
    entrada. O terceiro elemento alimenta o fallback de extração quando a
    página do artigo em si não puder ser baixada/raspada."""
    logger.info("Buscando feed RSS de '%s' (%s)", source.name, source.url)
    try:
        response = await _get_with_retry(client, source.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Falha ao buscar feed RSS de '%s': %s", source.name, exc)
        return []

    parsed = await asyncio.to_thread(feedparser.parse, response.content)

    entries: list[tuple[str, str, str | None]] = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        if not link:
            continue
        title = getattr(entry, "title", "")
        entries.append((link, title, _entry_feed_content(entry)))

    logger.info("Fonte '%s': %d links encontrados no feed RSS", source.name, len(entries))
    return entries


async def _extract_via_trafilatura(
    client: httpx.AsyncClient, url: str, _feed_content: str | None
) -> str | None:
    """Estratégia principal: baixa a página com o client HTTP compartilhado
    (já configurado com o perfil de navegador sorteado em run_ingestion —
    ver http_headers.py) e extrai o texto principal com trafilatura. Não
    usa mais trafilatura.fetch_url — assim
    o download da página passa pelo mesmo mecanismo de retry/headers usado
    para o resto do pipeline, em vez de duplicar essa configuração."""
    try:
        response = await _get_with_retry(client, url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("trafilatura: falha ao baixar '%s': %s", url, exc)
        return None

    content = await asyncio.to_thread(trafilatura.extract, response.text)
    if not content or not content.strip():
        logger.warning("trafilatura não conseguiu extrair texto principal de '%s'", url)
        return None

    logger.info("Conteúdo extraído via trafilatura de '%s' (%d caracteres)", url, len(content))
    return content


async def _extract_via_feed_content(
    _client: httpx.AsyncClient, url: str, feed_content: str | None
) -> str | None:
    """Fallback: usa o texto já entregue no próprio feed RSS (comum em
    instâncias do RSSHub, que costumam preencher <content:encoded> com o
    artigo completo) quando a página de origem está bloqueada (paywall,
    anti-bot, 401 etc.) e o trafilatura não teve o que extrair."""
    if not feed_content or len(feed_content) < MIN_FALLBACK_CONTENT_LENGTH:
        return None
    logger.info(
        "Conteúdo obtido via fallback do feed RSS para '%s' (%d caracteres)",
        url, len(feed_content),
    )
    return feed_content


# Cadeia de extração: cada estratégia é tentada em ordem até uma retornar
# conteúdo. Adicionar uma nova fonte de conteúdo (ex.: cache de AMP,
# Wayback Machine) é só escrever a função e colocá-la aqui — não precisa
# de uma hierarquia de classes para duas ou três estratégias.
_EXTRACTORS: list[Callable[[httpx.AsyncClient, str, str | None], Awaitable[str | None]]] = [
    _extract_via_trafilatura,
    _extract_via_feed_content,
]


async def _scrape_article(
    client: httpx.AsyncClient, url: str, feed_content: str | None
) -> str | None:
    """Extrai o texto principal da notícia tentando cada estratégia em
    _EXTRACTORS, em ordem, até uma funcionar."""
    for extractor in _EXTRACTORS:
        content = await extractor(client, url, feed_content)
        if content:
            return content
    return None


def _existing_urls(session: Session, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    statement = select(Article.original_url).where(Article.original_url.in_(urls))
    return set(session.exec(statement).all())


def _count_articles_today(session: Session, source_id: int) -> int:
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    statement = (
        select(func.count())
        .select_from(Article)
        .where(Article.source_id == source_id, Article.created_at >= start_of_day)
    )
    return session.exec(statement).one()


async def _process_source(client: httpx.AsyncClient, source: Source) -> int:
    """Processa uma fonte ativa e retorna a quantidade de artigos salvos."""
    assert source.id is not None

    logger.info(
        "Iniciando leitura da fonte '%s' (tipo=%s, url=%s)",
        source.name, source.source_type, source.url,
    )

    if source.source_type != SourceType.RSS:
        logger.info(
            "Tipo de fonte '%s' não suportado para coleta de links: %s",
            source.source_type,
            source.name,
        )
        return 0

    candidates = await _fetch_rss_entries(client, source)
    if not candidates:
        return 0

    with Session(engine) as session:
        already_today = _count_articles_today(session, source.id)
        remaining_quota = max(source.max_daily_articles - already_today, 0)
        logger.info(
            "Fonte '%s': %d artigos já salvos hoje, cota diária restante = %d",
            source.name, already_today, remaining_quota,
        )
        if remaining_quota == 0:
            logger.info("Fonte '%s' já atingiu o limite diário de artigos", source.name)
            return 0
        known_urls = _existing_urls(session, [url for url, _, _ in candidates])

    keyword_filtered: list[str] = []
    fresh_candidates: list[tuple[str, str, str | None]] = []
    for url, title, feed_content in candidates:
        if url in known_urls:
            continue
        if _is_blocked_by_keywords(title, url):
            keyword_filtered.append(url)
            continue
        fresh_candidates.append((url, title, feed_content))

    if keyword_filtered:
        logger.info(
            "Fonte '%s': %d links descartados pelo filtro de negative keywords: %s",
            source.name, len(keyword_filtered), keyword_filtered,
        )

    fresh_candidates = fresh_candidates[:remaining_quota]
    logger.info(
        "Fonte '%s': %d artigos novos a coletar (de %d links no feed)",
        source.name, len(fresh_candidates), len(candidates),
    )

    if not fresh_candidates:
        return 0

    # Extração estritamente sequencial (um link por vez, com pausa entre
    # eles) para poupar CPU/RAM — sem isso, várias extrações concorrentes
    # de páginas pesadas podiam sobrecarregar o processo.
    resolved_urls: list[str] = []
    contents: list[str | None] = []
    for i, (url, _title, feed_content) in enumerate(fresh_candidates):
        resolved_url = await _resolve_google_news_url(client, url)
        resolved_urls.append(resolved_url)
        contents.append(await _scrape_article(client, resolved_url, feed_content))
        if i < len(fresh_candidates) - 1:
            await asyncio.sleep(SCRAPE_DELAY_SECONDS)

    saved = 0
    with Session(engine) as session:
        for resolved_url, content in zip(resolved_urls, contents):
            if not content:
                logger.warning("Extração retornou conteúdo vazio para '%s'", resolved_url)
                continue

            session.add(
                Article(
                    source_id=source.id,
                    original_url=resolved_url,
                    original_content=content,
                    status=ArticleStatus.PENDING,
                )
            )
            try:
                session.commit()
                saved += 1
            except IntegrityError:
                session.rollback()
                logger.info("Artigo '%s' já existia (condição de corrida)", resolved_url)

    return saved


async def run_ingestion() -> dict[str, int]:
    """Executa um ciclo completo de ingestão para todas as fontes ativas.

    Retorna um dicionário {nome_da_fonte: artigos_salvos}.
    """
    logger.info("Iniciando ciclo de ingestão")

    with Session(engine) as session:
        sources = session.exec(
            select(Source).where(Source.active.is_(True))
        ).all()

    logger.info("Encontradas %d fontes ativas", len(sources))
    if not sources:
        logger.info("Nenhuma fonte ativa — encerrando ciclo de ingestão")
        return {}

    feed_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

    async def _bounded(source: Source) -> tuple[str, int]:
        async with feed_semaphore:
            count = await _process_source(client, source)
            return source.name, count

    profile = random_profile()
    logger.info("Ingestão: usando perfil de navegador '%s'", profile.name)
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT, headers=profile.headers()
    ) as client:
        results = await asyncio.gather(*(_bounded(source) for source in sources))

    result_dict = dict(results)
    logger.info("Ciclo de ingestão concluído: %s", result_dict)
    return result_dict


if __name__ == "__main__":
    summary = asyncio.run(run_ingestion())
    logger.info("Ingestão concluída: %s", summary)
