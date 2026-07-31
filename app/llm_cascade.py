"""Leitura da cascata de modelos de chat/instruct configurada via variáveis
de ambiente numeradas (LLM_MODEL_N / LLM_API_BASE_N / LLM_API_KEY_N).

Compartilhado por llm_processor.py (título/resumo neutros dos artigos) e
search/intent_service.py (extração de filtro de data da busca) — mesmo
tipo de modelo e mesma cascata de fallback, sem motivo para configurar
duas vezes.
"""

import os
from typing import Any


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


LLM_CASCADE: list[dict[str, Any]] = load_llm_cascade()
