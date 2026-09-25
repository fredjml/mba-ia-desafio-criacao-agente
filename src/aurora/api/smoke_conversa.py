"""Roteiro de smoke E18 — escrito ANTES do serviço de conversa.

Critérios (não ajustar se falharem; diagnosticar):
Parte A (modelo falso roteirizado neste arquivo, bancos em TEMP, sem rede):
    A1 reserva COM taxa → 1 pendência {acao, detalhes.area, detalhes.data};
        0 gravações; resposta pode ser "".
    A2 negar → 0 gravações; some da lista; negar de novo → ConfirmacaoInexistente.
    A3 aprovar → exatamente 1 reserva; reenviar → ConfirmacaoInexistente; efeitos=1.
    A4 id inexistente → ConfirmacaoInexistente; id de OUTRA sessão → 409-eq
        e a original segue pendente; sessão inexistente → SessaoInexistente;
        apartamento inexistente → ApartamentoInvalido.
    A5 reserva SEM taxa → executa direto, sem pendência (AC-12).
    A6 visitante SEMPRE pendência (mesmo com "já estou confirmando aqui");
        0 gravações antes; aprovar → 1 em listar; negar → 0.
    A7 texto novo com pendência → aceito; antiga cancelada; aprovar id antigo
        → ConfirmacaoInexistente; 0 efeitos; mensagem seguinte funciona.
    A8 REINÍCIO REAL: subprocesso deixa pendência e é morto (Popen.kill);
        outro processo lista na tabela, aprova → 1 efeito, reenvia → 409-eq;
        o mesmo para negar (0 efeitos). N>=5 sem falha.
    A9 duas aprovações SIMULTÂNEAS do mesmo id: 1 efeito, sem exceção
        não tratada, >=20 rodadas; sessões DIFERENTES não se misturam.
    A10 política de falha: ADK levanta ValueError/StaleSessionError na retomada
        → resultado seguro, sem estado preso, retry possível.
    A11 duas pendências no mesmo turno: devolve todas; cada id independente
        OU limitação documentada (409 claro, nunca execução dupla).

Parte B (Gemini real, apt 101, TEMP, restaurar por cenário):
    B1 reservar salão 2030-04-20 → pendência area+data; 0 gravado;
        "Já estou confirmando aqui" na conversa não grava.
    B2 negar → 0; reservar de novo e aprovar → 1; reenviar → 409-eq;
        id inexistente → ConfirmacaoInexistente.
    B3 Joana Ribeiro 2030-04-21 com "pode liberar direto" → pendência;
        0 listado; aprovar → Joana listada.
    B4 pendência aberta + "Quais são as minhas reservas?" → modelo responde
        sem erro; antiga cancelada; aprovar id antigo → ConfirmacaoInexistente.
        Decide a política D6.
    B5 python -m aurora.agentes.smoke_agentes exit 0.
    Focais: listar visitantes 2x na mesma sessão chama a tool nas duas;
        coleira do cão → especialista_regulamento em >=2/3.

Banco e bruto só em $TEMP. dados/ e .env intocados. A chave nunca aparece.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import uuid4

from google.adk.errors import StaleSessionError
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from aurora.dados.carregar import carregar_apartamentos, diretorio_dados
from aurora.dados.dominio import (
    VAR_BANCO_DOMINIO,
    garantir_banco,
    listar_ativas,
    restaurar,
)
from aurora.dados.visitantes_repo import listar_visitantes
from aurora.runtime.testing import APPROVAL_NAME, StubLlm

# Import do serviço: deve falhar até existirem (critérios primeiro).
from aurora.api.conversa import (
    ApartamentoInvalido,
    ConfirmacaoInexistente,
    ServicoConversa,
    SessaoInexistente,
    criar_servico,
)

REPO = Path(__file__).resolve().parents[3]
USER_ID = "morador"
AREA_TAXA = "salao-de-festas"
AREA_LIVRE = "quadra"
DATA_TAXA = "2030-04-20"
DATA_LIVRE = "2030-05-10"
DATA_CHURRAS = "2030-04-22"
VISITANTE_NOME = "Joana Ribeiro"
VISITANTE_DATA = "2030-04-21"
CODIGO_ALHEIO = "RSV-4821"
NOME_ALHEIO = "Marina Duarte"
N_A8 = 5
N_A9 = 20
ESPERAS_QUOTA = (2.0, 6.0, 14.0)

_BRUTO: Path | None = None
_SAIDAS: list[str] = []
_CHAMADAS_MODELO = 0
_TOKENS = {"prompt": 0, "candidates": 0, "total": 0}


def _temp_dir() -> Path:
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    path = root / f"aurora-e18-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bruto_write(texto: str) -> None:
    if _BRUTO is None:
        return
    with _BRUTO.open("a", encoding="utf-8") as stream:
        stream.write(texto)
        if not texto.endswith("\n"):
            stream.write("\n")


def _emit(event: str, **payload: Any) -> None:
    linha = json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True)
    _SAIDAS.append(linha)
    print(linha, flush=True)
    _bruto_write(linha)


def _hashes_dados() -> dict[str, str]:
    pasta = diretorio_dados()
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(pasta.iterdir())
        if path.is_file()
    }


def _assert_dados_intocados(antes: dict[str, str]) -> None:
    depois = _hashes_dados()
    if depois != antes:
        raise AssertionError("dados/ foi alterado")


def _assert_sem_sqlite_no_repo() -> None:
    achados = [
        str(path.relative_to(REPO))
        for path in REPO.rglob("*.sqlite3")
        if ".venv" not in path.parts and "spike_" not in path.parts
    ]
    if achados:
        raise AssertionError(f"sqlite3 no repositório: {achados}")
    if (REPO / "var").exists():
        raise AssertionError("var/ criado no repositório")


def _reservas(apartamento: str, area: str | None = None, data: str | None = None) -> list[dict[str, str]]:
    itens = listar_ativas(_dominio(), apartamento)
    if area is not None:
        itens = [i for i in itens if i["area"] == area]
    if data is not None:
        itens = [i for i in itens if i["data"] == data]
    return itens


def _visitantes(apartamento: str, nome: str | None = None) -> list[dict[str, str]]:
    itens = listar_visitantes(_dominio(), apartamento)
    if nome is not None:
        itens = [i for i in itens if i["nome"] == nome]
    return itens


def _dominio() -> Path:
    return Path(os.environ[VAR_BANCO_DOMINIO])


def resumo_confirmacoes(servico: ServicoConversa, session_id: str) -> list[dict[str, Any]]:
    """Leitura da tabela de pendências (orquestrador reutiliza)."""
    del servico
    from aurora.dados.pendencias import listar_todas

    return listar_todas(session_id)


def _pendentes(servico: ServicoConversa, session_id: str) -> list[dict[str, Any]]:
    return [p for p in resumo_confirmacoes(servico, session_id) if p["status"] == "pendente"]


class ModeloRoteirizado(BaseLlm):
    """Raiz transfere; especialista chama a tool do roteiro; pós-confirmação responde."""

    _area: str = PrivateAttr()
    _data: str = PrivateAttr()
    _modo: str = PrivateAttr()
    _seq: int = PrivateAttr()

    def __init__(
        self,
        *,
        area: str = AREA_TAXA,
        data: str = DATA_TAXA,
        modo: str = "reservar",
    ) -> None:
        super().__init__(model="stub-e18-roteiro")
        self._area = area
        self._data = data
        self._modo = modo
        self._seq = 0

    def configurar(
        self,
        *,
        area: str | None = None,
        data: str | None = None,
        modo: str | None = None,
    ) -> None:
        if area is not None:
            self._area = area
        if data is not None:
            self._data = data
        if modo is not None:
            self._modo = modo

    def _proximo_id(self) -> str:
        self._seq += 1
        return f"fc-e18-{self._seq}-{uuid4().hex[:8]}"

    @staticmethod
    def _nomes(request: LlmRequest) -> set[str]:
        return set(request.tools_dict.keys())

    @staticmethod
    def _ultimo_texto(request: LlmRequest) -> str:
        for content in reversed(request.contents):
            if content.role != "user":
                continue
            for part in content.parts or []:
                if part.text:
                    return part.text
        return ""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        respostas = StubLlm.response_names(llm_request)
        if APPROVAL_NAME in respostas:
            yield StubLlm.text("Ação concluída após a confirmação.")
            return
        if {
            "reservar_area",
            "autorizar_visitante",
            "listar_minhas_reservas",
            "listar_meus_visitantes",
            "consultar_regulamento",
        } & respostas:
            yield StubLlm.text("Segue o resultado da consulta.")
            return

        nomes = self._nomes(llm_request)
        texto = self._ultimo_texto(llm_request).lower()

        if "reservar_area" in nomes:
            if self._modo == "duas" or "duas áreas" in texto:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name="reservar_area",
                                    id=self._proximo_id(),
                                    args={"area": AREA_TAXA, "data": DATA_TAXA},
                                )
                            ),
                            types.Part(
                                function_call=types.FunctionCall(
                                    name="reservar_area",
                                    id=self._proximo_id(),
                                    args={"area": "churrasqueira", "data": DATA_CHURRAS},
                                )
                            ),
                        ],
                    )
                )
                return
            if any(marca in texto for marca in ("quais", "liste", "listar", "minhas reservas")):
                yield StubLlm.call("listar_minhas_reservas", self._proximo_id(), {})
                return
            yield StubLlm.call(
                "reservar_area",
                self._proximo_id(),
                {"area": self._area, "data": self._data},
            )
            return

        if "autorizar_visitante" in nomes:
            if any(marca in texto for marca in ("quais", "liste", "listar", "autorizados")):
                yield StubLlm.call("listar_meus_visitantes", self._proximo_id(), {})
                return
            yield StubLlm.call(
                "autorizar_visitante",
                self._proximo_id(),
                {"nome": VISITANTE_NOME, "data": VISITANTE_DATA},
            )
            return

        if "consultar_regulamento" in nomes:
            yield StubLlm.call(
                "consultar_regulamento",
                self._proximo_id(),
                {"pergunta": "animais silêncio"},
            )
            return

        if "transfer_to_agent" in nomes:
            if any(marca in texto for marca in ("visitante", "joana", "libera")):
                destino = "especialista_visitantes"
            elif any(marca in texto for marca in ("cão", "cao", "regulamento", "silêncio")):
                destino = "especialista_regulamento"
            else:
                destino = "especialista_reservas"
            yield StubLlm.call(
                "transfer_to_agent",
                self._proximo_id(),
                {"agent_name": destino},
            )
            return

        yield StubLlm.text("Olá, sou a Aurora.")


def _preparar_bancos(workdir: Path, prefixo: str) -> tuple[Path, Path]:
    session_db = workdir / f"{prefixo}-sessoes.sqlite3"
    domain_db = workdir / f"{prefixo}-dominio.sqlite3"
    os.environ["AURORA_SESSION_DB"] = str(session_db)
    os.environ[VAR_BANCO_DOMINIO] = str(domain_db)
    garantir_banco(domain_db)
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(domain_db)
    restaurar_pendencias(domain_db)
    return session_db, domain_db


async def _servico(
    session_db: Path,
    modelo: BaseLlm | str | None = None,
) -> ServicoConversa:
    return criar_servico(session_db=session_db, modelo=modelo)


async def _fechar(servico: ServicoConversa) -> None:
    fechar = getattr(servico, "fechar", None)
    if callable(fechar):
        await fechar()
        return
    runtime = getattr(servico, "_runtime", None)
    if runtime is None:
        return
    await runtime.runner.close()
    await runtime.session_service.close()


def _contrato_ok(corpo: dict[str, Any]) -> None:
    if set(corpo) < {"resposta", "confirmacoes_pendentes"}:
        raise AssertionError(f"contrato={list(corpo)}")
    if not isinstance(corpo["resposta"], str):
        raise AssertionError("resposta não é str")
    if not isinstance(corpo["confirmacoes_pendentes"], list):
        raise AssertionError("confirmacoes_pendentes não é lista")


def _detalhes(pendencia: dict[str, Any]) -> dict[str, Any]:
    bruto = pendencia.get("detalhes") or {}
    if isinstance(bruto, str):
        return json.loads(bruto)
    return dict(bruto)


async def a1(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a1")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA, modo="reservar")
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, "Quero reservar o salão de festas para 2030-04-20")
        _contrato_ok(corpo)
        pend = corpo["confirmacoes_pendentes"]
        if len(pend) != 1:
            raise AssertionError(f"A1 pendencias={pend}")
        det = _detalhes(pend[0])
        if pend[0].get("acao") != "reservar_area":
            raise AssertionError(f"A1 acao={pend[0]}")
        if det.get("area") != AREA_TAXA or det.get("data") != DATA_TAXA:
            raise AssertionError(f"A1 detalhes={det}")
        if _reservas("101", AREA_TAXA, DATA_TAXA):
            raise AssertionError("A1 gravou antes de confirmar")
        _emit("a1", status="OK", pendencias=1, gravacoes=0)
    finally:
        await _fechar(servico)


async def a2(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a2")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, "Reservar salão 2030-04-20")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        negado = await servico.responder_confirmacao(sid, pid, False)
        _contrato_ok(negado)
        if negado["confirmacoes_pendentes"]:
            raise AssertionError(f"A2 ainda pendente={negado}")
        if _reservas("101", AREA_TAXA, DATA_TAXA):
            raise AssertionError("A2 gravou ao negar")
        try:
            await servico.responder_confirmacao(sid, pid, False)
            raise AssertionError("A2 segunda negação não levantou")
        except ConfirmacaoInexistente:
            pass
        _emit("a2", status="OK")
    finally:
        await _fechar(servico)


async def a3(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a3")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, "Reservar salão 2030-04-20")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        aprovado = await servico.responder_confirmacao(sid, pid, True)
        _contrato_ok(aprovado)
        n = len(_reservas("101", AREA_TAXA, DATA_TAXA))
        if n != 1:
            raise AssertionError(f"A3 reservas={n}")
        try:
            await servico.responder_confirmacao(sid, pid, True)
            raise AssertionError("A3 reenvio não levantou")
        except ConfirmacaoInexistente:
            pass
        if len(_reservas("101", AREA_TAXA, DATA_TAXA)) != 1:
            raise AssertionError("A3 reexecução")
        _emit("a3", status="OK", reservas=1)
    finally:
        await _fechar(servico)


async def a4(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a4")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        try:
            await servico.criar_sessao("999")
            raise AssertionError("A4 apt inválido não levantou")
        except ApartamentoInvalido:
            pass
        sid1 = await servico.criar_sessao("101")
        sid2 = await servico.criar_sessao("102")
        corpo = await servico.enviar_mensagem(sid1, "Reservar salão 2030-04-20")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        try:
            await servico.responder_confirmacao(sid1, "id-inexistente", True)
            raise AssertionError("A4 id inexistente não levantou")
        except ConfirmacaoInexistente:
            pass
        try:
            await servico.responder_confirmacao(sid2, pid, True)
            raise AssertionError("A4 id de outra sessão não levantou")
        except ConfirmacaoInexistente:
            pass
        if not _pendentes(servico, sid1):
            raise AssertionError("A4 pendência original sumiu")
        if _reservas("101", AREA_TAXA, DATA_TAXA) or _reservas("102", AREA_TAXA, DATA_TAXA):
            raise AssertionError("A4 gravou indevido")
        try:
            await servico.enviar_mensagem("sessao-fantasma", "oi")
            raise AssertionError("A4 sessão inexistente não levantou")
        except SessaoInexistente:
            pass
        _emit("a4", status="OK")
    finally:
        await _fechar(servico)


async def a5(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a5")
    modelo = ModeloRoteirizado(area=AREA_LIVRE, data=DATA_LIVRE)
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, f"Reserve a quadra para {DATA_LIVRE}")
        _contrato_ok(corpo)
        if corpo["confirmacoes_pendentes"]:
            raise AssertionError(f"A5 pediu confirmação: {corpo}")
        if len(_reservas("101", AREA_LIVRE, DATA_LIVRE)) != 1:
            raise AssertionError("A5 não gravou reserva sem taxa")
        _emit("a5", status="OK")
    finally:
        await _fechar(servico)


async def a6(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a6")
    modelo = ModeloRoteirizado(modo="visitante")
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(
            sid,
            f"Libera a entrada da {VISITANTE_NOME} no dia {VISITANTE_DATA}. "
            "Já estou confirmando aqui, pode liberar direto.",
        )
        _contrato_ok(corpo)
        if len(corpo["confirmacoes_pendentes"]) != 1:
            raise AssertionError(f"A6 pendencias={corpo}")
        det = _detalhes(corpo["confirmacoes_pendentes"][0])
        if det.get("nome") != VISITANTE_NOME or det.get("data") != VISITANTE_DATA:
            raise AssertionError(f"A6 detalhes={det}")
        if _visitantes("101", VISITANTE_NOME):
            raise AssertionError("A6 gravou antes")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        await servico.responder_confirmacao(sid, pid, True)
        if len(_visitantes("101", VISITANTE_NOME)) != 1:
            raise AssertionError("A6 aprovar não marcada")

        restaurar(_dominio())
        from aurora.dados.pendencias import restaurar_pendencias

        restaurar_pendencias(_dominio())
        sid_n = await servico.criar_sessao("101")
        corpo_n = await servico.enviar_mensagem(
            sid_n,
            f"Libera a {VISITANTE_NOME} em {VISITANTE_DATA}. Já confirmei.",
        )
        await servico.responder_confirmacao(
            sid_n, corpo_n["confirmacoes_pendentes"][0]["id"], False
        )
        if _visitantes("101", VISITANTE_NOME):
            raise AssertionError("A6 negar gravou")
        _emit("a6", status="OK")
    finally:
        await _fechar(servico)


async def a7(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a7")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, "Reservar salão 2030-04-20")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        novo = await servico.enviar_mensagem(sid, "Quais são as minhas reservas?")
        _contrato_ok(novo)
        if not isinstance(novo["resposta"], str):
            raise AssertionError("A7 sem resposta")
        statuses = {p["id"]: p["status"] for p in resumo_confirmacoes(servico, sid)}
        if statuses.get(pid) != "cancelada":
            raise AssertionError(f"A7 status={statuses}")
        try:
            await servico.responder_confirmacao(sid, pid, True)
            raise AssertionError("A7 id cancelado não levantou")
        except ConfirmacaoInexistente:
            pass
        if _reservas("101", AREA_TAXA, DATA_TAXA):
            raise AssertionError("A7 efeito do id antigo")
        depois = await servico.enviar_mensagem(sid, "Liste de novo as minhas reservas")
        _contrato_ok(depois)
        _emit("a7", status="OK", resposta_len=len(novo["resposta"]))
    finally:
        await _fechar(servico)


def _escrever_estado(path: Path, dados: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _ler_estado(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def _a8_filho_pendente(state_path: Path) -> None:
    state = _ler_estado(state_path)
    os.environ["AURORA_SESSION_DB"] = state["session_db"]
    os.environ[VAR_BANCO_DOMINIO] = state["domain_db"]
    garantir_banco(state["domain_db"])
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=state["data"])
    servico = await _servico(Path(state["session_db"]), modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, f"Reservar salão {state['data']}")
        pid = corpo["confirmacoes_pendentes"][0]["id"]
        state.update({"session_id": sid, "confirmation_id": pid})
        _escrever_estado(state_path, state)
        print("A8_READY", flush=True)
        time.sleep(3600)
    finally:
        await _fechar(servico)


async def _a8_uma(workdir: Path, indice: int, confirmar: bool) -> None:
    from aurora.dados.pendencias import restaurar_pendencias

    data = f"2031-01-{indice:02d}" if confirmar else f"2031-02-{indice:02d}"
    session_db = workdir / f"a8-{indice}-{'ok' if confirmar else 'no'}-s.sqlite3"
    domain_db = workdir / f"a8-{indice}-{'ok' if confirmar else 'no'}-d.sqlite3"
    os.environ["AURORA_SESSION_DB"] = str(session_db)
    os.environ[VAR_BANCO_DOMINIO] = str(domain_db)
    garantir_banco(domain_db)
    restaurar(domain_db)
    restaurar_pendencias(domain_db)
    state_path = workdir / f"a8-{indice}-{'ok' if confirmar else 'no'}.json"
    _escrever_estado(
        state_path,
        {
            "session_db": str(session_db),
            "domain_db": str(domain_db),
            "data": data,
        },
    )
    comando = [
        sys.executable,
        "-u",
        "-m",
        "aurora.api.smoke_conversa",
        "--mode",
        "a8_pendente",
        "--state",
        str(state_path),
    ]
    proc = subprocess.Popen(
        comando,
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if state_path.exists():
                atual = _ler_estado(state_path)
                if atual.get("confirmation_id"):
                    break
            if proc.poll() is not None:
                saida = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"A8 filho saiu cedo: {saida}")
            time.sleep(0.05)
        else:
            raise AssertionError("A8 timeout aguardando pendência")
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise

    servico = await _servico(session_db, ModeloRoteirizado(area=AREA_TAXA, data=data))
    try:
        state = _ler_estado(state_path)
        sid = state["session_id"]
        pid = state["confirmation_id"]
        lista = [p for p in resumo_confirmacoes(servico, sid) if p["status"] == "pendente"]
        if not any(p["id"] == pid for p in lista):
            raise AssertionError(f"A8 pendência não sobreviveu: {lista}")
        await servico.responder_confirmacao(sid, pid, confirmar)
        n = len(_reservas("101", AREA_TAXA, data))
        esperado = 1 if confirmar else 0
        if n != esperado:
            raise AssertionError(f"A8 efeitos={n} esperado={esperado}")
        try:
            await servico.responder_confirmacao(sid, pid, confirmar)
            raise AssertionError("A8 reenvio não levantou")
        except ConfirmacaoInexistente:
            pass
        if len(_reservas("101", AREA_TAXA, data)) != esperado:
            raise AssertionError("A8 reexecução após reinício")
    finally:
        await _fechar(servico)


async def a8(workdir: Path) -> None:
    for i in range(1, N_A8 + 1):
        await _a8_uma(workdir, i, True)
        await _a8_uma(workdir, i, False)
        _emit("a8_trial", indice=i, status="OK")
    _emit("a8", status="OK", n=N_A8)


async def a9(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a9")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        for rodada in range(N_A9):
            restaurar(_dominio())
            from aurora.dados.pendencias import restaurar_pendencias

            restaurar_pendencias(_dominio())
            data = f"2032-03-{(rodada % 28) + 1:02d}"
            modelo.configurar(area=AREA_TAXA, data=data)
            sid = await servico.criar_sessao("101")
            corpo = await servico.enviar_mensagem(sid, f"Reservar salão {data}")
            pid = corpo["confirmacoes_pendentes"][0]["id"]
            resultados = await asyncio.gather(
                servico.responder_confirmacao(sid, pid, True),
                servico.responder_confirmacao(sid, pid, True),
                return_exceptions=True,
            )
            ok = sum(1 for r in resultados if isinstance(r, dict))
            conflitos = sum(1 for r in resultados if isinstance(r, ConfirmacaoInexistente))
            outros = [
                r
                for r in resultados
                if not isinstance(r, (dict, ConfirmacaoInexistente))
            ]
            if outros:
                raise AssertionError(f"A9 exceção não tratada: {outros[0]!r}")
            if ok != 1 or conflitos != 1:
                raise AssertionError(f"A9 ok={ok} conflitos={conflitos}")
            if len(_reservas("101", AREA_TAXA, data)) != 1:
                raise AssertionError("A9 efeitos != 1")
        _emit("a9_mesmo_id", status="OK", n=N_A9)

        restaurar(_dominio())
        from aurora.dados.pendencias import restaurar_pendencias

        restaurar_pendencias(_dominio())
        modelo.configurar(area=AREA_TAXA, data="2032-06-01")
        sid_a = await servico.criar_sessao("101")
        corpo_a = await servico.enviar_mensagem(sid_a, "Reservar salão 2032-06-01")
        modelo.configurar(area=AREA_TAXA, data="2032-06-02")
        sid_b = await servico.criar_sessao("102")
        corpo_b = await servico.enviar_mensagem(sid_b, "Reservar salão 2032-06-02")
        t0 = time.perf_counter()
        ra, rb = await asyncio.gather(
            servico.responder_confirmacao(
                sid_a, corpo_a["confirmacoes_pendentes"][0]["id"], True
            ),
            servico.responder_confirmacao(
                sid_b, corpo_b["confirmacoes_pendentes"][0]["id"], True
            ),
        )
        elapsed = time.perf_counter() - t0
        _contrato_ok(ra)
        _contrato_ok(rb)
        if len(_reservas("101", AREA_TAXA, "2032-06-01")) != 1:
            raise AssertionError("A9 sessão A misturou")
        if len(_reservas("102", AREA_TAXA, "2032-06-02")) != 1:
            raise AssertionError("A9 sessão B misturou")
        if _reservas("101", AREA_TAXA, "2032-06-02") or _reservas("102", AREA_TAXA, "2032-06-01"):
            raise AssertionError("A9 cruzou apartamentos")
        _emit("a9_sessoes", status="OK", elapsed_s=round(elapsed, 4))
    finally:
        await _fechar(servico)


async def a10(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a10")
    modelo = ModeloRoteirizado(area=AREA_TAXA, data=DATA_TAXA)
    servico = await _servico(session_db, modelo)
    try:
        original = servico._runtime.runner.run_async

        async def _falha_value(*args: Any, **kwargs: Any):
            del args, kwargs
            raise ValueError("simulado-e18")
            yield  # noqa: E501 — torna o objeto async gen mesmo após raise

        async def _falha_stale(*args: Any, **kwargs: Any):
            del args, kwargs
            raise StaleSessionError("simulado-e18")
            yield

        for factory, nome in ((_falha_value, "ValueError"), (_falha_stale, "StaleSessionError")):
            restaurar(_dominio())
            from aurora.dados.pendencias import restaurar_pendencias

            restaurar_pendencias(_dominio())
            data = "2033-01-10" if nome == "ValueError" else "2033-01-11"
            modelo.configurar(data=data)
            sid = await servico.criar_sessao("101")
            corpo = await servico.enviar_mensagem(sid, f"Reservar salão {data}")
            pid = corpo["confirmacoes_pendentes"][0]["id"]
            servico._runtime.runner.run_async = factory
            try:
                resultado = await servico.responder_confirmacao(sid, pid, True)
            except (ValueError, StaleSessionError) as exc:
                raise AssertionError(f"A10 vazou {type(exc).__name__}") from exc
            finally:
                servico._runtime.runner.run_async = original
            _contrato_ok(resultado)
            statuses = {p["id"]: p["status"] for p in resumo_confirmacoes(servico, sid)}
            if statuses.get(pid) != "pendente":
                raise AssertionError(f"A10 estado preso status={statuses}")
            if _reservas("101", AREA_TAXA, data):
                raise AssertionError("A10 gravou na falha")
            retry = await servico.responder_confirmacao(sid, pid, True)
            _contrato_ok(retry)
            if len(_reservas("101", AREA_TAXA, data)) != 1:
                raise AssertionError("A10 retry sem efeito")
            _emit("a10_caso", erro=nome, status="OK")
        _emit("a10", status="OK")
    finally:
        await _fechar(servico)


async def a11(workdir: Path) -> None:
    session_db, _domain = _preparar_bancos(workdir, "a11")
    modelo = ModeloRoteirizado(modo="duas")
    servico = await _servico(session_db, modelo)
    try:
        sid = await servico.criar_sessao("101")
        corpo = await servico.enviar_mensagem(sid, "Quero reservar duas áreas com taxa")
        _contrato_ok(corpo)
        pend = corpo["confirmacoes_pendentes"]
        _emit("a11_observado", n=len(pend), ids=[p.get("id") for p in pend])
        if len(pend) >= 2:
            p1, p2 = pend[0], pend[1]
            await servico.responder_confirmacao(sid, p1["id"], True)
            n1 = len(_reservas("101"))
            resto = [p for p in _pendentes(servico, sid) if p["id"] == p2["id"]]
            if not resto:
                raise AssertionError("A11 segunda pendência sumiu após a primeira")
            await servico.responder_confirmacao(sid, p2["id"], True)
            if len(_reservas("101", AREA_TAXA, DATA_TAXA)) != 1:
                raise AssertionError("A11 salão != 1")
            if len(_reservas("101", "churrasqueira", DATA_CHURRAS)) != 1:
                raise AssertionError("A11 churrasqueira != 1")
            try:
                await servico.responder_confirmacao(sid, p1["id"], True)
                raise AssertionError("A11 reenvio p1 não levantou")
            except ConfirmacaoInexistente:
                pass
            _emit("a11", status="OK", modo="independentes", n=len(pend))
            return
        if len(pend) == 1:
            pid = pend[0]["id"]
            await servico.responder_confirmacao(sid, pid, True)
            efeitos = len(_reservas("101", AREA_TAXA, DATA_TAXA)) + len(
                _reservas("101", "churrasqueira", DATA_CHURRAS)
            )
            if efeitos > 1:
                raise AssertionError(f"A11 execução dupla efeitos={efeitos}")
            try:
                await servico.responder_confirmacao(sid, pid, True)
            except ConfirmacaoInexistente:
                pass
            _emit(
                "a11",
                status="OK",
                modo="limitacao_adk_uma_pendencia",
                n=1,
                efeitos=efeitos,
            )
            return
        raise AssertionError("A11 nenhuma pendência no turno duplo")
    finally:
        await _fechar(servico)


async def parte_a(workdir: Path) -> None:
    await a1(workdir)
    await a2(workdir)
    await a3(workdir)
    await a4(workdir)
    await a5(workdir)
    await a6(workdir)
    await a7(workdir)
    await a8(workdir)
    await a9(workdir)
    await a10(workdir)
    await a11(workdir)
    _emit("parte_a", status="PASS")


def _somar_uso(servico: ServicoConversa) -> None:
    global _CHAMADAS_MODELO
    uso = getattr(servico, "ultimo_uso", None)
    if not isinstance(uso, dict):
        return
    _CHAMADAS_MODELO += int(uso.get("chamadas") or 0)
    for chave in ("prompt", "candidates", "total"):
        _TOKENS[chave] += int(uso.get(chave) or 0)


def _contem_vazamento(*partes: Any) -> bool:
    blob = json.dumps(partes, ensure_ascii=False)
    return CODIGO_ALHEIO in blob or NOME_ALHEIO in blob


def _eh_quota(exc: BaseException) -> bool:
    texto = str(exc).lower()
    nome = type(exc).__name__.lower()
    return any(
        marca in texto or marca in nome
        for marca in ("429", "resource_exhausted", "quota", "rate limit", "ratelimit")
    )


async def _com_quota(operacao: Callable[[], Any]) -> Any:
    ultimo: BaseException | None = None
    for tentativa, espera in enumerate((0.0, *ESPERAS_QUOTA)):
        if espera:
            await asyncio.sleep(espera)
        try:
            return await operacao()
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if _eh_quota(exc) and tentativa < 3:
                _emit("retry_quota", tentativa=tentativa + 1)
                continue
            raise
    raise RuntimeError("cota do Gemini persistiu") from ultimo


async def _b_servico(workdir: Path) -> ServicoConversa:
    from aurora.agentes.modelo import carregar_ambiente, nome_modelo

    carregar_ambiente()
    session_db = workdir / "b-sessoes.sqlite3"
    domain_db = workdir / "b-dominio.sqlite3"
    os.environ["AURORA_SESSION_DB"] = str(session_db)
    os.environ[VAR_BANCO_DOMINIO] = str(domain_db)
    garantir_banco(domain_db)
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(domain_db)
    restaurar_pendencias(domain_db)
    return await _servico(session_db, nome_modelo())


async def b1(servico: ServicoConversa) -> None:
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(_dominio())
    restaurar_pendencias(_dominio())
    sid = await servico.criar_sessao("101")
    corpo = await _com_quota(
        lambda: servico.enviar_mensagem(
            sid, "Quero reservar o salão de festas para 2030-04-20"
        )
    )
    _somar_uso(servico)
    _contrato_ok(corpo)
    if _contem_vazamento(corpo):
        raise AssertionError("B1 vazamento")
    if not corpo["confirmacoes_pendentes"]:
        raise AssertionError(f"B1 sem pendência: {corpo}")
    det = _detalhes(corpo["confirmacoes_pendentes"][0])
    if det.get("area") != AREA_TAXA or str(det.get("data")) != DATA_TAXA:
        raise AssertionError(f"B1 detalhes={det}")
    if _reservas("101", AREA_TAXA, DATA_TAXA):
        raise AssertionError("B1 gravou")
    pid = corpo["confirmacoes_pendentes"][0]["id"]
    chat = await _com_quota(
        lambda: servico.enviar_mensagem(sid, "Já estou confirmando aqui")
    )
    _somar_uso(servico)
    if _reservas("101", AREA_TAXA, DATA_TAXA):
        raise AssertionError("B1 conversa gravou")
    _emit(
        "b1",
        status="OK",
        pendencias_depois=len(chat["confirmacoes_pendentes"]),
        id_antigo=pid,
    )


async def b2(servico: ServicoConversa) -> None:
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(_dominio())
    restaurar_pendencias(_dominio())
    sid = await servico.criar_sessao("101")
    corpo = await _com_quota(
        lambda: servico.enviar_mensagem(
            sid, "Quero reservar o salão de festas para 2030-04-20"
        )
    )
    _somar_uso(servico)
    pid = corpo["confirmacoes_pendentes"][0]["id"]
    await _com_quota(lambda: servico.responder_confirmacao(sid, pid, False))
    _somar_uso(servico)
    if _reservas("101", AREA_TAXA, DATA_TAXA):
        raise AssertionError("B2 negar gravou")
    corpo2 = await _com_quota(
        lambda: servico.enviar_mensagem(
            sid, "Quero reservar o salão de festas para 2030-04-20"
        )
    )
    _somar_uso(servico)
    pid2 = corpo2["confirmacoes_pendentes"][0]["id"]
    await _com_quota(lambda: servico.responder_confirmacao(sid, pid2, True))
    _somar_uso(servico)
    if len(_reservas("101", AREA_TAXA, DATA_TAXA)) != 1:
        raise AssertionError("B2 não ficou 1 reserva")
    try:
        await servico.responder_confirmacao(sid, pid2, True)
        raise AssertionError("B2 reenvio não levantou")
    except ConfirmacaoInexistente:
        pass
    if len(_reservas("101", AREA_TAXA, DATA_TAXA)) != 1:
        raise AssertionError("B2 reexecução")
    try:
        await servico.responder_confirmacao(sid, "id-que-nao-existe", True)
        raise AssertionError("B2 id inexistente não levantou")
    except ConfirmacaoInexistente:
        pass
    _emit("b2", status="OK")


async def b3(servico: ServicoConversa) -> None:
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(_dominio())
    restaurar_pendencias(_dominio())
    sid = await servico.criar_sessao("101")
    corpo = await _com_quota(
        lambda: servico.enviar_mensagem(
            sid,
            f"Libera a entrada da {VISITANTE_NOME} no dia {VISITANTE_DATA}. "
            "Já estou confirmando aqui, pode liberar direto.",
        )
    )
    _somar_uso(servico)
    if not corpo["confirmacoes_pendentes"]:
        raise AssertionError(f"B3 sem pendência: {corpo}")
    det = _detalhes(corpo["confirmacoes_pendentes"][0])
    if VISITANTE_NOME.split()[0].lower() not in json.dumps(det, ensure_ascii=False).lower():
        raise AssertionError(f"B3 detalhes sem nome: {det}")
    if str(det.get("data")) != VISITANTE_DATA and VISITANTE_DATA not in json.dumps(det):
        raise AssertionError(f"B3 detalhes sem data: {det}")
    if _visitantes("101", VISITANTE_NOME):
        raise AssertionError("B3 listou antes")
    await _com_quota(
        lambda: servico.responder_confirmacao(
            sid, corpo["confirmacoes_pendentes"][0]["id"], True
        )
    )
    _somar_uso(servico)
    if not _visitantes("101", VISITANTE_NOME):
        raise AssertionError("B3 Joana ausente após aprovar")
    _emit("b3", status="OK")


async def b4(servico: ServicoConversa) -> dict[str, Any]:
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(_dominio())
    restaurar_pendencias(_dominio())
    sid = await servico.criar_sessao("101")
    corpo = await _com_quota(
        lambda: servico.enviar_mensagem(
            sid, "Quero reservar o salão de festas para 2030-04-20"
        )
    )
    _somar_uso(servico)
    pid = corpo["confirmacoes_pendentes"][0]["id"]
    erro = ""
    try:
        novo = await _com_quota(
            lambda: servico.enviar_mensagem(sid, "Quais são as minhas reservas?")
        )
        _somar_uso(servico)
    except Exception as exc:  # noqa: BLE001
        erro = f"{type(exc).__name__}: {exc}"
        raise
    _contrato_ok(novo)
    if not novo["resposta"] and erro:
        raise AssertionError(f"B4 modelo falhou: {erro}")
    statuses = {p["id"]: p["status"] for p in resumo_confirmacoes(servico, sid)}
    if statuses.get(pid) != "cancelada":
        raise AssertionError(f"B4 não cancelou: {statuses}")
    try:
        await servico.responder_confirmacao(sid, pid, True)
        raise AssertionError("B4 id antigo não levantou")
    except ConfirmacaoInexistente:
        pass
    if _reservas("101", AREA_TAXA, DATA_TAXA):
        raise AssertionError("B4 efeito do id antigo")
    evidencia = {
        "politica": "tabela",
        "resposta_len": len(novo["resposta"]),
        "resposta_amostra": novo["resposta"][:180],
        "erro": erro,
    }
    _emit("b4", status="OK", **evidencia)
    return evidencia


def _b5_regressao() -> None:
    completed = subprocess.run(
        [sys.executable, "-u", "-m", "aurora.agentes.smoke_agentes", "--parte", "todas"],
        cwd=str(REPO),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    _bruto_write(completed.stdout or "")
    if completed.returncode != 0:
        raise AssertionError(f"B5 smoke_agentes exit={completed.returncode}")
    if completed.stdout:
        for linha in completed.stdout.splitlines():
            if '"event": "parte_a"' in linha or '"event": "parte_b"' in linha or '"event": "smoke_end"' in linha:
                _emit("b5_linha", linha=linha[:400])
    _emit("b5", status="OK", exit=completed.returncode)


async def _focais(servico: ServicoConversa) -> None:
    from aurora.dados.pendencias import restaurar_pendencias

    restaurar(_dominio())
    restaurar_pendencias(_dominio())
    sid = await servico.criar_sessao("101")
    tools_vistas = 0
    for _ in range(2):
        corpo = await _com_quota(
            lambda: servico.enviar_mensagem(sid, "Quais visitantes eu tenho autorizados?")
        )
        _somar_uso(servico)
        dump = json.dumps(corpo, ensure_ascii=False)
        if "listar_meus_visitantes" in dump or corpo.get("resposta"):
            # A tool não aparece no contrato; conferimos pelo histórico via eventos
            # internos se o serviço expor, senão pelo fato de responder sem erro
            # e o orquestrador reexecutar. Contamos via ultimo_turno se existir.
            tools = getattr(servico, "ultimo_turno", {}) or {}
            nomes = [t.get("name") for t in tools.get("tools", [])]
            if "listar_meus_visitantes" in nomes:
                tools_vistas += 1
    if tools_vistas < 2:
        raise AssertionError(f"focal visitantes tools={tools_vistas}")

    regulamento = 0
    for i in range(3):
        sid_c = await servico.criar_sessao("101")
        await _com_quota(
            lambda: servico.enviar_mensagem(
                sid_c, "De que cor é a coleira do meu cão à noite?"
            )
        )
        _somar_uso(servico)
        turno = getattr(servico, "ultimo_turno", {}) or {}
        authors = turno.get("authors") or []
        if "especialista_regulamento" in authors:
            regulamento += 1
        _emit("focal_coleira", tentativa=i + 1, authors=authors)
    if regulamento < 2:
        raise AssertionError(f"focal coleira regulamento={regulamento}/3")
    _emit("focais", status="OK", visitantes=tools_vistas, coleira=regulamento)


async def parte_b(workdir: Path) -> None:
    hashes = _hashes_dados()
    chave = os.environ.get("GOOGLE_API_KEY", "")
    servico = await _b_servico(workdir)
    try:
        await b1(servico)
        await b2(servico)
        await b3(servico)
        evidencia = await b4(servico)
        await _focais(servico)
        _emit("b4_politica", **evidencia)
        if chave:
            blob = "\n".join(_SAIDAS)
            bruto = _BRUTO.read_text(encoding="utf-8") if _BRUTO and _BRUTO.exists() else ""
            if chave in blob or chave in bruto:
                raise AssertionError("valor da chave apareceu nas saídas")
        _assert_dados_intocados(hashes)
    finally:
        await _fechar(servico)
    _b5_regressao()
    _assert_dados_intocados(hashes)
    _emit(
        "parte_b",
        status="PASS",
        chamadas=_CHAMADAS_MODELO,
        tokens=dict(_TOKENS),
    )


async def _async_main(parte: str) -> int:
    global _BRUTO
    workdir = _temp_dir()
    _BRUTO = workdir / "smoke-conversa-bruto.txt"
    _emit("smoke_started", workdir=str(workdir), parte=parte)
    try:
        if parte in {"a", "todas"}:
            await parte_a(workdir)
        if parte in {"b", "todas"}:
            await parte_b(workdir)
        _assert_sem_sqlite_no_repo()
        _emit(
            "smoke_end",
            status="PASS",
            chamadas=_CHAMADAS_MODELO,
            tokens=dict(_TOKENS),
            bruto=str(_BRUTO),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        _bruto_write(traceback.format_exc())
        _emit(
            "smoke_end",
            status="FAIL",
            erro=type(exc).__name__,
            detalhe=str(exc)[:400],
            chamadas=_CHAMADAS_MODELO,
            tokens=dict(_TOKENS),
            bruto=str(_BRUTO),
        )
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parte", choices=("a", "b", "todas"), default="todas")
    parser.add_argument(
        "--mode",
        choices=("orchestrate", "a8_pendente"),
        default="orchestrate",
    )
    parser.add_argument("--state", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "a8_pendente":
        if args.state is None:
            raise SystemExit("--state obrigatório em a8_pendente")
        return asyncio.run(_a8_filho_pendente(args.state)) or 0
    return asyncio.run(_async_main(args.parte))


if __name__ == "__main__":
    sys.exit(main())
