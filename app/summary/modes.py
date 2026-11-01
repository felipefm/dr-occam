"""Formatos do resumo da pauta: rótulo exibido na tela e prompt de sistema.

Módulo à parte (sem dependências) para a tela inicial e a impressão
listarem os formatos sem importar o serviço de resumo."""

_COMMON_RULES = (
    "Seja estritamente factual, sem adjetivos sensacionalistas, sem opinião "
    "e sem viés político ou ideológico. NÃO invente informações que não "
    "estejam nos textos fornecidos. NÃO inclua links, URLs nem numeração "
    "das notícias — os links são anexados separadamente pelo sistema. "
    "Escreva em português do Brasil, mesmo que algum texto esteja em outro "
    "idioma. Responda só com o texto, em texto simples (sem markdown, sem "
    "asteriscos, sem marcadores), e sem frases de abertura como "
    '"Aqui está o resumo:".'
)

_INTRO = (
    "Você é um editor de notícias neutro e imparcial do agregador Dr. Occam. "
    "Você recebe as notícias que o editor separou para a pauta, cada uma "
    "com título, fonte e texto. "
)

MODES: dict[str, dict[str, str]] = {
    "briefing": {
        "label": "Briefing por temas",
        "prompt": _INTRO
        + "Escreva um briefing da pauta. Comece com um parágrafo curto (2 a 3 "
        "frases) com a visão geral do que a pauta cobre. Depois, escreva um "
        "parágrafo por tema, começando com o nome curto do tema seguido de "
        'dois-pontos (por exemplo, "Economia: ..."). Notícias sobre o mesmo '
        "fato ou sobre assuntos relacionados ficam no mesmo parágrafo, sem "
        "repetir informação. Todas as notícias da pauta devem aparecer no "
        "briefing. Separe os parágrafos com uma linha em branco. "
        + _COMMON_RULES,
    },
    "corrido": {
        "label": "Texto corrido",
        "prompt": _INTRO
        + "Escreva um texto corrido que sintetize a pauta, sem títulos e sem "
        "rótulos de tema, ligando naturalmente as notícias relacionadas e "
        "evitando repetição. Todas as notícias da pauta devem aparecer no "
        "texto. " + _COMMON_RULES,
    },
}
DEFAULT_MODE = "briefing"


def mode_options() -> list[dict[str, str]]:
    """Formatos para o seletor da tela, na ordem de MODES."""
    return [{"value": key, "label": mode["label"]} for key, mode in MODES.items()]
