"""Perfis de headers HTTP que imitam um navegador real, usados pelo único
httpx.AsyncClient do pipeline de ingestão (compartilhado entre download de
feed, extração de página e resolução de links do Google News — ver
ingestion.run_ingestion).

Por que um perfil inteiro e não só um User-Agent: WAFs (Cloudflare etc.)
não decidem só pelo User-Agent, comparam o conjunto de headers com o que
um navegador real manda junto (Accept, Accept-Language, Accept-Encoding,
Client Hints Sec-Ch-Ua*) e um UA "de Chrome" desacompanhado desses headers
já é, por si, um sinal de bot. Por isso cada perfil abaixo é um conjunto
coerente (o Firefox, por exemplo, não manda Sec-Ch-Ua — não faz sentido
adicionar esse header a um perfil com UA de Firefox).

Limite conhecido: nada aqui contorna um desafio JS/Turnstile do Cloudflare
(exige executar JavaScript) nem o fingerprint de TLS/JA3 do handshake (o
`ssl` do Python não replica o de um Chrome real). Se o bloqueio for desse
tipo, a saída é trocar a origem da requisição, não o header — ver o caso
do RSSHub: preferir sempre a instância self-hosted do docker-compose
(`http://192.168.0.11:1200/...`) em vez da pública `rsshub.app`, que
aplica rate limit/bloqueio agressivo a tráfego de datacenter
independentemente dos headers enviados.
"""

import random
from dataclasses import dataclass, field

_DEFAULT_ACCEPT_LANGUAGE = "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"


@dataclass(frozen=True)
class BrowserProfile:
    """Conjunto de headers de um navegador real, para uso como
    `headers=` de um httpx.Client/AsyncClient inteiro (não por request)."""

    name: str
    user_agent: str
    extra_headers: dict[str, str] = field(default_factory=dict)

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": _DEFAULT_ACCEPT_LANGUAGE,
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            **self.extra_headers,
        }


# Cada entrada precisa continuar internamente consistente (UA e Client
# Hints do mesmo navegador/versão). Adicionar um perfil novo é só copiar
# um bloco destes com dados de uma versão/OS real — não precisa mexer em
# mais nada, ingestion.py só enxerga `random_profile()`.
_PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        name="chrome-windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_headers={
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    ),
    BrowserProfile(
        name="chrome-macos",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_headers={
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
        },
    ),
    BrowserProfile(
        name="firefox-linux",
        user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
        # Firefox não envia Client Hints (Sec-Ch-Ua*) — de propósito, sem extra_headers.
    ),
    BrowserProfile(
        name="edge-windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
        ),
        extra_headers={
            "Sec-Ch-Ua": '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    ),
]


def random_profile() -> BrowserProfile:
    """Sorteia um perfil para a duração de um AsyncClient inteiro.

    De propósito por-cliente (uma vez por execução do pipeline) e não
    por-request: alternar o perfil a cada requisição para o mesmo host,
    no meio de uma sessão, é em si uma inconsistência que um WAF nota
    (iria parecer que múltiplos "navegadores" compartilham a mesma
    conexão/sequência de cookies)."""
    return random.choice(_PROFILES)
