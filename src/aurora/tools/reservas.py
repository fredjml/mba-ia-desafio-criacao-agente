"""Tools de reservas. Apartamento vem da sessão, nunca do modelo.

As tools são síncronas e nunca levantam exceção para o chamador.
Não aceitam parâmetro de apartamento: leem tool_context.state[CHAVE_APARTAMENTO].
listar_reservas_do_apartamento (repositório E19) propaga falha de infraestrutura.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from google.adk.tools import FunctionTool

from aurora.dados.carregar import carregar_apartamentos, carregar_areas
from aurora.dados.dominio import (
    caminho_banco,
    cancelar_no_banco,
    disponivel,
    gravar_reserva,
    listar_ativas,
)

logger = logging.getLogger("aurora.tools")

CHAVE_APARTAMENTO = "apartamento"

STATUS_RESERVADA = "reservada"
STATUS_JA_RESERVADA = "ja_reservada"
STATUS_RECUSADA = "recusada"
STATUS_AREA_INVALIDA = "area_invalida"
STATUS_DATA_INVALIDA = "data_invalida"
STATUS_SESSAO_INVALIDA = "sessao_invalida"
STATUS_CANCELADA = "cancelada"
STATUS_NAO_ENCONTRADA = "nao_encontrada"
STATUS_JA_CANCELADA = "ja_cancelada"
STATUS_INDISPONIVEL = "indisponivel_temporariamente"

_DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _registrar_falha_infra(operacao: str) -> None:
    logger.exception("falha de infraestrutura em %s", operacao)


def _areas() -> dict[str, Any]:
    return {str(item["id"]): item for item in carregar_areas()}


def _apartamentos() -> set[str]:
    return {str(item["numero"]) for item in carregar_apartamentos()}


def _taxa_area(area: str) -> float | None:
    item = _areas().get(area)
    if item is None:
        return None
    return float(item["taxa"])


def _data_valida(valor: str) -> bool:
    if not valor or not _DATA_RE.match(valor):
        return False
    try:
        date.fromisoformat(valor)
    except ValueError:
        return False
    return True


def _apartamento_da_sessao(tool_context: Any) -> str | None:
    state = getattr(tool_context, "state", None)
    if state is None or not hasattr(state, "get"):
        return None
    valor = state.get(CHAVE_APARTAMENTO)
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _sessao_valida(tool_context: Any) -> str | None:
    apartamento = _apartamento_da_sessao(tool_context)
    if apartamento is None or apartamento not in _apartamentos():
        return None
    return apartamento


def reservar_exige_confirmacao(
    area: str,
    data: str = "",
    tool_context: object = None,
) -> bool:
    """True quando a área gera cobrança (taxa > 0).

    Assinatura alinhada à da tool: o ADK 2.9.2 invoca o callable com os
    argumentos da chamada (function_tool.py:197-205, args_to_call).
    """
    del data, tool_context
    taxa = _taxa_area(area)
    return taxa is not None and taxa > 0


def consultar_disponibilidade(
    area: str,
    data: str,
    tool_context: object,
) -> dict[str, Any]:
    """Consulta se uma área comum está livre numa data. Não revela quem reservou."""
    del tool_context
    try:
        if area not in _areas() or not _data_valida(data):
            return {"area": area, "data": data, "disponivel": False}
        return {
            "area": area,
            "data": data,
            "disponivel": disponivel(caminho_banco(), area, data),
        }
    except Exception:
        _registrar_falha_infra("consultar_disponibilidade")
        return {"status": STATUS_INDISPONIVEL}


def reservar_area(
    area: str,
    data: str,
    tool_context: object,
) -> dict[str, Any]:
    """Reserva a área na data para o apartamento da sessão. Nunca vaza terceiros."""
    try:
        apartamento = _sessao_valida(tool_context)
        if apartamento is None:
            return {"status": STATUS_SESSAO_INVALIDA}
        if area not in _areas():
            return {"status": STATUS_AREA_INVALIDA}
        if not _data_valida(data):
            return {"status": STATUS_DATA_INVALIDA}
        taxa = _taxa_area(area)
        resultado = gravar_reserva(caminho_banco(), apartamento, area, data)
        status = resultado.get("status")
        if status == STATUS_RECUSADA:
            return {"status": STATUS_RECUSADA}
        return {
            "status": status,
            "codigo": resultado["codigo"],
            "area": area,
            "data": data,
            "taxa": taxa,
        }
    except Exception:
        _registrar_falha_infra("reservar_area")
        return {"status": STATUS_INDISPONIVEL}


def cancelar_reserva(codigo: str, tool_context: object) -> dict[str, Any]:
    """Cancela reserva própria (ativa=0). Alheia e inexistente são idênticas."""
    try:
        apartamento = _sessao_valida(tool_context)
        if apartamento is None:
            return {"status": STATUS_NAO_ENCONTRADA}
        return cancelar_no_banco(caminho_banco(), apartamento, codigo)
    except Exception:
        _registrar_falha_infra("cancelar_reserva")
        return {"status": STATUS_INDISPONIVEL}


def listar_minhas_reservas(
    tool_context: object,
) -> list[dict[str, str]] | dict[str, Any]:
    """Lista reservas ativas do apartamento da sessão.

    Sucesso: lista {codigo, area, data}. Falha de infra: dict com
    status indisponivel_temporariamente e reservas vazia (nunca levanta).
    """
    try:
        apartamento = _sessao_valida(tool_context)
        if apartamento is None:
            return []
        return listar_ativas(caminho_banco(), apartamento)
    except Exception:
        _registrar_falha_infra("listar_minhas_reservas")
        return {"status": STATUS_INDISPONIVEL, "reservas": []}


def listar_reservas_do_apartamento(
    apartamento: str,
    db_path: str | None = None,
) -> list[dict[str, str]]:
    """Repositório não-tool (E19): formato exato de GET /apartamentos/{n}/reservas.

    Em falha de infraestrutura (banco inacessível, lock esgotado, erro sqlite)
    LEVANTA a exceção — a rota precisa responder erro de servidor, não lista vazia.
    """
    return listar_ativas(caminho_banco(db_path), str(apartamento))


def criar_tools_reservas() -> list[FunctionTool]:
    """FunctionTools do ADK 2.9.2. Só reservar_area confirma quando taxa > 0."""
    return [
        FunctionTool(func=consultar_disponibilidade),
        FunctionTool(
            func=reservar_area,
            require_confirmation=reservar_exige_confirmacao,
        ),
        FunctionTool(func=cancelar_reserva),
        FunctionTool(func=listar_minhas_reservas),
    ]
