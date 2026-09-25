"""Nome do modelo e carregamento mínimo do arquivo de ambiente.

Não imprime, não loga e não devolve o valor de GOOGLE_API_KEY.
Não corre no import: só ``carregar_ambiente()`` e ``nome_modelo()``.
"""

from __future__ import annotations

import os
from pathlib import Path

# ADK 2.9.2 (LlmAgent.DEFAULT_MODEL) e a linha Flash barata da Gemini API.
MODELO_PADRAO = "gemini-3.5-flash"
VAR_MODELO = "AURORA_MODEL"
VAR_CHAVE = "GOOGLE_API_KEY"


def _raiz_projeto() -> Path:
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "pyproject.toml").is_file():
                return candidate
    return Path(__file__).resolve().parents[3]


def nome_modelo() -> str:
    """Lê AURORA_MODEL; se ausente, devolve o Flash padrão verificado."""
    valor = os.environ.get(VAR_MODELO, "").strip()
    return valor or MODELO_PADRAO


def carregar_ambiente() -> None:
    """Lê o .env da raiz (CHAVE=VALOR). Não sobrescreve variável já definida."""
    caminho = _raiz_projeto() / ".env"
    if not caminho.is_file():
        raise RuntimeError(
            "arquivo de ambiente ausente na raiz do projeto; "
            f"{VAR_CHAVE} não pode ser carregada"
        )
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        compacta = linha.strip()
        if not compacta or compacta.startswith("#"):
            continue
        if "=" not in compacta:
            continue
        chave, _, bruto = compacta.partition("=")
        chave = chave.strip()
        if not chave:
            continue
        valor = bruto.strip()
        quoted = len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in {"'", '"'}
        if quoted:
            valor = valor[1:-1]
        elif "#" in valor:
            valor = valor.split("#", 1)[0].rstrip()
        if chave not in os.environ:
            os.environ[chave] = valor
    if not os.environ.get(VAR_CHAVE, "").strip():
        raise RuntimeError(f"{VAR_CHAVE} ausente no ambiente")
