"""Fábricas dos três especialistas. Sem estado global; sem efeito no import."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from aurora.agentes.instrucoes import (
    INSTRUCAO_REGULAMENTO,
    INSTRUCAO_RESERVAS,
    INSTRUCAO_VISITANTES,
)
from aurora.tools.regulamento import criar_tools_regulamento
from aurora.tools.reservas import criar_tools_reservas
from aurora.tools.visitantes import criar_tools_visitantes

Modelo = str | BaseLlm

_GERACAO = types.GenerateContentConfig(
    temperature=0.2,
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(initial_delay=1, attempts=2),
    ),
)


def criar_especialista_reservas(modelo: Modelo) -> LlmAgent:
    # MOTIVO: tools com confirmação (taxa) ficam no especialista, não na raiz.
    return LlmAgent(
        name="especialista_reservas",
        description=(
            "Reservas de áreas comuns: disponibilidade, reservar, cancelar "
            "e listar as reservas do morador da sessão."
        ),
        model=modelo,
        instruction=INSTRUCAO_RESERVAS,
        tools=criar_tools_reservas(),
        generate_content_config=_GERACAO,
    )


def criar_especialista_visitantes(modelo: Modelo) -> LlmAgent:
    # MOTIVO: autorizar_visitante sempre confirma; o replay corre aqui (E11).
    return LlmAgent(
        name="especialista_visitantes",
        description=(
            "Autorização e listagem de visitantes do morador da sessão."
        ),
        model=modelo,
        instruction=INSTRUCAO_VISITANTES,
        tools=criar_tools_visitantes(),
        generate_content_config=_GERACAO,
    )


def criar_especialista_regulamento(modelo: Modelo) -> LlmAgent:
    # MOTIVO: consulta pontual; o regulamento não vai para o prompt do agente.
    return LlmAgent(
        name="especialista_regulamento",
        description=(
            "Consulta pontual ao regulamento interno do condomínio."
        ),
        model=modelo,
        instruction=INSTRUCAO_REGULAMENTO,
        tools=criar_tools_regulamento(),
        generate_content_config=_GERACAO,
    )
