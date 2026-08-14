"""Configuração de ambiente do resumo de notícias por período.

Variáveis de ambiente:
    SUMMARY_LLM_TIMEOUT_SECONDS: timeout da chamada de chat que gera o texto
        de síntese (default 240, mesmo valor inicial de LLM_TIMEOUT_SECONDS/
        INTENT_LLM_TIMEOUT_SECONDS — ver `.env`). O prompt de resumo é ordens
        de magnitude maior que os prompts de título/resumo por artigo, então
        pode precisar de um valor próprio depois de medir o tempo real (ver
        log em summary/service.py).
    SUMMARY_MAX_ARTICLES: teto rígido de artigos concatenados no prompt,
        além do teto de dias (1-3) já embutido no endpoint. Existe porque o
        volume por dia pode crescer (mais fontes cadastradas) sem que o
        teto de dias mude — protege contra o mesmo tipo de sobrecarga de
        host já documentado no incidente que motivou a trava de execução
        concorrente do pipeline (ver pipeline_state.py). Default 400,
        folga generosa sobre os ~192 artigos/dia medidos na análise de
        viabilidade (ver docs/poc-resumo-por-periodo.md).
"""

import os

SUMMARY_LLM_TIMEOUT_SECONDS: int = int(os.getenv("SUMMARY_LLM_TIMEOUT_SECONDS", "240"))
SUMMARY_MAX_ARTICLES: int = int(os.getenv("SUMMARY_MAX_ARTICLES", "400"))
