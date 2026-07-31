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

_running = False


def try_acquire() -> bool:
    """Marca o pipeline como em execução e retorna True, ou retorna False
    sem alterar nada se já houver uma execução em andamento."""
    global _running
    if _running:
        return False
    _running = True
    return True


def release() -> None:
    global _running
    _running = False


def is_running() -> bool:
    return _running
