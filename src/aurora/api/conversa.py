"""Serviço de conversa (lógica das rotas E19), sem HTTP e sem FastAPI.

Política D6 (texto novo com pendência): cancela na tabela e segue o
texto. Evidência em api/smoke_conversa.py B4 (Gemini real). Aprovação do
id cancelado → ConfirmacaoInexistente; 0 execuções.

Política de falha: pendente → processando antes da retomada (impede 2ª
aprovação) e só então → aprovada/negada. Qualquer falha ou cancelamento
reverte para pendente; ValueError/StaleSessionError preservam o contrato
vazio. Quedas são recuperadas na partida e, após o limite, durante o uso.
"""

from __future__ import annotations

from typing import Any
from weakref import WeakValueDictionary

import asyncio
import re
import unicodedata

from google.adk.errors import StaleSessionError
from google.adk.events import Event
from google.genai import types

from aurora.agentes.principal import criar_agente_principal
from aurora.dados.carregar import carregar_apartamentos
from aurora.dados.pendencias import (
    STATUS_APROVADA,
    STATUS_NEGADA,
    cancelar_pendentes,
    finalizar_processamento,
    garantir_tabela,
    iniciar_processamento,
    listar_pendentes,
    recuperar_processando,
    registrar,
    reverter_para_pendente,
)
from aurora.runtime.fabrica import RuntimeMontado, montar_runtime

APPROVAL_NAME = "adk_request_confirmation"
USER_ID = "morador"
LIMITE_PROCESSANDO_S = 300.0
RESPOSTA_VAZIA = "Não consegui responder agora. Pode repetir o pedido?"
RESPOSTA_OUTRO_APARTAMENTO = (
    "Só posso atender o apartamento desta sessão. Não consulto nem altero dados de outra unidade."
)


class SessaoInexistente(Exception):
    """Sessão ausente no serviço de sessões (E19 → 404)."""


class ConfirmacaoInexistente(Exception):
    """Pendência inexistente, de outra sessão, já respondida ou cancelada (E19 → 409)."""


class ApartamentoInvalido(Exception):
    """Número ausente de dados/apartamentos.json."""


def _apartamentos_validos() -> set[str]:
    return {str(item["numero"]) for item in carregar_apartamentos()}


# O número só conta como apartamento com uma pista antes dele ("apto 302", "do 302"); sem pista
# ("R$ 302,00", "lote 302") a fala segue normal. A Garantia 2 é das tools, não desta guarda.
_PISTA_APARTAMENTO = (
    r"(?:\b(?:apartamento|apto?|apt|ap|unidade|casa|bloco|torre)(?![a-zà-ú])\.?\s*"
    r"|\b(?:do|da|dos|das|no|na|nos|nas|ao|pelo|pela|o|a)\s+)"
)


def _cita_outro_apartamento(texto: str, proprio: str) -> bool:
    """True se o texto cita, com pista de apartamento, o número de uma unidade diferente da sessão."""
    texto = unicodedata.normalize("NFKC", texto)
    for numero in _apartamentos_validos() - {proprio}:
        if re.search(
            rf"{_PISTA_APARTAMENTO}(?:n[º°o]\.?\s*|#)?{re.escape(numero)}(?![\w\-]|[.,]\d)",
            texto,
            re.I,
        ):
            return True
    return False


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
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._user_id = USER_ID
        self.ultimo_uso = {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
        self.ultimo_turno: dict[str, Any] = {}
        garantir_tabela()
        recuperar_processando()

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

    def _resposta_segura(self, session_id: str, resposta: str = "") -> dict[str, Any]:
        self.ultimo_uso = {"prompt": 0, "candidates": 0, "total": 0, "chamadas": 0}
        self.ultimo_turno = {"authors": [], "tools": [], "texts": [], "text": ""}
        return self._contrato(session_id, resposta)

    async def criar_sessao(self, apartamento: str) -> str:
        if str(apartamento) not in _apartamentos_validos():
            raise ApartamentoInvalido(apartamento)
        sessao = await self._runtime.session_service.create_session(
            app_name=self._app_name,
            user_id=self._user_id,
            state={"apartamento": str(apartamento)},
        )
        return sessao.id

    async def eventos(self, session_id: str) -> list[dict[str, Any]]:
        sessao = await self._obter_sessao(session_id)
        return [
            evento.model_dump(mode="json", exclude_none=True)
            for evento in sessao.events
        ]

    async def enviar_mensagem(self, session_id: str, texto: str) -> dict[str, Any]:
        async with self._lock(session_id):
            recuperar_processando(
                session_id, mais_velho_que_s=LIMITE_PROCESSANDO_S
            )
            sessao = await self._obter_sessao(session_id)
            # Fala que cita outro apartamento não chega ao modelo nem entra na sessão:
            # evita que o histórico contamine os pedidos seguintes (recusa indevida).
            if _cita_outro_apartamento(texto, str(sessao.state.get("apartamento") or "")):
                return self._resposta_segura(session_id, RESPOSTA_OUTRO_APARTAMENTO)
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
            if self._turno_vazio(session_id):
                # Sem texto, sem pendência e sem tool executada: refazer não duplica efeito.
                uso_primeira = dict(self.ultimo_uso)
                try:
                    events = await self._rodar(session_id, _mensagem_texto(texto))
                except (ValueError, StaleSessionError):
                    return self._resposta_segura(session_id, RESPOSTA_VAZIA)
                self._anotar_turno(events)
                self._registrar_novas(session_id, events)
                self.ultimo_uso = {k: v + uso_primeira.get(k, 0) for k, v in self.ultimo_uso.items()}
            resposta = self.ultimo_turno.get("text") or ""
            if self._turno_vazio(session_id):
                resposta = RESPOSTA_VAZIA
            return self._contrato(session_id, resposta)

    def _turno_vazio(self, session_id: str) -> bool:
        return (
            not self.ultimo_turno.get("text")
            and not self.ultimo_turno.get("tools")
            and not listar_pendentes(session_id)
        )

    async def responder_confirmacao(
        self, session_id: str, id: str, confirmado: bool
    ) -> dict[str, Any]:
        async with self._lock(session_id):
            recuperar_processando(
                session_id, mais_velho_que_s=LIMITE_PROCESSANDO_S
            )
            await self._obter_sessao(session_id)
            destino = STATUS_APROVADA if confirmado else STATUS_NEGADA
            if not iniciar_processamento(session_id, id, destino):
                raise ConfirmacaoInexistente(id)
            concluido = False
            try:
                try:
                    events = await self._rodar(
                        session_id, _mensagem_confirmacao(id, confirmado)
                    )
                except (ValueError, StaleSessionError):
                    return self._resposta_segura(session_id)
                self._anotar_turno(events)
                self._registrar_novas(session_id, events)
                if not finalizar_processamento(session_id, id):
                    raise RuntimeError("pendência deixou de estar processando")
                concluido = True
            finally:
                # Síncrono de propósito: CancelledError não interrompe a reversão.
                if not concluido:
                    reverter_para_pendente(session_id, id)
            return self._contrato(session_id, self.ultimo_turno.get("text") or "")
