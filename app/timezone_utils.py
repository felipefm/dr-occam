"""Fuso horário de exibição do Dr. Occam.

Os timestamps são armazenados em UTC (ver `_utcnow` em models.py), mas
"hoje", "essa semana" etc. só fazem sentido no horário local do usuário.
Compartilhado por main.py (conversão para exibição no feed/admin) e pelos
módulos de busca e resumo por período (interpretação/conversão de datas
relativas em linguagem natural ou de um seletor de janela).
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def local_date_to_utc_naive(d: date, end_of_day: bool) -> datetime:
    """Converte uma data de calendário no fuso de exibição (America/Sao_Paulo)
    para o instante UTC correspondente (início ou fim do dia local), e
    remove o tzinfo antes de retornar.

    O tzinfo precisa ser removido porque `Article.created_at` é uma coluna
    `DateTime` sem timezone: o SQLite não guarda fuso, então os valores já
    salvos ficam naive (ver `_to_display_timezone` em main.py, que trata
    exatamente essa mesma característica na leitura). Comparar um datetime
    aware com uma coluna naive geraria uma comparação lexicográfica
    inconsistente (sufixo "+00:00" vs. nenhum sufixo).
    """
    local_moment = datetime.combine(
        d, time.max if end_of_day else time.min, tzinfo=DISPLAY_TIMEZONE
    )
    return local_moment.astimezone(timezone.utc).replace(tzinfo=None)


def as_aware_utc(dt: datetime) -> datetime:
    """`Article.created_at` volta do SQLite naive (sem tzinfo), embora
    represente sempre um instante UTC (ver `local_date_to_utc_naive` acima
    e `_to_display_timezone` em main.py, que trata a mesma característica).
    Sem isso, o JSON serializaria o datetime sem sufixo de fuso e o
    front-end interpretaria como hora local do navegador, exibindo a data
    errada."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
