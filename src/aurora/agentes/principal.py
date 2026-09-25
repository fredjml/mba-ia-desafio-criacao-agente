"""Fábrica do agente raiz. Sem estado global; sem efeito no import."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from aurora.agentes.especialistas import (
    criar_especialista_regulamento,
    criar_especialista_reservas,
    criar_especialista_visitantes,
)
from aurora.agentes.instrucoes import INSTRUCAO_PRINCIPAL
from aurora.agentes.modelo import nome_modelo

Modelo = str | BaseLlm

_GERACAO = types.GenerateContentConfig(
    temperature=0.2,
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(initial_delay=1, attempts=2),
    ),
)


def criar_agente_principal(modelo: Modelo | None = None) -> LlmAgent:
    """Monta a raiz e os três especialistas. None usa ``nome_modelo()``."""
    resolvido: Modelo = modelo if modelo is not None else nome_modelo()
    return LlmAgent(
        name="aurora_principal",
        description=(
            "Assistente virtual residencial Aurora: recebe o morador e "
            "encaminha ao especialista de reservas, visitantes ou regulamento."
        ),
        model=resolvido,
        instruction=INSTRUCAO_PRINCIPAL,
        tools=[],
        sub_agents=[
            criar_especialista_reservas(resolvido),
            criar_especialista_visitantes(resolvido),
            criar_especialista_regulamento(resolvido),
        ],
        generate_content_config=_GERACAO,
    )
