"""Tools de visitantes. Apartamento vem da sessão, nunca do modelo.

autorizar_visitante sempre exige confirmação (Garantia 1).
listar_meus_visitantes não exige. Tools síncronas, nunca levantam.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.tools import FunctionTool

from aurora.dados.dominio import caminho_banco
from aurora.dados.visitantes_repo import (
    gravar_visitante,
    listar_visitantes,
    listar_visitantes_do_apartamento,
)
from aurora.tools.reservas import (
    STATUS_DATA_INVALIDA,
    STATUS_INDISPONIVEL,
    STATUS_SESSAO_INVALIDA,
    _data_valida,
    _sessao_valida,
)

logger = logging.getLogger("aurora.tools")

STATUS_AUTORIZADO = "autorizado"
STATUS_JA_AUTORIZADO = "ja_autorizado"
STATUS_NOME_INVALIDO = "nome_invalido"

_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")
_NOME_MAX = 120


def _registrar_falha_infra(operacao: str) -> None:
    logger.exception("falha de infraestrutura em %s", operacao)


def _nome_normalizado(nome: str) -> str | None:
    if nome is None or _CONTROLE.search(str(nome)):
        return None
    compacto = " ".join(str(nome).split())
    if not compacto or len(compacto) > _NOME_MAX:
        return None
    return compacto


def autorizar_visitante(nome: str, data: str, tool_context: object) -> dict[str, Any]:
    """Autoriza visitante do apartamento da sessão. Sempre com confirmação."""
    try:
        apartamento = _sessao_valida(tool_context)
        if apartamento is None:
            return {"status": STATUS_SESSAO_INVALIDA}
        normalizado = _nome_normalizado(nome)
        if normalizado is None:
            return {"status": STATUS_NOME_INVALIDO}
        if not _data_valida(data):
            return {"status": STATUS_DATA_INVALIDA}
        return gravar_visitante(caminho_banco(), apartamento, normalizado, data)
    except Exception:
        _registrar_falha_infra("autorizar_visitante")
        return {"status": STATUS_INDISPONIVEL}


def listar_meus_visitantes(
    tool_context: object,
) -> list[dict[str, str]] | dict[str, Any]:
    """Lista visitantes do apartamento da sessão.

    Sucesso: lista {nome, data}. Falha de infra: dict com
    status indisponivel_temporariamente e visitantes vazia (nunca levanta).
    """
    try:
        apartamento = _sessao_valida(tool_context)
        if apartamento is None:
            return []
        return listar_visitantes(caminho_banco(), apartamento)
    except Exception:
        _registrar_falha_infra("listar_meus_visitantes")
        return {"status": STATUS_INDISPONIVEL, "visitantes": []}


def criar_tools_visitantes() -> list[FunctionTool]:
    """FunctionTools do ADK 2.9.2. Autorizar sempre confirma; listar não."""
    return [
        FunctionTool(func=autorizar_visitante, require_confirmation=True),
        FunctionTool(func=listar_meus_visitantes),
    ]


__all__ = [
    "STATUS_AUTORIZADO",
    "STATUS_JA_AUTORIZADO",
    "STATUS_NOME_INVALIDO",
    "autorizar_visitante",
    "criar_tools_visitantes",
    "listar_meus_visitantes",
    "listar_visitantes_do_apartamento",
]
