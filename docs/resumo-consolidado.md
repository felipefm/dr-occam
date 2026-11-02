# Resumo consolidado de várias pautas

**Status:** implementado na branch `resumo-consolidado` (2026-11-02). A ideia e a discussão que levaram a ele estão em `docs/ideia-resumo-consolidado.md`.

## O que faz

Em `/pautas`, você marca as pautas arquivadas, ou usa os atalhos "Últimos 7 dias" e "Últimos 30 dias", e clica em **Gerar consolidado** (mínimo de 2 pautas). A mesma seleção reabre o consolidado que já existe. A página do consolidado (`/consolidados/<id>`) tem:

- o mesmo quadro de versões do resumo da pauta: formato, várias versões, escolher, apagar, prompt usado e cobertura;
- envio por Telegram, WhatsApp e "Copiar texto", com o resumo escolhido e uma linha por dia ("31/10 — 15 notícias"). Os links das pautas usam o endereço da rede local e não abririam para quem recebe a mensagem, por isso o envio não leva links;
- impressão/PDF com o resumo e, como apêndice, todas as notícias de cada dia com os links;
- "Apagar este consolidado", que apaga só o consolidado e as versões dele, sem tocar nas pautas.

Os consolidados aparecem listados no topo de `/pautas`.

## Entrada da IA (`app/summary/consolidation_service.py`)

- As pautas são agrupadas **por dia** (data de arquivamento no horário de Brasília), em ordem cronológica.
- Pauta **com resumo escolhido**: entra o texto desse resumo e, como referência, os títulos originais das notícias, para a IA conferir fatos.
- Pauta **sem resumo escolhido**: entram as próprias notícias (título + resumo do pipeline), sem gerar nada antes. A página avisa quantas pautas entraram assim.
- Teto de `CONSOLIDATED_MAX_TOTAL_CHARS` (120.000 caracteres, ~30 mil tokens, abaixo do contexto de 51.200 do modelo local). Se passar, os títulos de referência saem; se ainda passar, a resposta é 409 pedindo um período menor.

## Formatos

Os formatos têm um tipo: **diário** (uma pauta) ou **consolidado** (várias pautas). Cada quadro só mostra os formatos do seu tipo, e o `/admin` os mostra em dois grupos. O tipo é escolhido ao criar e não muda depois. De fábrica vem o **Panorama do período**, com o olhar no tempo: o que dominou o período (em que dias), como evoluiu de um dia para o outro com datas, o que surgiu e não se repetiu, conferir fatos pelos títulos e "Também no período:" para os assuntos menores.

## Cobertura por tema

Com "Garantir todas as notícias" ligado no formato, a conferência (`app/summary/coverage.py`, a mesma do resumo diário) usa **temas**: cada parágrafo do resumo escolhido de cada pauta (menos o "Também na pauta:", que o resumo do dia já tratou como assunto menor) e, nas pautas sem resumo, cada notícia. O que faltar entra em "Também no período:" na 2ª rodada. O quadro mostra "Cobertura: X de N temas no texto + Y completados".

**Limite conhecido:** um parágrafo que mistura vários assuntos conta como citado se o resumo cobrir pelo menos um deles; os outros podem ficar de fora.

## Teste (cópia do banco, 2026-11-02)

8 pautas em 2 dias (31/10 e 01/11), 3 com resumo escolhido e 5 pelas notícias, 45 notícias, 20 temas.

- 1ª rodada: 3.889 tokens de entrada, 106s. 2ª rodada: 6 temas, 17s.
- **Evolução:** o texto ligou 31/10 a 01/11 em três assuntos (protestos na França → investigação no dia seguinte; eleições nos EUA; tecnologia → data centers).
- **Detector:** pegou os 4 temas que de fato faltavam (telecom, Texas, Mercosul/IBGE, malware VOLTA), com 2 falsos alarmes (temas já citados, que saem repetidos em "Também no período:").
- **Erros herdados:** a referência pelos títulos **não** impediu que o consolidado repetisse dois erros do resumo diário de 01/11 (o "interesse dos EUA" no caso da Nvidia e o "fardo político para os Estados Unidos após sua fuga"), e o modelo ainda chamou Trump de "ex-presidente". Vale revisar o texto antes de enviar e, se o problema persistir, reforçar o prompt no `/admin`.

## Evoluções possíveis

- Detecção automática de temas recorrentes entre os dias ("apareceu em 4 dos 7 dias"), por embedding, passada à IA como estrutura. Só faz sentido calibrar com algumas semanas de pautas reais.
- Cobertura por frase (em vez de por parágrafo) para os parágrafos que misturam vários assuntos.
