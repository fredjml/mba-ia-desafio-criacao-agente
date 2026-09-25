"""Agentes Aurora: principal + especialistas. Só exporta fábricas."""

from aurora.agentes.modelo import carregar_ambiente, nome_modelo
from aurora.agentes.principal import criar_agente_principal

__all__ = ["carregar_ambiente", "criar_agente_principal", "nome_modelo"]
