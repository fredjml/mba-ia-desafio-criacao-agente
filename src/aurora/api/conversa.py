"""Serviço de conversa (lógica das rotas E19), sem HTTP e sem FastAPI.

Política D6 (texto novo com pendência): cancela na tabela e segue o
texto. Evidência em api/smoke_conversa.py B4 (Gemini real). Aprovação do
id cancelado → ConfirmacaoInexistente; 0 execuções.

Política de falha (retomada ADK após transição): transiciona primeiro
(impede 2ª aprovação). Se o ADK levantar ValueError/StaleSessionError,
reverte para pendente e devolve o contrato vazio. Motivo: tools são
idempotentes; sem revert a pendência ficaria aprovada sem efeito
(estado preso); com revert o morador reenvia. A exceção do ADK não vaza.
"""

from __future__ import annotations

from typing import Any

import asyncio

from google.adk.errors import StaleSessionError
from google.adk.events import Event
from google.genai import types

from aurora.agentes.principal import criar_agente_principal
from aurora.dados.carregar import carregar_apartamentos
from aurora.dados.pendencias import (
    STATUS_APROVADA,
    STATUS_NEGADA,
    cancelar_pendentes,
    garantir_tabela,
    listar_pendentes,
    registrar,
    reverter_para_pendente,
    transicionar,
)
from aurora.runtime.fabrica import RuntimeMontado, montar_runtime

APPROVAL_NAME = "adk_request_confirmation"
USER_ID = "morador"


class SessaoInexistente(Exception):
    """Sessão ausente no serviço de sessões (E19 → 404)."""


class ConfirmacaoInexistente(Exception):
    """Pendência inexistente, de outra sessão, já respondida ou cancelada (E19 → 409)."""


class ApartamentoInvalido(Exception):
    """Número ausente de dados/apartamentos.json."""


def _apartamentos_validos() -> set[str]:
    return {str(item["numero"]) for item in carregar_apartamentos()}


def _args_dict(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    if hasattr(args, "model_dump"):
        dumped = args.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    try:
        return dict(args)
    except (TypeError, ValueError):
        return {}


def _texto_evento(event: Event) -> str:
    content = event.content
    if content is None or not content.parts:
        return ""
    return " ".join(part.text or "" for part in content.parts)


def _uso_evento(event: Event) -> dict[str, int]:
    meta = getattr(event, "usage_metadata", None)
    if meta is None:
        return {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
    prompt = int(getattr(meta, "prompt_token_count", 0) or 0)
    candidates = int(getattr(meta, "candidates_token_count", 0) or 0)
    total = int(getattr(meta, "total_token_count", 0) or 0)
    if total == 0:
        total = prompt + candidates
    return {"prompt": prompt, "candidates": candidates, "total": total, "chamadas": 1}


def _mensagem_texto(texto: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=texto)])


def _mensagem_confirmacao(call_id: str, confirmado: bool) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=APPROVAL_NAME,
                    response={"confirmed": confirmado},
                )
            )
        ],
    )


def _pendencias_do_evento(events: list[Event]) -> list[dict[str, Any]]:
    encontradas: list[dict[str, Any]] = []
    for event in events:
        for call in event.get_function_calls():
            if call.name != APPROVAL_NAME or not call.id:
                continue
            args = _args_dict(call.args)
            original = args.get("originalFunctionCall") or args.get(
                "original_function_call"
            )
            original_d = _args_dict(original)
            acao = str(original_d.get("name") or "")
            detalhes = _args_dict(original_d.get("args"))
            detalhes.pop("tool_context", None)
            encontradas.append(
                {"id": call.id, "acao": acao, "detalhes": detalhes}
            )
    return encontradas


def criar_servico(
    *,
    session_db: str | None = None,
    modelo: Any = None,
) -> ServicoConversa:
    runtime = montar_runtime(criar_agente_principal(modelo), session_db=session_db)
    return ServicoConversa(runtime)


class ServicoConversa:
    def __init__(self, runtime: RuntimeMontado) -> None:
        self._runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}
        self._user_id = USER_ID
        self.ultimo_uso = {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
        self.ultimo_turno: dict[str, Any] = {}
        garantir_tabela()

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    @property
    def _app_name(self) -> str:
        return self._runtime.app.name

    async def fechar(self) -> None:
        await self._runtime.runner.close()
        await self._runtime.session_service.close()

    async def _obter_sessao(self, session_id: str) -> Any:
        sessao = await self._runtime.session_service.get_session(
            app_name=self._app_name,
            user_id=self._user_id,
            session_id=session_id,
            config=None,
        )
        if sessao is None:
            raise SessaoInexistente(session_id)
        return sessao

    async def _rodar(self, session_id: str, mensagem: types.Content) -> list[Event]:
        events: list[Event] = []
        async for event in self._runtime.runner.run_async(
            user_id=self._user_id,
            session_id=session_id,
            new_message=mensagem,
        ):
            events.append(event)
        return events

    def _anotar_turno(self, events: list[Event]) -> None:
        uso = {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
        authors: list[str] = []
        tools: list[dict[str, Any]] = []
        texts: list[str] = []
        for event in events:
            if event.author and event.author != "user":
                authors.append(event.author)
            trecho = _texto_evento(event)
            if trecho.strip() and event.author != "user":
                texts.append(trecho)
            for call in event.get_function_calls():
                if call.name and call.name not in {APPROVAL_NAME, "transfer_to_agent"}:
                    tools.append(
                        {
                            "name": call.name,
                            "args": _args_dict(call.args),
                            "id": call.id,
                        }
                    )
            parte = _uso_evento(event)
            for chave in ("prompt", "candidates", "total", "chamadas"):
                uso[chave] += parte[chave]
        self.ultimo_uso = uso
        self.ultimo_turno = {
            "authors": authors,
            "tools": tools,
            "texts": texts,
            "text": texts[-1] if texts else "",
        }

    def _registrar_novas(self, session_id: str, events: list[Event]) -> None:
        for item in _pendencias_do_evento(events):
            registrar(session_id, item["id"], item["acao"], item["detalhes"])

    def _contrato(self, session_id: str, resposta: str) -> dict[str, Any]:
        pendentes = listar_pendentes(session_id)
        return {
            "resposta": resposta,
            "confirmacoes_pendentes": [
                {"id": p["id"], "acao": p["acao"], "detalhes": p["detalhes"]}
                for p in pendentes
            ],
        }

    def _resposta_segura(self, session_id: str) -> dict[str, Any]:
        self.ultimo_uso = {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
        self.ultimo_turno = {"authors": [], "tools": [], "texts": [], "text": ""}
        return self._contrato(session_id, "")

    async def criar_sessao(self, apartamento: str) -> str:
        if str(apartamento) not in _apartamentos_validos():
            raise ApartamentoInvalido(apartamento)
        sessao = await self._runtime.session_service.create_session(
            app_name=self._app_name,
            user_id=self._user_id,
            state={"apartamento": str(apartamento)},
        )
        return sessao.id

    async def enviar_mensagem(self, session_id: str, texto: str) -> dict[str, Any]:
        async with self._lock(session_id):
            await self._obter_sessao(session_id)
            # D6: cancela na tabela e segue (sem negação sintética).
            # Evidência B4 (Gemini real, 2026-09-25): pendência aberta +
            # "Quais são as minhas reservas?" → resposta com RSV-1377 da
            # semente, sem exceção; id antigo → ConfirmacaoInexistente.
            cancelar_pendentes(session_id)
            try:
                events = await self._rodar(session_id, _mensagem_texto(texto))
            except (ValueError, StaleSessionError):
                return self._resposta_segura(session_id)
            self._anotar_turno(events)
            self._registrar_novas(session_id, events)
            return self._contrato(session_id, self.ultimo_turno.get("text") or "")

    async def responder_confirmacao(
        self, session_id: str, id: str, confirmado: bool
    ) -> dict[str, Any]:
        async with self._lock(session_id):
            await self._obter_sessao(session_id)
            destino = STATUS_APROVADA if confirmado else STATUS_NEGADA
            if not transicionar(session_id, id, destino):
                raise ConfirmacaoInexistente(id)
            try:
                events = await self._rodar(
                    session_id, _mensagem_confirmacao(id, confirmado)
                )
            except (ValueError, StaleSessionError):
                reverter_para_pendente(session_id, id)
                return self._resposta_segura(session_id)
            self._anotar_turno(events)
            self._registrar_novas(session_id, events)
            return self._contrato(session_id, self.ultimo_turno.get("text") or "")
