"""Configuração de ambiente do resumo por IA da pauta.

Variáveis de ambiente:
    SUMMARY_LLM_TIMEOUT_SECONDS: timeout da chamada de chat que gera o texto
        de síntese (default 240, mesmo valor inicial de LLM_TIMEOUT_SECONDS/
        INTENT_LLM_TIMEOUT_SECONDS — ver `.env`). O prompt de resumo é bem
        maior que os prompts de título/resumo por artigo; o tempo real de
        cada geração sai no log (ver summary/engine.py).
    PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE: quantos caracteres do texto
        original de cada notícia entram no prompt do resumo da pauta
        (default 3000). A pauta é curta (escolhida a dedo), então dá para
        mandar o texto original em vez de só o resumo do pipeline.
    PAUTA_SUMMARY_MAX_TOTAL_CHARS: teto do texto somado de todas as
        notícias da pauta (default 60000, ~15-20 mil tokens). Com uma pauta
        grande, o limite por notícia encolhe para caber nesse total.
"""

import os

SUMMARY_LLM_TIMEOUT_SECONDS: int = int(os.getenv("SUMMARY_LLM_TIMEOUT_SECONDS", "240"))
PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE: int = int(
    os.getenv("PAUTA_SUMMARY_MAX_CHARS_PER_ARTICLE", "3000")
)
PAUTA_SUMMARY_MAX_TOTAL_CHARS: int = int(os.getenv("PAUTA_SUMMARY_MAX_TOTAL_CHARS", "60000"))
