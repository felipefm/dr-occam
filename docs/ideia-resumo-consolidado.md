# Ideia: resumo consolidado de várias pautas

**Status:** implementado em 2026-11-02, ver `docs/resumo-consolidado.md`. Este documento fica como registro da discussão (registrada em 2026-11-01).
**Origem:** depois do resumo por IA da pauta ir para produção (ver `docs/resumo-da-pauta.md`).

## A ideia

Gerar a pauta todo dia e, depois de um período (ex.: uma semana), pedir um **resumo de todas as pautas do período**. A IA usaria **os resumos diários já escolhidos**, e não as notícias, para montar o resumo consolidado.

## Por que é viável (e a POC por período não era)

A POC de resumo por período caiu por volume: um dia já tinha ~350 notícias (~62 mil tokens), acima do contexto do modelo local (`docs/poc-resumo-por-periodo.md`). A saída apontada lá era map-reduce: resumir pedaços e depois resumir os resumos. Esta ideia é esse map-reduce, com a primeira etapa já feita e com curadoria humana:

- **Etapa 1 (já existe):** a pauta diária, escolhida a dedo, com a versão de resumo escolhida.
- **Etapa 2 (nova):** a IA lê só os resumos diários escolhidos. Sete resumos dão ~10–15 mil tokens, o mesmo porte de uma pauta de 24 notícias (~1 min hoje).

## Pontos levantados

1. **Mostrar a evolução, não só resumir os resumos.** O valor novo da visão semanal está no tempo: o que dominou a semana, o que se desenvolveu de um dia para o outro, o que surgiu e sumiu. Cada resumo diário entra com a data, e o prompt pede esse olhar. Os embeddings, já usados na cobertura (`app/summary/coverage.py`), podem detectar temas recorrentes ("apareceu em 4 dos 7 dias") e passar isso à IA.
2. **A cobertura muda de nível.** "Garantir todas as notícias" não serve para mais de 100 notícias. A garantia passaria a ser: todos os dias, e os temas principais de cada dia, aparecem no texto. É a mesma conferência por embedding, comparando com temas em vez de notícias.
3. **Erros passam adiante.** Um erro de um resumo diário (ex.: o do "fardo político" da família Bolsonaro, na pauta de 01/11) é herdado e pode ser reforçado. Proteção possível: enviar também os títulos originais das notícias como referência (barato em tokens).

## Decisões em aberto

- **Período:** semanal fixo, ou seleção livre em `/pautas` (intervalo de datas ou marcar caixas)? Sugestão: intervalo livre, que cobre o semanal e outros recortes.
- **Pauta sem resumo escolhido:** gerar na hora, usar só os títulos, ou avisar e não incluir?
- **Várias pautas no mesmo dia:** juntar por data ou tratar cada pauta separadamente?
- **Links no envio:** todos os links da semana fica enorme. Alternativas: um link por pauta arquivada ("ver as notícias do dia"), ou só as notícias dos temas que dominaram.
- **Formatos:** os formatos do `/admin` ganham um tipo (diário ou consolidado), porque os prompts são de natureza diferente.

## O que já existe e seria reaproveitado

Motor com trava (`summary/engine.py`), versões com escolha, formatos editáveis no `/admin`, conferência por embedding, impressão e envio. O novo seria: tela de seleção das pautas, prompt de evolução, conferência por tema e o vínculo do consolidado com as pautas arquivadas.

## Sugestão antes de implementar

Usar o resumo diário por 1–2 semanas. Os resumos acumulados viram o material de teste do consolidado, e dá para calibrar com dados reais, como foi feito com a cobertura.

## Decisões tomadas (2026-11-02)

- **Período:** seleção livre em `/pautas` (caixas de marcar + atalhos "Últimos 7/30 dias").
- **Pauta sem resumo escolhido:** entra pelas próprias notícias, sem gerar o resumo antes.
- **Várias pautas no mesmo dia:** agrupadas por data (horário de Brasília).
- **Links:** o envio leva o texto e a contagem por dia; o PDF leva todas as notícias com links.
- **Formatos:** tipo diário/consolidado; de fábrica, "Panorama do período".
- **Cobertura:** por tema (parágrafos dos resumos diários e notícias das pautas sem resumo).
- **Erros herdados:** títulos originais enviados como referência.
- **Fica para depois:** detecção automática de temas recorrentes.
