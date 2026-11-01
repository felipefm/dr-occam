"""Formatos do resumo da pauta: os padrões de fábrica e as regras fixas do
sistema.

Os formatos em uso ficam na tabela `summary_format` e são editados no
/admin (nome, prompt, ordem, ativo). Este módulo guarda só:
  - DEFAULT_FORMATS: o conteúdo de fábrica dos formatos que vêm com o
    sistema — semeia a tabela numa instalação nova e serve de base para o
    botão "Restaurar padrão";
  - FIXED_RULES: regras técnicas anexadas a TODO prompt, fora do alcance do
    /admin, porque o envio depende delas (os links são anexados pelo
    sistema; WhatsApp/Telegram não renderizam markdown);
  - clean_output: rede de proteção para quando o modelo desobedece a regra
    de texto simples.
"""

import re

_EDITORIAL_RULES = (
    "Seja estritamente factual, sem adjetivos sensacionalistas, sem opinião "
    "e sem viés político ou ideológico. NÃO invente informações que não "
    "estejam nos textos fornecidos. Escreva em português do Brasil, mesmo "
    "que algum texto esteja em outro idioma."
)

_INTRO = (
    "Você é um editor de notícias neutro e imparcial do agregador Dr. Occam. "
    "Você recebe as notícias que o editor separou para a pauta, cada uma "
    "com título, fonte e texto."
)

DEFAULT_FORMATS: dict[str, dict[str, str]] = {
    "briefing": {
        "label": "Briefing por temas",
        "prompt": _INTRO
        + "\n\n"
        + "Escreva um briefing da pauta. Comece com um parágrafo curto (2 a 3 "
        "frases) com a visão geral do que a pauta cobre. Depois, escreva um "
        "parágrafo por tema, começando com o nome curto do tema seguido de "
        'dois-pontos (por exemplo, "Economia: ..."). Notícias sobre o mesmo '
        "fato ou sobre assuntos relacionados ficam no mesmo parágrafo, sem "
        "repetir informação. Todas as notícias da pauta devem aparecer no "
        "briefing. Separe os parágrafos com uma linha em branco."
        + "\n\n"
        + _EDITORIAL_RULES,
    },
    "panorama": {
        "label": "Panorama",
        "prompt": _INTRO
        + "\n\n"
        + "Escreva um panorama da pauta em texto corrido, de 3 a 4 parágrafos, "
        "separados por uma linha em branco:\n"
        "- Comece pelo assunto principal da pauta (o que reúne mais notícias "
        "ou tem mais peso) e desenvolva-o num parágrafo encadeado, mostrando "
        "como os fatos se relacionam.\n"
        "- Nos parágrafos seguintes, trate dos outros assuntos que tenham "
        "relevância própria, ligando notícias apenas quando houver relação "
        "real entre elas. Nunca invente ligação entre fatos sem relação.\n"
        "- Todas as notícias precisam aparecer. As que não couberem nos "
        "parágrafos entram numa única frase final que comece com "
        '"Também na pauta:", com poucas palavras para cada uma, separadas por '
        "ponto e vírgula.\n"
        "- Não use conectivos vazios para emendar assuntos sem relação, como "
        '"Em outras esferas", "Em outras notícias", "Paralelamente", '
        '"Por fim" ou "Em um contexto mais amplo".'
        + "\n\n"
        + _EDITORIAL_RULES,
    },
}

# Prompts de fábrica anteriores, por formato. Ao subir a aplicação, um
# formato cujo prompt ainda é idêntico a um destes (ou seja, nunca foi
# editado no /admin) é atualizado para o prompt de fábrica atual; um
# formato editado nunca é tocado. Ao mudar um prompt em DEFAULT_FORMATS,
# acrescente o texto antigo aqui.
PREVIOUS_DEFAULT_PROMPTS: dict[str, list[str]] = {
    "panorama": [
        _INTRO
        + "\n\n"
        + "Escreva um panorama da pauta em texto corrido, de 3 a 4 parágrafos, "
        "separados por uma linha em branco:\n"
        "- Comece pelo assunto principal da pauta (o que reúne mais notícias "
        "ou tem mais peso) e desenvolva-o num parágrafo encadeado, mostrando "
        "como os fatos se relacionam.\n"
        "- Nos parágrafos seguintes, trate dos outros assuntos que tenham "
        "relevância própria, ligando notícias apenas quando houver relação "
        "real entre elas. Nunca invente ligação entre fatos sem relação.\n"
        "- Não é preciso citar todas as notícias: assuntos isolados e menores "
        'podem ficar de fora ou entrar numa única frase final que comece com '
        '"Também na pauta:".\n'
        "- Não use conectivos vazios para emendar assuntos sem relação, como "
        '"Em outras esferas", "Em outras notícias", "Paralelamente", '
        '"Por fim" ou "Em um contexto mais amplo".'
        + "\n\n"
        + _EDITORIAL_RULES,
    ],
}

# Rótulos de formatos que existiram antes de os formatos irem para o banco
# — usados só para preencher o nome em versões antigas (ver database.py).
LEGACY_LABELS: dict[str, str] = {"briefing": "Briefing por temas", "corrido": "Texto corrido"}

FIXED_RULES = (
    "Regras do sistema (obrigatórias): NÃO inclua links, URLs nem numeração "
    "das notícias — os links são anexados separadamente pelo sistema. "
    "Responda só com o texto final, em texto simples: sem markdown (sem "
    "asteriscos, sem #, sem marcadores de lista) e sem frases de abertura "
    'como "Aqui está o resumo:".'
)


def system_prompt(editable_prompt: str) -> str:
    return f"{editable_prompt.strip()}\n\n{FIXED_RULES}"


_BOLD_ITALIC = re.compile(r"(\*\*|\*)(?=\S)(.+?)(?<=\S)\1")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*[*•]\s+", re.MULTILINE)


def clean_output(text: str) -> str:
    """Tira a formatação markdown que o modelo às vezes devolve mesmo
    instruído a não usar (`*prompt injection*`, `**Economia:**`, `# Título`),
    para não chegar asterisco no WhatsApp."""
    text = _HEADING.sub("", text)
    text = _BULLET.sub("- ", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    return text.strip()
