"""Estado compartilhado de execução do pipeline: evita que uma varredura
(`run_pipeline`, disparada manualmente ou pelo agendador) e um
reprocessamento de artigos DEAD (`run_llm_processing` standalone) rodem ao
mesmo tempo. Sem essa trava, cada clique repetido em "Executar varredura"
(ou uma varredura agendada coincidindo com uma manual) empilhava execuções
concorrentes independentes — cada uma com seu próprio conjunto de conexões
HTTP e chamadas de IA simultâneas — o que já chegou a esgotar a memória do
host.

Pressupõe um único processo/worker uvicorn (mesma premissa já documentada
em scheduler.py) — um flag em memória de processo é suficiente, sem
precisar de lock distribuído."""

import time

_running = False

# Andamento da varredura atual, para a tela inicial (GET /api/home/status):
# fontes lidas, notícias novas encontradas, notícias resumidas pela IA e
# uma previsão de término. None quando a execução não informa andamento
# (ex.: backfill de embeddings) ou quando nada está rodando.
_progress: dict[str, float | int | str] | None = None

# Segundos por notícia na etapa da IA, medido na última varredura (vale
# até o container reiniciar). O valor inicial é o observado com o LM
# Studio local; serve só para a previsão antes de a IA começar.
_seconds_per_article = 8.0


def try_acquire() -> bool:
    """Marca o pipeline como em execução e retorna True, ou retorna False
    sem alterar nada se já houver uma execução em andamento."""
    global _running, _progress
    if _running:
        return False
    _running = True
    _progress = None
    return True


def release() -> None:
    global _running, _progress
    _running = False
    _progress = None


def is_running() -> bool:
    return _running


def start_ingestion(sources_total: int) -> None:
    global _progress
    _progress = {
        "stage": "ingestion",
        "sources_done": 0,
        "sources_total": sources_total,
        "articles_found": 0,
        "llm_done": 0,
        "llm_total": 0,
        "stage_started": time.monotonic(),
    }


def source_done(articles_found: int) -> None:
    if _progress is not None and _progress["stage"] == "ingestion":
        _progress["sources_done"] = int(_progress["sources_done"]) + 1
        _progress["articles_found"] = int(_progress["articles_found"]) + articles_found


def start_llm(total: int) -> None:
    global _progress
    if _progress is None:
        # Reprocessamento de DEAD: só a etapa da IA, sem ingestão antes.
        start_ingestion(0)
    assert _progress is not None
    _progress.update(stage="llm", llm_done=0, llm_total=total, stage_started=time.monotonic())


def article_done() -> None:
    global _seconds_per_article
    if _progress is None or _progress["stage"] != "llm":
        return
    _progress["llm_done"] = min(int(_progress["llm_done"]) + 1, int(_progress["llm_total"]))
    _seconds_per_article = (time.monotonic() - float(_progress["stage_started"])) / int(_progress["llm_done"])


def _eta_seconds(p: dict) -> int | None:
    elapsed = time.monotonic() - float(p["stage_started"])
    if p["stage"] == "ingestion":
        done, total = int(p["sources_done"]), int(p["sources_total"])
        if done == 0:
            return None
        # Projeta as notícias novas das fontes que faltam pela média das
        # já lidas, e o tempo da IA pelo ritmo da última varredura.
        remaining_ingestion = elapsed / done * (total - done)
        projected_articles = int(p["articles_found"]) * total / done
        return round(remaining_ingestion + projected_articles * _seconds_per_article)
    done, total = int(p["llm_done"]), int(p["llm_total"])
    rate = elapsed / done if done else _seconds_per_article
    return round(rate * (total - done))


def progress() -> dict[str, int | str | None] | None:
    if _progress is None:
        return None
    view = {k: v for k, v in _progress.items() if k != "stage_started"}
    view["eta_seconds"] = _eta_seconds(_progress)
    return view
