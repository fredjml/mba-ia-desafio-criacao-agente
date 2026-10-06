"""Rotas HTTP do contrato (E19) sobre ServicoConversa. Uvicorn: aurora.api.app:app."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from aurora.agentes.modelo import carregar_ambiente
from aurora.api.conversa import (
    ApartamentoInvalido,
    ConfirmacaoInexistente,
    ServicoConversa,
    SessaoInexistente,
    criar_servico,
)
from aurora.dados.visitantes_repo import listar_visitantes_do_apartamento
from aurora.tools.reservas import listar_reservas_do_apartamento


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


def _servico(request: Request) -> ServicoConversa:
    return request.app.state.servico


def criar_app(servico: ServicoConversa | None = None) -> FastAPI:
    """Sem ``servico``, o lifespan carrega o .env e monta o serviço com o Gemini."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        proprio = servico is None
        if proprio:
            carregar_ambiente()
            app.state.servico = criar_servico()
        else:
            app.state.servico = servico
        try:
            yield
        finally:
            if proprio:
                await app.state.servico.fechar()

    app = FastAPI(title="Aurora", lifespan=lifespan)

    @app.post("/sessoes", status_code=201)
    async def criar_sessao(corpo: NovaSessao, request: Request) -> dict[str, str]:
        try:
            session_id = await _servico(request).criar_sessao(corpo.apartamento)
        except ApartamentoInvalido:
            raise HTTPException(status_code=422, detail="apartamento inexistente")
        return {"session_id": session_id}

    @app.post("/sessoes/{session_id}/mensagens")
    async def enviar_mensagem(
        session_id: str, corpo: NovaMensagem, request: Request
    ) -> dict[str, Any]:
        try:
            return await _servico(request).enviar_mensagem(session_id, corpo.texto)
        except SessaoInexistente:
            raise HTTPException(status_code=404, detail="sessão inexistente")

    @app.post("/sessoes/{session_id}/confirmacoes")
    async def responder_confirmacao(
        session_id: str, corpo: RespostaConfirmacao, request: Request
    ) -> dict[str, Any]:
        try:
            return await _servico(request).responder_confirmacao(
                session_id, corpo.id, corpo.confirmado
            )
        except SessaoInexistente:
            raise HTTPException(status_code=404, detail="sessão inexistente")
        except ConfirmacaoInexistente:
            raise HTTPException(status_code=409, detail="confirmação não pendente")

    @app.get("/sessoes/{session_id}/eventos")
    async def listar_eventos(session_id: str, request: Request) -> list[dict[str, Any]]:
        try:
            return await _servico(request).eventos(session_id)
        except SessaoInexistente:
            raise HTTPException(status_code=404, detail="sessão inexistente")

    # sqlite é síncrono: em thread para não bloquear o event loop.
    @app.get("/apartamentos/{numero}/reservas")
    async def reservas_do_apartamento(numero: str) -> list[dict[str, str]]:
        return await asyncio.to_thread(listar_reservas_do_apartamento, numero)

    @app.get("/apartamentos/{numero}/visitantes")
    async def visitantes_do_apartamento(numero: str) -> list[dict[str, str]]:
        return await asyncio.to_thread(listar_visitantes_do_apartamento, numero)

    return app


app = criar_app()
