# Especificação de Negócio: Agregador de Notícias Neutro com IA (Versão Final)

## 1. Visão Geral do Produto

Um sistema autônomo de curadoria de informações que atua como um leitor de feeds avançado. Ele coleta notícias de fontes globais em diversos idiomas, extrai o conteúdo completo, remove vieses ideológicos e narrativas usando Inteligência Artificial (priorizando processamento local com contingência em nuvem), e entrega um resumo conciso, factual e traduzido para o usuário.

## 2. O Problema

Acompanhar o cenário geopolítico e as notícias de países em "hiperfoco" exige consumir múltiplas fontes. O volume é esmagador e a maioria dos textos está carregada de viés e/ou ruído jornalístico. O usuário gasta muito tempo filtrando essas informações. Além disso, o processamento de IA em nuvem pode gerar custos elevados, e o processamento local depende de hardware que não fica disponível em tempo integral (24h/7).

## 3. Público-Alvo

Uso pessoal. Um usuário técnico que deseja consumir informação de alta densidade e baixa fricção, com total controle sobre os custos e a infraestrutura.

## 4. Funcionalidades Principais (Core Features)

* **Gestão de Fontes (Inputs):**
* Capacidade de cadastrar URLs de feeds RSS tradicionais.
* Capacidade de cadastrar URLs de sites específicos que não possuem RSS (exigindo raspagem programada da página inicial).


* **Filtro Pré-LLM (Anti-Ruído):**
* Camada de triagem baseada em palavras-chave negativas (ex: "fofoca", "celebridades") para descartar links irrelevantes logo na entrada, economizando recursos de raspagem e tokens de IA.


* **Motor de Coleta (Scraping):**
* **Motor de Coleta (Scraping Delegado):** A complexidade de burlar defesas anti-bot e renderizar JavaScript será terceirizada para um microsserviço independente (baseado no M.arvel).
* **Estratégia de Sobrevivência (Contingência):** O sistema central (Agregador) deve ser agnóstico à ferramenta de raspagem. Caso o microsserviço de coleta seja abandonado pelo criador original e fique obsoleto frente a novas defesas da web, ele poderá ser substituído por outra solução de mercado em container, sem impacto ou necessidade de refatoração do sistema principal.


* **Agrupamento de Fatos (Deduplicação):**
* Identificação de notícias redundantes cobrindo o mesmo evento em múltiplas fontes. O sistema consolida as URLs relacionadas e envia para a IA processar o fato apenas uma vez.


* **Pipeline de Inteligência Artificial (O "Frank"):**
* **Tradução Universal:** Ingerir textos em japonês, chinês, russo, inglês, etc., e normalizar o processamento.
* **Extração de Fatos:** Identificar qual é o evento central que gerou a notícia.
* **Filtro de Viés:** Remover adjetivos valorativos, jargões ideológicos, militância política e opiniões do autor original.
* **Síntese e Formatação:** Gerar um resumo estruturado e direto ao ponto.


* **Orquestrador de LLMs (Sistema de Cascata / Fallback):**
* **Fila de Prioridade:** O sistema tentará processar as notícias em uma ordem pré-definida:
1. LLM Local (se a máquina estiver ligada e acessível na rede).
2. API Comercial Gratuita 1 (ex: Gemini).
3. API Comercial Gratuita 2 (ex: Grok), e assim sucessivamente.


* **Monitoramento de Limites (Rate Limit):** O sistema deve capturar erros de "Limite de API excedido" (ex: HTTP 429) e transferir o processamento automaticamente para a próxima IA da fila.
* **Extensibilidade:** Cadastro de novas APIs comerciais feito exclusivamente via variáveis de ambiente (ex: `LLM_API_3_NAME`, `LLM_API_3_KEY`, `LLM_API_3_URL`), sem necessidade de alterar o código-fonte.


* **Gatilho de Execução Sob Demanda:**
* Interface ou endpoint simples para disparar a varredura e o processamento de forma manual, no momento em que o usuário desejar.


* **Interface de Consumo (Output) e Rastreabilidade:**
* **Painel Web:** Interface limpa listando as top 10/20 notícias processadas do dia. Cada card deve conter: Título neutro (gerado pela IA), Resumo Factual, Contexto Adicional, links consolidados (se agrupado) e a etiqueta de rastreio de IA (ex: *Processado por: Llama3-Local | Prompt: v1.2-strict*).
* **Saída RSS Nativa:** Geração de um arquivo XML/RSS 2.0 padrão contendo os resumos, permitindo que as notícias limpas sejam consumidas de forma fluida diretamente em leitores RSS nativos de navegadores (como o do Vivaldi).


## 5. Regras de Negócio

* **Neutralidade Estrita:** O sistema não deve emitir opiniões próprias; seu papel é de extração cirúrgica de fatos.
* **Restrição de Volume por Provedor:** Ao utilizar uma API Comercial Gratuita, o sistema aplicará uma regra de limite de processamento (ex: top 10 notícias) para preservar a cota. No LLM Local, não há restrição de volume.
* **Desacoplamento de Credenciais:** Nenhuma chave de API ou IP local pode ser fixada no código. Tudo é gerenciado pelo ambiente (`.env`).
* **Resiliência e Persistência:** Se todas as APIs falharem, o texto extraído recebe o status de "Pendente de Processamento" no banco de dados para ser processado no próximo gatilho.
* **Isolamento e Leveza:** A aplicação deve ser conteinerizada (via Docker), operando com serviços enxutos para garantir execução eficiente em ambientes de hardware limitado, como uma Raspberry Pi gerenciada via CasaOS.