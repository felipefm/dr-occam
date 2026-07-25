# Dr. Occam

Agregador de notícias neutro. Um sistema autônomo de curadoria que lê feeds de fontes globais, extrai o texto principal das notícias, remove viés ideológico e ruído editorial usando IA, e entrega um resumo factual e conciso — consumível tanto por um painel web simples quanto por qualquer leitor RSS.

## O problema

Acompanhar notícias exige consumir múltiplas fontes, e a maior parte do texto vem carregada de viés e ruído jornalístico. O usuário gasta tempo demais filtrando isso manualmente. O Dr. Occam automatiza essa triagem, com controle total de custo e infraestrutura (uso pessoal, rodando em container, com processamento de IA plugável).

## Arquitetura

O `docker-compose.yml` sobe dois containers:

- **`api-occam`** (`./app`, FastAPI + SQLModel/SQLite): o núcleo do Dr. Occam — orquestra ingestão, processamento por IA e expõe o painel e o feed RSS. A extração de conteúdo das notícias roda in-process via `trafilatura`, sem depender de nenhum microsserviço externo de scraping/renderização.
- **`rsshub`** (imagem `diygod/rsshub`, porta `1200`): instância própria do [RSSHub](https://github.com/DIYgod/RSSHub), projeto open-source que gera feeds RSS para sites que não oferecem um nativamente. Não é consumido automaticamente pelo `api-occam` — serve pra gerar manualmente a URL de um feed (ex.: `http://rsshub:1200/bbc/technology` pra `bbc.com/technology`) que depois é cadastrada como uma fonte `RSS` comum no `/admin`. Ver "Ideia futura: auto-detecção de rota RSSHub" no Roadmap abaixo.

### Pipeline de dados

1. **Ingestão** (`app/ingestion.py`): lê as fontes `active=True` da tabela `source`. Para fontes `RSS`, extrai os links via `feedparser` (junto com o texto de `<content:encoded>`/`<description>` de cada item, guardado como fallback). Aplica o filtro anti-ruído de `NEGATIVE_KEYWORDS` (título/URL) antes de gastar uma extração, deduplica contra URLs já salvas e respeita `max_daily_articles` por fonte. Para cada link novo aprovado, baixa a página com o `httpx.AsyncClient` compartilhado (headers de navegador, retry com backoff exponencial em falhas transitórias como 503) e extrai o texto principal com `trafilatura.extract`; se a página não puder ser baixada/raspada (paywall, bloqueio anti-bot, 401 etc.), cai para o texto já entregue no próprio feed, se ele tiver tamanho mínimo plausível de artigo. O artigo resultante é salvo como `PENDING`. Os artigos de uma mesma fonte são processados **estritamente em sequência** (um por vez, com uma pausa de 2s entre eles) para manter o uso de CPU/RAM baixo.
2. **Deduplicação** *(planejado, ainda não implementado — ver Roadmap)*: agruparia notícias do mesmo evento vindas de fontes diferentes (`cluster_id`) antes do processamento por IA, para não gastar tokens processando o mesmo fato várias vezes.
3. **Processamento por IA** (`app/llm_processor.py`): para cada artigo `PENDING`, trunca o conteúdo em `MAX_CONTENT_LENGTH` caracteres (marcando `is_truncated`) e envia para uma **cascata de modelos** (via `litellm`, ver seção abaixo), pedindo um título e resumo neutros e factuais em JSON. O prompt instrui o modelo a nunca usar aspas duplas dentro de `title`/`summary`; mesmo assim, o parsing da resposta é resiliente: extrai estritamente o bloco entre `{` e `}` (descarta markdown/texto ao redor) e, se o `json.loads()` falhar (tipicamente por aspas internas não escapadas), tenta um fallback por regex específico pro formato `{"title": ..., "summary": ...}` antes de desistir — se nem isso reconhecer o texto, o log de warning inclui a resposta bruta da IA para depuração. Cada tentativa é registrada em `llm_processing_log` (modelo que efetivamente respondeu, versão do prompt, tokens, status). Sucesso → `PROCESSED`; falha em toda a cascata (ou JSON irrecuperável) → soma em `retry_count`, e após 5 tentativas o artigo vira `DEAD`.
4. **Consumo**: `GET /` lista os artigos `PROCESSED` mais recentes em HTML; `GET /feed.xml` gera o mesmo conteúdo como RSS 2.0 válido, pronto para qualquer leitor de feeds; `GET /admin` é o painel de gestão das fontes.

### Painel de administração (`/admin`)

Template Jinja2 (`app/templates/admin.html`) servido pelo próprio FastAPI, sem build step — Tailwind CSS via CDN (`cdn.tailwindcss.com`) para o estilo e JavaScript puro (`fetch`) para o CRUD, sem nenhum framework frontend. A lista inicial é renderizada no servidor; toda ação (adicionar, alternar status, editar limite, excluir) chama a API REST correspondente e atualiza só a linha afetada no DOM, sem recarregar a página. Fontes são sempre criadas como `RSS` (o formulário só pede nome + URL); o campo `source_type` fica de fora do formulário porque a extração de links só é implementada para `RSS`.

O `/admin` também tem um botão **"Executar varredura"** (dispara `POST /trigger-pipeline`) e um botão **"Ver logs"** que abre um modal estilo terminal (preto/verde) com as últimas linhas de log da aplicação, atualizando por polling (`GET /api/logs`, a cada 2s) enquanto o modal estiver aberto — dá pra acompanhar o pipeline rodando sem precisar de `docker logs` no terminal. Os logs ficam num buffer em memória (`collections.deque`, últimas 500 linhas, `app/main.py`); reiniciar o container zera o histórico.

### Cascata de IAs (fallback)

`llm_processor.py` lê modelos numerados do ambiente (`LLM_MODEL_1`, `LLM_MODEL_2`, ...) e monta a cascata nativa de `fallbacks` do `litellm`: a chamada usa o modelo `1` como primário; se ele falhar (conexão recusada, timeout, rate limit etc.), o `litellm` tenta o `2`, depois o `3`, e assim por diante — de forma automática e silenciosa, sem que isso conte como falha de processamento do artigo (só falha de verdade se **todos** os modelos da cascata falharem). Cada modelo pode ter seu próprio `LLM_API_BASE_N`/`LLM_API_KEY_N`, o que permite misturar um LLM local (ex.: LM Studio numa outra máquina da rede, nem sempre ligada) com APIs comerciais na nuvem como fallback. `LLM_TIMEOUT_SECONDS` garante que uma máquina local desligada não trave a cascata por muito tempo antes de cair para o próximo modelo.

O ciclo completo (ingestão → processamento por IA) é disparado sob demanda via `POST /trigger-pipeline`, que roda em background e responde imediatamente.

## Modelo de dados

- **`source`**: fontes cadastradas (nome, URL, tipo `RSS`/`HTML_SCRAPE`, ativa, limite diário de artigos).
- **`article`**: cada notícia coletada — conteúdo original, status (`PENDING`/`PROCESSED`/`DEAD`/`DEDUPLICATED`), título e resumo gerados pela IA, `cluster_id`, contagem de tentativas e flag de truncamento.
- **`llm_processing_log`**: rastreabilidade de cada chamada de IA feita sobre um artigo (provider, versão do prompt, tokens consumidos, status).

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/trigger-pipeline` | Dispara ingestão + processamento por IA como background task. Responde `202` imediatamente. |
| `GET` | `/feed.xml` | Feed RSS 2.0 dos artigos `PROCESSED`, mais recentes primeiro. |
| `GET` | `/` | Painel HTML mínimo para leitura rápida dos últimos artigos processados. |
| `GET` | `/admin` | Painel de administração (Jinja2 + Tailwind via CDN) para gerenciar as fontes. |
| `POST` | `/api/sources` | Cria uma fonte `RSS` nova (`{"name": ..., "url": ...}`). |
| `PATCH` | `/api/sources/{id}/toggle` | Alterna `active` da fonte. |
| `PUT` | `/api/sources/{id}/limit` | Atualiza `max_daily_articles` (`{"max_daily_articles": N}`). |
| `DELETE` | `/api/sources/{id}` | Remove a fonte. |
| `GET` | `/api/logs` | Últimas linhas de log em memória (`{"logs": [...]}`), usado pelo modal de logs do `/admin`. |

## Variáveis de ambiente (`.env`)

| Variável | Descrição |
|---|---|
| `LLM_MODEL_1` | Modelo primário da cascata de IA, no formato aceito pelo `litellm` (ex.: `openai/<nome-do-modelo>` para um servidor OpenAI-compatible como o LM Studio). |
| `LLM_API_BASE_1` | Base URL do modelo primário (ex.: `http://<IP_DA_MAQUINA_NA_REDE>:1234/v1` do LM Studio). Opcional — só necessário para endpoints customizados. |
| `LLM_API_KEY_1` | Chave/token do modelo primário (LM Studio local geralmente aceita qualquer valor, ex.: `lm-studio`). |
| `LLM_MODEL_2` | Modelo de fallback, usado automaticamente se o primário falhar ou der timeout (ex.: `gemini/gemini-1.5-flash`). |
| `LLM_API_KEY_2` | Credencial do modelo de fallback. |
| `LLM_MODEL_N` / `LLM_API_BASE_N` / `LLM_API_KEY_N` | Padrão repetível para adicionar mais modelos à cascata (N = 3, 4, ...), sem alterar código. |
| `MAX_CONTENT_LENGTH` | Tamanho máximo (caracteres) do texto enviado à IA antes de truncar. |
| `NEGATIVE_KEYWORDS` | Palavras-chave (separadas por vírgula) que descartam uma notícia antes da extração. |

## Como rodar

```bash
# 1. Preencha as chaves reais em .env (LLM_MODEL_1/LLM_API_BASE_1, LLM_MODEL_2/LLM_API_KEY_2, etc.)
docker compose up -d --build

# 2. Cadastre fontes pelo painel de admin (ou use o script auxiliar add_source.py como atalho):
# http://localhost:8383/admin

# 3. Dispare o pipeline manualmente (ou clique em "Executar varredura" no /admin)
curl -X POST http://localhost:8383/trigger-pipeline

# 4. Consuma o resultado
# http://localhost:8383/         (painel de leitura)
# http://localhost:8383/feed.xml (RSS)
```

## Estado atual vs. visão do produto

Já implementado: ingestão RSS, filtro anti-ruído, limite diário por fonte, extração de conteúdo in-process via trafilatura (sequencial, sem microsserviço externo), cascata de IAs com fallback automático (local + nuvem), resumo neutro via IA com log de rastreabilidade, retry/DEAD, feed RSS 2.0, painel de leitura HTML, painel de administração de fontes (`/admin`, CRUD completo), gatilho manual assíncrono.

Ainda não implementado (fazem parte da especificação de negócio original, em `negocio.md`):

- **Deduplicação/agrupamento** de notícias do mesmo evento (`cluster_id` existe no schema, mas nunca é preenchido ainda).
- **Monitoramento explícito de rate limit (HTTP 429)** como gatilho de fallback — hoje a cascata do `litellm` já pula de modelo em qualquer exceção (conexão, timeout, rate limit), mas não há tratamento diferenciado por tipo de erro.
- **Tradução explícita** de fontes multi-idioma como etapa dedicada do pipeline.
- **Fontes `HTML_SCRAPE`** — o schema já suporta o tipo, mas nem o `/admin` (o formulário só cria `RSS`) nem a ingestão (que só sabe extrair links de `RSS`) têm esse caminho implementado.
- **Etiqueta de rastreio no painel de leitura** ("Processado por: X | Prompt: vY") — o dado já é gravado em `llm_processing_log`, mas ainda não aparece na UI pública.

### Ideia futura: auto-detecção de rota RSSHub ao cadastrar fonte

Hoje, pra cadastrar um site sem RSS nativo (ex.: `bbc.com/technology`), o usuário precisa descobrir manualmente a rota certa do RSSHub (`docs.rsshub.app`) e colar a URL já traduzida (`http://rsshub:1200/bbc/technology`) no `/admin`. A ideia é automatizar isso: o usuário cola o link original do site e o sistema descobre/sugere a rota RSSHub sozinho.

**Viabilidade — mecanismo existe e é acessível:**

- O RSSHub tem um sistema nativo pra isso, o **Radar**: cada rota pode declarar uma regra mapeando um padrão de URL do site original (`source`) para o template da própria rota (`target`). É o mesmo mecanismo que alimenta a extensão de navegador "RSSHub Radar".
- Qualquer instância do RSSHub expõe essas regras via `GET /api/radar/rules` — no nosso caso, `http://rsshub:1200/api/radar/rules`. Retorna um JSON indexado por domínio, com os padrões `source`/`target` de cada rota conhecida (ex.: `bbc.com` → `source: "/:channel?"`, `target: "/bbc/:channel"`).
- Fluxo proposto: usuário cola `https://www.bbc.com/technology` → backend extrai domínio + path → busca o domínio no JSON de regras (cacheado localmente, refresh periódico) → casa o path contra o padrão `source` e extrai os parâmetros → substitui no `target` → gera a URL RSSHub final → **valida antes de salvar** (faz um GET real na URL gerada e confere se voltou um XML de feed com itens, não só HTTP 200 — a própria página de erro do RSSHub retorna corpo HTML) → salva como `Source(source_type=RSS, url=...)`.

**Limitações a considerar antes de implementar:**

- Cobertura não é universal — só domínios com regra de Radar cadastrada pelos mantenedores do RSSHub aparecem no JSON; um site menor pode ter rota funcional sem regra de Radar (ou nem ter rota nenhuma).
- O casamento `source`/`target` usa sintaxe estilo `path-to-regexp` (parâmetros opcionais, wildcards) — não é regex trivial, precisa de um matcher dedicado (portar/reimplementar em Python, já que o `api-occam` não roda Node).
- Alguns parâmetros de rota não existem na URL visível da página (ex.: a rota `/bbc/topics/:topic` pede um ID que só aparece dentro do HTML/URL de uma página de tópico específica do BBC) — nesses casos a auto-detecção falha e precisa cair pra um fluxo manual de qualquer forma.
- Por isso, qualquer implementação futura precisa de um fallback gracioso ("não encontrei rota automática pra esse domínio, cadastre manualmente") em vez de assumir que todo link terá sucesso.

### Ideia futura: ativar `--reload` do uvicorn (evitar rebuild a cada ajuste)

Hoje, qualquer alteração em código (`.py`) só é refletida no container depois de um ciclo completo `docker compose down` + `docker compose up -d --build` — lento e repetitivo durante desenvolvimento iterativo.

**Causa raiz:** o `docker-compose.yml` já monta `./app:/app` por volume (o próprio comentário no arquivo diz "para hot-reload"), mas o `CMD` do `Dockerfile` nunca foi ajustado pra aproveitar isso — o `uvicorn` sobe sem a flag `--reload`, então o processo dentro do container não percebe mudança nenhuma sozinho.

**Correção proposta:** trocar o `CMD` do `Dockerfile` (`app/Dockerfile:21`) para incluir `--reload`, por exemplo:

```
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Como o código já é bind-mount (não uma cópia estática da imagem), o `uvicorn` passa a detectar sozinho quando um arquivo `.py` muda e reinicia o processo automaticamente (~1s) — sem precisar de nenhum comando manual. Templates Jinja2 (`app/templates/*.html`) já são lidos do disco a cada requisição por padrão, então esses já deveriam refletir mudança só com F5, independente do `--reload`.

**Ressalvas:**

- Precisa de **um rebuild único** pra essa mudança de `CMD` entrar em vigor (depois disso, edits de código não pedem mais rebuild).
- Mudanças em `requirements.txt` (dependência nova) continuam exigindo `docker compose up -d --build` — pacotes instalados ficam "assados" na imagem, o `--reload` não cobre isso.
- É um watcher de arquivos rodando o tempo todo — custo de CPU/RAM desprezível pro uso pessoal/dev, mas foge um pouco do princípio de "leveza" do `negocio.md` pensado pra produção numa Raspberry Pi. Não é recomendado deixar `--reload` ativo num ambiente de produção "real" — só faz sentido enquanto o projeto está em desenvolvimento ativo.
- Não precisa mexer em nada no CasaOS pra isso — o botão de restart/recreate dele já roda `docker compose` por baixo dos panos; resolvendo o `--reload`, o CasaOS nem entra na equação pra esse tipo de ajuste.

## Observação importante

A extração de conteúdo trocou de um microsserviço externo (Browserless, que abria uma instância de Chromium por notícia e chegava a consumir 100% de CPU/RAM com poucas raspagens simultâneas) para `trafilatura`, uma biblioteca leve que baixa e extrai o texto principal do artigo dentro do próprio processo do `api-occam`. Como consequência, o `docker-compose.yml` voltou a ter um único container. Para manter o consumo de recursos baixo, os artigos de uma mesma fonte são raspados **um de cada vez** (`app/ingestion.py`), com uma pausa de `SCRAPE_DELAY_SECONDS` (2s, fixo no código) entre cada extração — isso é deliberadamente conservador; fontes diferentes ainda podem ser processadas em paralelo entre si (`MAX_CONCURRENT_FEEDS`).

`trafilatura` depende só de requisições HTTP simples (sem JavaScript) para baixar a página — sites fortemente dependentes de renderização client-side (SPAs) podem não ter o conteúdo capturado corretamente. Se isso virar um problema recorrente, um serviço de renderização (tipo o Browserless removido) volta a ser necessário para essas fontes específicas.

Bloqueios de origem (ex.: 401 anti-bot/paywall da Reuters) e instabilidade do RSSHub local (ex.: 503 sob rate-limit em rotas como `/bbc/travel`) são tratados em duas camadas, ambas em `app/ingestion.py`: retry com backoff exponencial para status transitórios (`429`/`502`/`503`/`504`) nas requisições HTTP, e uma cadeia de extração (`_EXTRACTORS`) que cai para o texto de `<content:encoded>`/`<description>` do próprio feed quando o `trafilatura` não consegue baixar ou extrair a página. Esse fallback só é aceito acima de `MIN_FALLBACK_CONTENT_LENGTH` caracteres (hoje 80, calibrado para os resumos curtos que a Reuters entrega no feed).

O diretório `./scraper-akita` (Frank Investigator) não é referenciado pelo `docker-compose.yml` — ele fica no repo sem uso pelo Dr. Occam por enquanto.

`DELETE /api/sources/{id}` remove só a linha da fonte — a tabela `source` não tem `ON DELETE CASCADE` (nem SQLite aplica `FOREIGN KEY` por padrão), então artigos já coletados dessa fonte continuam no banco com um `source_id` órfão em vez de serem apagados junto. Isso é intencional (excluir uma fonte não deveria apagar o histórico de notícias já processadas), mas vale saber que o artigo antigo não desaparece do feed/painel só porque a fonte foi removida.
