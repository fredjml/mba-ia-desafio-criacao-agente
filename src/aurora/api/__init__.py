"""Camada de serviço da conversa (E18). Sem FastAPI nesta fatia."""

from aurora.api.conversa import (
    ApartamentoInvalido,
    ConfirmacaoInexistente,
    ServicoConversa,
    SessaoInexistente,
    criar_servico,
)

__all__ = [
    "ApartamentoInvalido",
    "ConfirmacaoInexistente",
    "ServicoConversa",
    "SessaoInexistente",
    "criar_servico",
]
