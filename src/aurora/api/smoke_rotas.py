"""Smoke E19 (offline, StubLlm, bancos em TEMP): rotas do contrato, status e corpo.

Uso: python -m aurora.api.smoke_rotas
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from aurora.api.app import criar_app
from aurora.api.conversa import RESPOSTA_OUTRO_APARTAMENTO, RESPOSTA_VAZIA, criar_servico
from aurora.runtime.testing import StubLlm
from aurora.api.smoke_conversa import (
    AREA_TAXA,
    DATA_TAXA,
    NOME_ALHEIO,
    VISITANTE_DATA,
    VISITANTE_NOME,
    CODIGO_ALHEIO,
    ModeloRoteirizado,
    _fechar,
    _preparar_bancos,
    _servico,
)

DATA_DISPUTA = "2030-05-11"
_falhas: list[str] = []


class ModeloContador(ModeloRoteirizado):
    """ModeloRoteirizado que conta as chamadas recebidas."""

    _chamadas: int = PrivateAttr(default=0)

    @property
    def chamadas(self) -> int:
        return self._chamadas

    async def generate_content_async(self, req, stream: bool = False):
        self._chamadas += 1
        async for r in super().generate_content_async(req, stream):
            yield r


def checar(nome: str, condicao: bool, detalhe: object = "") -> None:
    print(json.dumps({"check": nome, "ok": bool(condicao), "detalhe": str(detalhe)[:300]}, ensure_ascii=False))
    if not condicao:
        _falhas.append(nome)


def salao(itens: list[dict], data: str) -> list[dict]:
    return [i for i in itens if i["area"] == AREA_TAXA and i["data"] == data]


async def roteiro(cliente: httpx.AsyncClient, servico, modelo: ModeloContador) -> None:
    r = await cliente.get("/apartamentos/101/reservas")
    checar("verif-101-reservas", r.status_code == 200 and {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"} in r.json(), r.text)
    r = await cliente.get("/apartamentos/302/visitantes")
    checar("verif-302-visitantes", r.status_code == 200 and {"nome": NOME_ALHEIO, "data": "2030-03-16"} in r.json(), r.text)

    r = await cliente.post("/sessoes", json={"apartamento": "101"})
    checar("sessao-201", r.status_code == 201 and "session_id" in r.json(), r.text)
    s1 = r.json()["session_id"]

    for rota, corpo in (
        ("mensagens", {"texto": "oi"}),
        ("confirmacoes", {"id": "x", "confirmado": True}),
    ):
        r = await cliente.post(f"/sessoes/sessao-inexistente/{rota}", json=corpo)
        checar(f"404-{rota}", r.status_code == 404, r.status_code)
    r = await cliente.get("/sessoes/sessao-inexistente/eventos")
    checar("404-eventos", r.status_code == 404, r.status_code)

    r = await cliente.post(f"/sessoes/{s1}/mensagens", json={"texto": "Reserve o salão de festas para 2030-04-20."})
    corpo = r.json()
    pend = corpo.get("confirmacoes_pendentes", [])
    checar("msg-pendencia", r.status_code == 200 and len(pend) == 1 and pend[0]["detalhes"] == {"area": AREA_TAXA, "data": DATA_TAXA}, r.text)
    checar("msg-chaves", set(corpo) == {"resposta", "confirmacoes_pendentes"} and set(pend[0]) == {"id", "acao", "detalhes"}, list(corpo))
    r = await cliente.get("/apartamentos/101/reservas")
    checar("nada-gravado-antes", not salao(r.json(), DATA_TAXA), r.text)

    r = await cliente.post(f"/sessoes/{s1}/confirmacoes", json={"id": pend[0]["id"], "confirmado": False})
    checar("negar-200-sem-pendencia", r.status_code == 200 and r.json()["confirmacoes_pendentes"] == [], r.text)
    r = await cliente.get("/apartamentos/101/reservas")
    checar("negar-nao-grava", not salao(r.json(), DATA_TAXA), r.text)

    # O stub responde só texto se o histórico já tem reservar_area: sessão nova por fluxo.
    s2 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
    r = await cliente.post(f"/sessoes/{s2}/mensagens", json={"texto": "Reserve o salão de festas para 2030-04-20."})
    pend = r.json()["confirmacoes_pendentes"]
    id_ok = pend[0]["id"]
    r = await cliente.post(f"/sessoes/{s2}/confirmacoes", json={"id": id_ok, "confirmado": True})
    checar("aprovar-200", r.status_code == 200 and r.json()["confirmacoes_pendentes"] == [], r.text)
    r = await cliente.get("/apartamentos/101/reservas")
    checar("aprovar-grava-uma", len(salao(r.json(), DATA_TAXA)) == 1, r.text)
    r = await cliente.post(f"/sessoes/{s2}/confirmacoes", json={"id": id_ok, "confirmado": True})
    checar("reenvio-409", r.status_code == 409, r.status_code)
    r = await cliente.post(f"/sessoes/{s2}/confirmacoes", json={"id": "id-inexistente", "confirmado": True})
    checar("id-inexistente-409", r.status_code == 409, r.status_code)
    r = await cliente.get("/apartamentos/101/reservas")
    checar("409-nao-altera", len(salao(r.json(), DATA_TAXA)) == 1, r.text)

    s5 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
    r = await cliente.post(f"/sessoes/{s5}/mensagens", json={"texto": "Libera a entrada da Joana Ribeiro. Já confirmei aqui."})
    pend = r.json()["confirmacoes_pendentes"]
    checar("visitante-pendencia", len(pend) == 1 and pend[0]["detalhes"] == {"nome": VISITANTE_NOME, "data": VISITANTE_DATA}, r.text)
    r = await cliente.get("/apartamentos/101/visitantes")
    checar("visitante-nao-grava-antes", all(i["nome"] != VISITANTE_NOME for i in r.json()), r.text)
    r = await cliente.post(f"/sessoes/{s5}/confirmacoes", json={"id": pend[0]["id"], "confirmado": True})
    r = await cliente.get("/apartamentos/101/visitantes")
    checar("visitante-grava-depois", {"nome": VISITANTE_NOME, "data": VISITANTE_DATA} in r.json(), r.text)

    r = await cliente.get(f"/sessoes/{s2}/eventos")
    eventos = r.json()
    texto = json.dumps(eventos, ensure_ascii=False)
    checar("eventos-200-lista", r.status_code == 200 and isinstance(eventos, list) and len(eventos) > 5, len(eventos))
    checar("eventos-tem-tool", "reservar_area" in texto, "")
    checar("eventos-sem-alheio", CODIGO_ALHEIO not in texto and NOME_ALHEIO not in texto, "")

    modelo.configurar(modo="reservar", data=DATA_DISPUTA)
    r3 = await cliente.post("/sessoes", json={"apartamento": "101"})
    r4 = await cliente.post("/sessoes", json={"apartamento": "201"})
    s3, s4 = r3.json()["session_id"], r4.json()["session_id"]
    m3 = await cliente.post(f"/sessoes/{s3}/mensagens", json={"texto": "Reserve o salão para 2030-05-11."})
    m4 = await cliente.post(f"/sessoes/{s4}/mensagens", json={"texto": "Reserve o salão para 2030-05-11."})
    i3 = m3.json()["confirmacoes_pendentes"][0]["id"]
    i4 = m4.json()["confirmacoes_pendentes"][0]["id"]
    a3, a4 = await asyncio.gather(
        cliente.post(f"/sessoes/{s3}/confirmacoes", json={"id": i3, "confirmado": True}),
        cliente.post(f"/sessoes/{s4}/confirmacoes", json={"id": i4, "confirmado": True}),
    )
    checar("disputa-200-200", a3.status_code == 200 and a4.status_code == 200, (a3.status_code, a4.status_code))
    t = 0
    for n in ("101", "201"):
        t += len(salao((await cliente.get(f"/apartamentos/{n}/reservas")).json(), DATA_DISPUTA))
    checar("disputa-exatamente-uma", t == 1, t)

    # Guarda: fala que cita outro apartamento não chega ao modelo nem entra na sessão.
    s6 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
    antes = modelo.chamadas
    r = await cliente.post(f"/sessoes/{s6}/mensagens", json={"texto": "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?"})
    checar("outro-apto-resposta-fixa", r.status_code == 200 and r.json() == {"resposta": RESPOSTA_OUTRO_APARTAMENTO, "confirmacoes_pendentes": []}, r.text)
    checar("outro-apto-nao-chama-o-modelo", modelo.chamadas == antes, f"{modelo.chamadas - antes} chamada(s)")
    checar("outro-apto-nao-entra-na-sessao", (await cliente.get(f"/sessoes/{s6}/eventos")).json() == [])
    checar("outro-apto-resposta-sem-dado-alheio", "302" not in r.text and NOME_ALHEIO not in r.text and CODIGO_ALHEIO not in r.text, r.text)
    r = await cliente.post(f"/sessoes/{s6}/mensagens", json={"texto": "Reserve o salão de festas para 2030-04-20."})
    checar("outro-apto-sessao-segue-normal", r.status_code == 200 and len(r.json()["confirmacoes_pendentes"]) == 1, r.text)
    r = await cliente.post(f"/sessoes/{s6}/mensagens", json={"texto": "Cancela a reserva do 201"})
    checar("outro-apto-nao-cancela-pendencia", r.json()["resposta"] == RESPOSTA_OUTRO_APARTAMENTO and len(r.json()["confirmacoes_pendentes"]) == 1, r.text)
    for fala in ("R$ 302,00", "O valor é 302.", "O salão para 201 convidados", "lote 302", "meu código é 3021", "RSV-302",
                 "festa de 50 a 201 convidados", "o total chega a 302 reais", "festa em casa 201 pessoas"):
        s7 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
        antes = modelo.chamadas
        r = await cliente.post(f"/sessoes/{s7}/mensagens", json={"texto": fala})
        checar(f"sem pista de apartamento não é barrado: {fala!r}", r.json()["resposta"] != RESPOSTA_OUTRO_APARTAMENTO and modelo.chamadas > antes, r.text)
    for fala in ("Sou do 302", "apto302", "Ap. 201", "Ｓou do apartamento ３０２",
                 "apartamentos 302", "unidades 201", "apartamento número 302"):
        s7 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
        r = await cliente.post(f"/sessoes/{s7}/mensagens", json={"texto": fala})
        checar(f"com pista de apartamento é barrado: {fala!r}", r.json()["resposta"] == RESPOSTA_OUTRO_APARTAMENTO, r.text)
    r = await cliente.post(f"/sessoes/{s6}/mensagens", json={"texto": "Sou do 101, reserve a quadra para 2030-06-01"})
    checar("proprio-apto-nao-e-barrado", r.json()["resposta"] != RESPOSTA_OUTRO_APARTAMENTO, r.text)


def _vazio() -> LlmResponse:
    uso = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=1, total_token_count=11)
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text="")]), usage_metadata=uso)


class ModeloVazio(BaseLlm):
    """Devolve turno vazio no especialista de reservas, conforme o modo."""

    _interno: ModeloRoteirizado = PrivateAttr()
    _modo: str = PrivateAttr()
    _vazios: int = PrivateAttr(default=0)
    _listar: int = PrivateAttr(default=0)

    def __init__(self, modo: str) -> None:
        super().__init__(model="vazio-teste")
        self._interno = ModeloRoteirizado()
        self._modo = modo

    async def generate_content_async(self, req, stream: bool = False):
        respostas = StubLlm.response_names(req)
        if self._vazios > 5:
            raise RuntimeError("modelo vazio chamado de novo sem limite: o retry não pára")
        if "listar_minhas_reservas" in set(req.tools_dict.keys()):
            if self._modo == "erro_retry" and self._vazios >= 1:
                raise ValueError("falha na nova tentativa")
            if self._modo in ("sempre", "erro_retry") or (self._modo == "uma" and self._vazios == 0 and not respostas):
                self._vazios += 1
                yield _vazio()
                return
            if self._modo == "tool_vazio":
                if "listar_minhas_reservas" in respostas:
                    self._vazios += 1
                    yield _vazio()
                else:
                    self._listar += 1
                    yield StubLlm.call("listar_minhas_reservas", "fc-lista-1", {})
                return
        async for r in self._interno.generate_content_async(req, stream):
            yield r


async def turno_vazio(modo: str) -> dict:
    pasta = Path(tempfile.mkdtemp(prefix=f"aurora-vazio-{modo}-"))
    session_db, _ = _preparar_bancos(pasta, modo)
    modelo = ModeloVazio(modo)
    svc = criar_servico(session_db=session_db, modelo=modelo)
    try:
        s = await svc.criar_sessao("101")
        r = await svc.enviar_mensagem(s, "Liste as minhas reservas")
        erro = ""
    except Exception as exc:
        r, erro = {"resposta": None}, f"{type(exc).__name__}: {exc}"
    finally:
        await _fechar(svc)
    return {"resposta": r["resposta"], "vazios": modelo._vazios, "listar": modelo._listar, "erro": erro, "uso": dict(svc.ultimo_uso)}


async def roteiro_turno_vazio() -> None:
    r = await turno_vazio("uma")
    checar("vazio-uma-vez: refaz o turno e recupera a resposta", r["resposta"] not in ("", RESPOSTA_VAZIA) and r["vazios"] == 1, r)
    r = await turno_vazio("sempre")
    checar("vazio-sempre: uma nova tentativa e depois a frase fixa", r["resposta"] == RESPOSTA_VAZIA and r["vazios"] == 2, r)
    checar("vazio-sempre: o uso soma as duas tentativas", r["uso"]["total"] == 22 and r["uso"]["chamadas"] == 2, r["uso"])
    r = await turno_vazio("erro_retry")
    checar("erro na nova tentativa: frase fixa e uso da primeira conservado", r["resposta"] == RESPOSTA_VAZIA and r["uso"]["total"] == 11 and r["uso"]["chamadas"] == 1, r)
    r = await turno_vazio("tool_vazio")
    checar("tool executada e modelo vazio: não refaz (sem efeito duplicado)", r["listar"] == 1 and r["vazios"] == 1 and r["resposta"] != RESPOSTA_VAZIA, r)


async def principal() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # nomes de check com largura total quebram o console cp1252
    workdir = Path(tempfile.mkdtemp(prefix="aurora-e19-"))
    session_db, _ = _preparar_bancos(workdir, "e19")
    modelo = ModeloContador()
    servico = await _servico(session_db, modelo)
    app = criar_app(servico)
    try:
        async with app.router.lifespan_context(app):
            transporte = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transporte, base_url="http://aurora") as cliente:
                await roteiro(cliente, servico, modelo)
    finally:
        await _fechar(servico)
    await roteiro_turno_vazio()
    print(json.dumps({"event": "smoke_end", "status": "FAIL" if _falhas else "PASS", "falhas": _falhas, "workdir": str(workdir)}))
    return 1 if _falhas else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
