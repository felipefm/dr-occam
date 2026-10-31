"""Configuração da tela inicial ("caixa de entrada" de notícias)."""

import os

# Janela de artigos exibidos na tela inicial. Sem um recorte, todo o acervo
# PROCESSED apareceria como não lido; o que for mais antigo continua
# acessível pela busca (/search).
HOME_WINDOW_HOURS: int = int(os.getenv("HOME_WINDOW_HOURS", "72"))

# Intervalo sem nenhum artigo novo que separa uma varredura da seguinte no
# agrupamento da tela. Não existe um id de execução gravado por artigo, então
# o agrupamento é inferido pelos `created_at`: os artigos de uma mesma
# varredura são gravados em sequência, minutos entre si.
SCAN_GAP_MINUTES: int = int(os.getenv("HOME_SCAN_GAP_MINUTES", "20"))

# Teto de ids aceitos por chamada de marcação em lote (POST /api/articles/read).
MAX_READ_BATCH: int = 5000
