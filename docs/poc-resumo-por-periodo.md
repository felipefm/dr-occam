# POC: Resumo de notícias por período

**Status:** proposta, não implementada.
**Origem:** discussão em sessão de 2026-08-13, a partir da pergunta "dá pra eu chegar no campo de busca e pedir 'faça um resumo das notícias dos últimos X dias'?".
**Este documento:** registra a análise de viabilidade/dificuldade feita antes de qualquer implementação, para retomar em outra sessão. Nenhum código foi escrito.

---

## 1. Objetivo

Permitir que o usuário peça uma síntese das notícias de um período (ex.: "resuma os últimos 3 dias", "o que saiu essa semana sobre tecnologia") e receba um texto corrido gerado por IA, em vez de uma lista de artigos individuais.

## 2. Por que isso não é "só mais uma busca"

A busca semântica atual (`/api/search`, ver `app/search/`) é um problema de **recuperação por similaridade**: a query vira um embedding, é comparada por distância de cosseno com os artigos, e o top-K mais parecido é devolvido. Isso funciona porque existe um tópico concreto para comparar.

"Resuma os últimos X dias" não tem um tópico contra o qual comparar similaridade — é um pedido abrangente ("me dê tudo que teve na janela"). Então:

- A etapa de embedding + KNN (`embeddings/service.py`, `search/repository.find_similar_articles`) **não se aplica** aqui. O que se aplica é só o filtro de data que a busca já usa (`search/repository.get_candidate_article_ids`) — mas sem ranquear por relevância, e sim pegando (quase) tudo da janela.
- O resultado não é uma lista de cards, é um texto — muda a etapa de composição da resposta e a forma de exibição no front-end.
- A extração de intenção de data já existe e é reaproveitável (`search/intent_service.extract_date_intent`) — essa parte do pipeline atual serve tanto para busca quanto para resumo.

## 3. Volume real de dados (levantado em 2026-08-13)

Consulta direta em `app/data/occam.db`:

| Janela | Artigos `PROCESSED` |
|---|---|
| hoje | 192 |
| últimos 7 dias | 610 |
| últimos 14 dias | 2.108 |
| últimos 30 dias | ~5.000 (é praticamente a base inteira — o pipeline só roda desde 2026-07-24) |

- `ai_summary` médio: ~630 caracteres (~150-160 tokens), máximo observado 1.592 caracteres.
- `ai_title` médio: ~85 caracteres.

**Implicação direta:** "resuma os últimos 7 dias" significa reunir ~610 resumos já existentes, algo como **100 mil+ tokens** — não cabe num prompt único para a maioria dos modelos locais. Mesmo "resuma hoje" (192 artigos, ~35 mil tokens) já é pesado. Um prompt único só é viável para janelas bem curtas (ordem de 1 dia, dependendo do context window do modelo configurado).

## 4. Restrições do ambiente que pesam na decisão

- `LLM_MODEL_1` (chat) e `EMBEDDING_MODEL` apontam para o **mesmo servidor LM Studio** (`http://192.168.0.2:1234/v1`) — chat e embedding já competem pelo mesmo hardware de inferência.
- Timeouts generosos e já configurados (`LLM_TIMEOUT_SECONDS`, `INTENT_LLM_TIMEOUT_SECONDS`, `EMBEDDING_TIMEOUT_SECONDS` = 240s cada) — pensados para dar folga a um modelo local mais lento, mas significam que qualquer nova chamada de IA nessa mesma máquina pode legitimamente demorar minutos.
- Já houve um incidente de sobrecarga do host inteiro (memória/CPU no talo, derrubando containers não relacionados como Plex) disparado por execução concorrente do pipeline — resolvido com trava de execução (`pipeline_state.py`) e limites de `mem_limit`/`cpus` no `docker-compose.yml`. Qualquer funcionalidade nova que dispare chamadas pesadas de LLM deve ser pensada com esse histórico em mente: idealmente sem concorrência descontrolada e sem prompts gigantes de uma vez só.

## 5. Escopo recomendado para o POC

A recomendação é **não tentar suportar todas as janelas de uma vez**, e tratar isso como uma funcionalidade **separada** da busca (endpoint e UI próprios), não uma extensão do mesmo campo de texto.

### Dentro do escopo do POC

- Endpoint dedicado (proposta de nome: `GET /api/summary` ou similar — a decidir na implementação), que recebe um período (não uma query livre em linguagem natural, para simplificar o POC — ex.: um seletor de "hoje / últimos 3 dias" na UI, em vez de tentar interpretar frase livre).
- Reaproveita o filtro de data já existente (`get_candidate_article_ids`), sem a etapa de embedding/KNN.
- Busca os `ai_summary` + `ai_title` dos artigos da janela (dado que já existe, sem custo adicional de IA para gerá-los de novo).
- Um único prompt para o LLM, concatenando os resumos existentes e pedindo uma síntese — **sem** map-reduce e **sem** clustering por tópico nessa primeira versão.
- Teto rígido na janela permitida no POC (recomendação: até 1-2 dias, ajustável depois de ver o tamanho real do prompt gerado e o tempo de resposta do LM Studio na prática).
- Exibição simples: um bloco de texto na tela (pode reaproveitar a página de busca como uma segunda aba/seção, ou uma página nova — a decidir).

### Fora do escopo do POC (evoluções futuras, não bloqueantes)

1. **Map-reduce para janelas maiores** (7/14/30 dias): dividir os artigos em lotes, resumir cada lote, depois resumir os resumos dos lotes. Necessário para qualquer janela além de ~1-2 dias dado o volume medido na seção 3. Mais chamadas sequenciais ao mesmo LLM compartilhado → mais latência e mais um ponto de contenção.
2. **Clustering por assunto antes de resumir**: com ~192 artigos/dia, é provável que várias fontes cubram o mesmo fato. Um resumo "cru" (concatenar tudo) tende a sair repetitivo. Agrupar por tópico (via embedding existente ou categoria do pipeline) antes de sumarizar melhora a qualidade, mas é complexidade adicional.
3. **Cache do resumo gerado**: gerar de novo a cada clique é caro (chamada de LLM grande). Vale cachear por (data de referência + janela), invalidando quando o pipeline processar artigos novos daquele dia.
4. **Unificação com o campo de busca existente**: aceitar "resuma os últimos X dias" digitado no mesmo campo da busca semântica exigiria uma etapa de classificação de intenção (buscar vs. resumir) antes de decidir qual caminho seguir. Vale considerar só depois do POC validar que o resumo em si é útil — misturar os dois fluxos cedo demais aumenta a complexidade sem necessidade.
5. **Filtro por assunto dentro do período** (ex.: "resuma o que saiu sobre tecnologia nos últimos 7 dias") — combinaria filtro de data + filtro semântico antes de resumir. Só faz sentido depois que o resumo simples por período estiver validado.

## 6. Riscos a observar durante o POC

- **Tempo de resposta**: mesmo limitado a 1-2 dias, um prompt com ~35 mil tokens (caso de "hoje") pode ser lento no servidor LLM local atual. Vale medir o tempo real antes de decidir o teto de janela do POC.
- **Contenção com o pipeline**: se o resumo for pedido enquanto o pipeline está rodando (mesmo com a trava de execução concorrente já existente, que impede múltiplas *varreduras* simultâneas, mas não impede a busca/resumo de rodar durante uma varredura), a chamada pode competir pelo mesmo LM Studio e demorar mais. Vale decidir se o resumo deve respeitar `pipeline_state.is_running()` de alguma forma (ex.: avisar o usuário que pode demorar mais, similar ao aviso já implementado na tela de busca).
- **Qualidade do resumo sem clustering**: validar com exemplos reais se o resumo "cru" (sem agrupar por tópico) já é útil o suficiente, ou se a repetição de notícias correlatas compromete o resultado a ponto de já ser necessário o clustering na v1.

## 7. Critérios de sucesso do POC

- O usuário consegue pedir um resumo de uma janela curta (ex.: hoje) e recebe um texto coerente em tempo aceitável (a definir um teto, ex.: menos de 1-2 minutos).
- O resumo cobre os principais assuntos do período sem ser uma lista mecânica de manchetes coladas.
- Nenhum impacto negativo observado no restante do sistema (sem repetir o incidente de sobrecarga do host).

## 8. Perguntas em aberto para a próxima sessão

**Decisões tomadas em 2026-08-13 (sessão de continuação):**

1. **Teto de janela: configurável entre 1-3 dias.** Seletor na UI com opções tipo "hoje / últimos 2 dias / últimos 3 dias" — não fica travado em "hoje".
2. **UI: nova seção/aba dentro de `/search`.** Reaproveita layout, navegação e avisos já existentes (ex.: aviso de pipeline rodando), evitando duplicar estrutura de página.
3. **Medição do tempo de resposta: feita durante a implementação da POC, não antes.** Instrumentar as chamadas ao LM Studio desde a primeira versão funcional e usar os números reais coletados para fechar/ajustar o teto de janela ao final da POC — não é um passo bloqueante que precisa acontecer antes de começar a codar.
4. **Fontes: sim, lista de artigos (título + link) no rodapé do resumo.** Dá rastreabilidade de onde cada informação do texto de síntese veio; o dado já existe, custo de implementação baixo.
