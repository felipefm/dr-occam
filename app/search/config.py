"""Configuração de ambiente da busca híbrida.

Variáveis de ambiente:
    INTENT_LLM_TIMEOUT_SECONDS: timeout das chamadas de chat auxiliares da
        busca (extração de filtro de data em intent_service.py e sugestões
        de refinamento em suggestion_service.py) — mesma cascata, mesmo
        tipo de tarefa curta, mesmo timeout (default 10s).
    SEARCH_MIN_RELEVANCE_PERCENTAGE: piso de relevância (0 a 100). Resultados
        abaixo disso são descartados da resposta — evita mostrar artigos
        sem relação nenhuma só porque entraram no top_k (buscas com uma
        única palavra genérica tendem a produzir similaridade de cosseno
        artificialmente parecida entre tudo, sem discriminar bem o que é
        de fato relevante; ver discussão que motivou este piso). Default 65,
        escolhido por observação empírica: buscas vagas produziram ~60-61%
        de "relevância" até pra artigos sem nenhuma relação, enquanto uma
        busca bem formada separou claramente os relevantes (69%+) do resto.
        Ajuste conforme for observando resultados reais do seu acervo.
"""

import os

INTENT_LLM_TIMEOUT_SECONDS: int = int(os.getenv("INTENT_LLM_TIMEOUT_SECONDS", "10"))
MIN_RELEVANCE_PERCENTAGE: float = float(os.getenv("SEARCH_MIN_RELEVANCE_PERCENTAGE", "65"))
