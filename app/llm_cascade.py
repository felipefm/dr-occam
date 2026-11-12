"""Leitura da cascata de modelos de chat/instruct configurada via variáveis
de ambiente numeradas (LLM_MODEL_N / LLM_API_BASE_N / LLM_API_KEY_N).

Compartilhado por llm_processor.py (título/resumo neutros dos artigos) e
search/intent_service.py (extração de filtro de data da busca) — mesmo
tipo de modelo e mesma cascata de fallback, sem motivo para configurar
duas vezes.
"""

import os
from typing import Any

import llm_server


def load_llm_cascade() -> list[dict[str, Any]]:
    """Lê LLM_MODEL_N / LLM_API_BASE_N / LLM_API_KEY_N em sequência a
    partir de N=1 até a numeração parar de existir."""
    entries: list[dict[str, Any]] = []
    n = 1
    while True:
        model = os.getenv(f"LLM_MODEL_{n}")
        if not model:
            break
        entry: dict[str, Any] = {"model": model}
        api_base = os.getenv(f"LLM_API_BASE_{n}")
        api_key = os.getenv(f"LLM_API_KEY_{n}")
        if api_base:
            entry["api_base"] = api_base
        if api_key:
            entry["api_key"] = api_key
        entries.append(entry)
        n += 1
    return entries


_ENV_CASCADE: list[dict[str, Any]] = load_llm_cascade()


def env_local_entry() -> dict[str, Any] | None:
    """Primeira entrada da cascata do ambiente que aponta para o servidor
    local — é a que tem o modelo trocável no /admin."""
    return next((e for e in _ENV_CASCADE if llm_server.is_local(e.get("api_base"))), None)


def get_llm_cascade() -> list[dict[str, Any]]:
    """A cascata do ambiente com endereço e modelo do servidor local trocados
    pelos salvos no /admin, se houver (ver llm_server.py). Chamar a cada uso
    — não guardar o resultado em constante de módulo, senão a troca no
    /admin só valeria depois de reiniciar."""
    model_override = llm_server.get_overrides().model
    local_entry = env_local_entry()
    cascade: list[dict[str, Any]] = []
    for env_entry in _ENV_CASCADE:
        entry = dict(env_entry)
        if "api_base" in entry:
            entry["api_base"] = llm_server.resolve_api_base(entry["api_base"])
        if model_override and env_entry is local_entry:
            entry["model"] = llm_server.to_litellm_model(model_override, env_entry["model"])
        cascade.append(entry)
    return cascade
