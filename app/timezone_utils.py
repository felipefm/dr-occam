"""Fuso horário de exibição do Dr. Occam.

Os timestamps são armazenados em UTC (ver `_utcnow` em models.py), mas
"hoje", "essa semana" etc. só fazem sentido no horário local do usuário.
Compartilhado por main.py (conversão para exibição no feed/admin) e pelo
módulo de busca (interpretação de datas relativas em linguagem natural).
"""

from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")
