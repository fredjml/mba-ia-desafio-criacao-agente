"""Roteiro de smoke E15 — escrito ANTES da implementação das tools.

VALIDAÇÃO (não ajustar se falhar; diagnosticar e reportar):
W1 funcional (contexto falso com .state["apartamento"]):
    autorizar_visitante → autorizado | ja_autorizado | nome_invalido
        | data_invalida | sessao_invalida;
    listar_meus_visitantes → lista {nome, data} só do apt da sessão;
    listar_visitantes_do_apartamento → formato da rota GET.
    AFIRMA: nenhuma resposta contém dados de outro apartamento (AC-14);
    302 tem Marina Duarte e o 101 não a vê.

W2 política (caminho real do ADK 2.9.2):
    await tool.check_require_confirmation(args, ctx)
    autorizar_visitante → True; listar_meus_visitantes → False.
    tool._get_declaration().parameters_json_schema NÃO expõe
    "apartamento" nem "tool_context".

W3 idempotência sob concorrência:
    mesma autorização 2× ao mesmo tempo → 1 linha,
    uma "autorizado" e uma "ja_autorizado".
    2 threads (N alvo 100) e 2 processos spawn (N alvo 20),
    largada sincronizada. N dimensionado por piloto (LL-15).
    Controle SELECT-depois-INSERT sem restrição DEVE falhar sob disputa.

W4 persistência entre processos:
    visitante gravado no processo A aparece no B;
    dominio.restaurar(db) devolve a semente (302/Marina Duarte).

W5 falha de infraestrutura:
    AURORA_DOMAIN_DB = Z:\\caminho\\que\\nao\\existe\\d.sqlite3
    → tools devolvem indisponivel_temporariamente
    e listar_visitantes_do_apartamento LEVANTA.

Banco só em $TEMP. Não cria var/ nem .sqlite3 no repositório.
Sem modelo, sem chave, sem HTTP, sem ler .env.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue
import sqlite3
import threading
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from aurora.dados.carregar import carregar_apartamentos, carregar_visitantes
from aurora.dados.dominio import (
    VAR_BANCO_DOMINIO,
    caminho_banco,
    garantir_banco,
    restaurar,
)
from aurora.tools.visitantes import (
    autorizar_visitante,
    criar_tools_visitantes,
    listar_meus_visitantes,
    listar_visitantes_do_apartamento,
)

TETO_THREADS = 100
TETO_PROCESSOS = 20
ORCAMENTO_W3_S = 60.0
PILOTO_THREADS = 8
PILOTO_SPAWN = 4

REPO = Path(__file__).resolve().parents[3]
NOME_SEMENTE_302 = "Marina Duarte"
DATA_SEMENTE_302 = "2030-03-16"
NOME_SEMENTE_201 = "Paulo Nogueira"


class ContextoFalso:
    """Objeto mínimo com .state; não é ToolContext do ADK."""

    def __init__(self, apartamento: str | None) -> None:
        self.state: dict[str, str] = {}
        if apartamento is not None:
            self.state["apartamento"] = apartamento


def _temp_dir() -> Path:
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    path = root / f"aurora-e15-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _usar_banco(db_path: Path) -> Path:
    os.environ[VAR_BANCO_DOMINIO] = str(db_path)
    garantir_banco(db_path)
    restaurar(db_path)
    return db_path


def _emit(event: str, **payload: Any) -> None:
    print(
        json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _serializar(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True)


def _linhas_visitantes(
    db_path: Path, apartamento: str, nome: str, data_valor: str
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM visitantes "
                "WHERE apartamento=? AND lower(nome)=lower(?) AND data=?",
                (apartamento, nome, data_valor),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _nomes_por_apartamento(db_path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(db_path)
    try:
        donos: dict[str, set[str]] = {}
        for apt, nome in conn.execute("SELECT apartamento, nome FROM visitantes"):
            donos.setdefault(str(apt), set()).add(str(nome))
        return donos
    finally:
        conn.close()


def _afirmar_sem_vazamento(
    respostas: list[Any],
    apt_sessao: str,
    db_path: Path,
    rotulo: str,
) -> None:
    texto = _serializar(respostas)
    apartamentos = {str(item["numero"]) for item in carregar_apartamentos()}
    donos = _nomes_por_apartamento(db_path)
    for apt in apartamentos:
        if apt != apt_sessao and f'"{apt}"' in texto:
            raise AssertionError(
                f"{rotulo}: vazou apartamento {apt} nas respostas de {apt_sessao}: {texto}"
            )
    for dono, nomes in donos.items():
        if dono == apt_sessao:
            continue
        for nome in nomes:
            if nome in texto:
                raise AssertionError(
                    f"{rotulo}: vazou visitante {nome!r} do {dono} "
                    f"nas respostas de {apt_sessao}: {texto}"
                )


def _chamar_autorizar(
    db_path: Path, apt: str, nome: str, data_valor: str
) -> dict[str, Any]:
    os.environ[VAR_BANCO_DOMINIO] = str(db_path)
    return autorizar_visitante(nome, data_valor, ContextoFalso(apt))


def w1(db_path: Path) -> None:
    ctx101 = ContextoFalso("101")
    ctx302 = ContextoFalso("302")
    ctx_vazio = ContextoFalso(None)
    ctx_fantasma = ContextoFalso("999")

    lista_302 = listar_meus_visitantes(ctx302)
    if not isinstance(lista_302, list):
        raise AssertionError(f"listar 302 deveria ser lista: {lista_302}")
    if {"nome": NOME_SEMENTE_302, "data": DATA_SEMENTE_302} not in lista_302:
        raise AssertionError(f"302 sem Marina Duarte: {lista_302}")

    lista_101 = listar_meus_visitantes(ctx101)
    if not isinstance(lista_101, list):
        raise AssertionError(f"listar 101 deveria ser lista: {lista_101}")
    if any(item.get("nome") == NOME_SEMENTE_302 for item in lista_101):
        raise AssertionError(f"101 viu Marina Duarte: {lista_101}")
    if any(item.get("nome") == NOME_SEMENTE_201 for item in lista_101):
        raise AssertionError(f"101 viu Paulo Nogueira: {lista_101}")

    via_repo_302 = listar_visitantes_do_apartamento("302")
    if via_repo_302 != lista_302:
        raise AssertionError((via_repo_302, lista_302))
    for item in via_repo_302:
        if set(item) != {"nome", "data"}:
            raise AssertionError(item)

    nome_ok = "  Maria   Clara  "
    data_ok = "2030-08-01"
    autorizado = autorizar_visitante(nome_ok, data_ok, ctx101)
    if autorizado != {
        "status": "autorizado",
        "nome": "Maria Clara",
        "data": data_ok,
    }:
        raise AssertionError(autorizado)

    repetido = autorizar_visitante(nome_ok, data_ok, ctx101)
    if repetido != {
        "status": "ja_autorizado",
        "nome": "Maria Clara",
        "data": data_ok,
    }:
        raise AssertionError(repetido)

    nomes_invalidos = [
        autorizar_visitante("", data_ok, ctx101),
        autorizar_visitante("   ", data_ok, ctx101),
        autorizar_visitante("Joao\x01Silva", data_ok, ctx101),
        autorizar_visitante("A" * 121, data_ok, ctx101),
    ]
    for resp in nomes_invalidos:
        if resp != {"status": "nome_invalido"}:
            raise AssertionError(resp)

    datas_invalidas = [
        autorizar_visitante("Ana Souza", "09-03-2030", ctx101),
        autorizar_visitante("Ana Souza", "2030/08/02", ctx101),
        autorizar_visitante("Ana Souza", "2030-02-30", ctx101),
        autorizar_visitante("Ana Souza", "", ctx101),
    ]
    for resp in datas_invalidas:
        if resp != {"status": "data_invalida"}:
            raise AssertionError(resp)

    sessao_vazia = autorizar_visitante("Ana Souza", "2030-08-02", ctx_vazio)
    sessao_fantasma = autorizar_visitante("Ana Souza", "2030-08-02", ctx_fantasma)
    if sessao_vazia != {"status": "sessao_invalida"}:
        raise AssertionError(sessao_vazia)
    if sessao_fantasma != {"status": "sessao_invalida"}:
        raise AssertionError(sessao_fantasma)

    minhas_101 = listar_meus_visitantes(ctx101)
    if {"nome": "Maria Clara", "data": data_ok} not in minhas_101:
        raise AssertionError(minhas_101)
    if any(item.get("nome") == NOME_SEMENTE_302 for item in minhas_101):
        raise AssertionError(f"101 listou Marina: {minhas_101}")

    ainda_302 = listar_meus_visitantes(ctx302)
    if any(item.get("nome") == "Maria Clara" for item in ainda_302):
        raise AssertionError(f"302 viu visitante do 101: {ainda_302}")

    respostas_101 = [
        lista_101,
        autorizado,
        repetido,
        *nomes_invalidos,
        *datas_invalidas,
        sessao_vazia,
        sessao_fantasma,
        minhas_101,
    ]
    _afirmar_sem_vazamento(respostas_101, "101", db_path, "W1-101")
    if NOME_SEMENTE_302 in _serializar(respostas_101):
        raise AssertionError("101 vazou Marina Duarte")

    _emit(
        "w1_ok",
        autorizado=autorizado,
        ja_autorizado=repetido,
        nome_invalido=nomes_invalidos[0],
        data_invalida=datas_invalidas[0],
        sessao_invalida=sessao_vazia,
        marina_no_302=True,
        marina_no_101=False,
        sem_vazamento=True,
    )


def w2() -> None:
    tools = criar_tools_visitantes()
    por_nome = {tool.name: tool for tool in tools}
    esperados = {"autorizar_visitante", "listar_meus_visitantes"}
    if set(por_nome) != esperados:
        raise AssertionError(set(por_nome))

    ctx = ContextoFalso("101")

    async def _checar() -> dict[str, bool]:
        autorizar = await por_nome["autorizar_visitante"].check_require_confirmation(
            {"nome": "Ana Souza", "data": "2030-08-03"},
            ctx,
        )
        listar = await por_nome["listar_meus_visitantes"].check_require_confirmation(
            {},
            ctx,
        )
        return {"autorizar": bool(autorizar), "listar": bool(listar)}

    flags = asyncio.run(_checar())
    if flags["autorizar"] is not True:
        raise AssertionError(f"autorizar deveria exigir confirmação: {flags}")
    if flags["listar"] is not False:
        raise AssertionError(f"listar NÃO exige confirmação: {flags}")

    for nome, tool in por_nome.items():
        schema = tool._get_declaration().parameters_json_schema
        blob = json.dumps(schema, ensure_ascii=False) if schema is not None else "null"
        if "apartamento" in blob or "tool_context" in blob:
            raise AssertionError(f"{nome} schema expõe apt/contexto: {schema}")
        props = (schema or {}).get("properties") or {}
        if "apartamento" in props or "tool_context" in props:
            raise AssertionError(f"{nome} properties: {props}")

    _emit(
        "w2_ok",
        tools=sorted(por_nome),
        confirmacao=flags,
        schema_autorizar=por_nome["autorizar_visitante"]
        ._get_declaration()
        .parameters_json_schema,
        schema_listar=por_nome["listar_meus_visitantes"]
        ._get_declaration()
        .parameters_json_schema,
    )


def _perform_autorizar(job: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _chamar_autorizar(
            Path(job["db_path"]),
            job["apartamento"],
            job["nome"],
            job["data"],
        )
        return {"result": result, "infra_error": None}
    except BaseException as exc:  # noqa: BLE001 — fronteira do smoke
        return {
            "result": {"status": "erro"},
            "infra_error": f"{type(exc).__name__}: {exc}",
        }


class _ThreadRunner:
    def run(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        barrier = threading.Barrier(3)
        results: list[dict[str, Any] | None] = [None, None]

        def target(index: int) -> None:
            barrier.wait()
            results[index] = _perform_autorizar(jobs[index])

        threads = [threading.Thread(target=target, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        return [item for item in results if item is not None]

    def close(self) -> None:
        return


def _process_worker(barrier: Any, input_queue: Any, output_queue: Any) -> None:
    while True:
        job = input_queue.get()
        if job is None:
            return
        barrier.wait()
        output_queue.put(_perform_autorizar(job))


class _SpawnRunner:
    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.barrier = context.Barrier(3)
        self.inputs = [context.Queue(), context.Queue()]
        self.outputs = [context.Queue(), context.Queue()]
        self.processes = [
            context.Process(
                target=_process_worker,
                args=(self.barrier, self.inputs[i], self.outputs[i]),
            )
            for i in range(2)
        ]
        for process in self.processes:
            process.start()

    def run(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, job in enumerate(jobs):
            self.inputs[index].put(job)
        self.barrier.wait()
        results = []
        for output in self.outputs:
            try:
                results.append(output.get(timeout=60))
            except queue.Empty:
                results.append(
                    {
                        "result": {"status": "erro"},
                        "infra_error": "spawn worker timeout",
                    }
                )
        return results

    def close(self) -> None:
        for input_queue in self.inputs:
            input_queue.put(None)
        for process in self.processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        for item in self.inputs + self.outputs:
            item.close()


def _jobs_par(
    db_path: Path, indice: int, modo: str
) -> tuple[list[dict[str, Any]], str, str, str]:
    offset = 0 if modo == "threads" else 4000
    data_valor = (date(2040, 1, 1) + timedelta(days=offset + indice)).isoformat()
    nome = f"Visitante {modo} {indice:04d}"
    apt = "101"
    jobs = [
        {
            "db_path": str(db_path),
            "apartamento": apt,
            "nome": nome,
            "data": data_valor,
        },
        {
            "db_path": str(db_path),
            "apartamento": apt,
            "nome": nome,
            "data": data_valor,
        },
    ]
    return jobs, apt, nome, data_valor


def _avaliar_par(
    db_path: Path,
    responses: list[dict[str, Any]],
    apt: str,
    nome: str,
    data_valor: str,
) -> bool:
    if any(item.get("infra_error") for item in responses):
        return False
    public = [item["result"] for item in responses]
    statuses = sorted(str(item.get("status", "")) for item in public)
    linhas = _linhas_visitantes(db_path, apt, nome, data_valor)
    return statuses == ["autorizado", "ja_autorizado"] and linhas == 1


def _rodar_pares(
    db_path: Path,
    runner: _ThreadRunner | _SpawnRunner,
    modo: str,
    n: int,
) -> tuple[int, int]:
    falhas = 0
    primeira: dict[str, Any] | None = None
    for indice in range(n):
        jobs, apt, nome, data_valor = _jobs_par(db_path, indice, modo)
        responses = runner.run(jobs)
        ok = _avaliar_par(db_path, responses, apt, nome, data_valor)
        if not ok:
            falhas += 1
            if primeira is None:
                primeira = {
                    "modo": modo,
                    "indice": indice,
                    "responses": responses,
                    "linhas": _linhas_visitantes(db_path, apt, nome, data_valor),
                }
    if primeira is not None:
        _emit("w3_primeira_falha", **primeira)
    return falhas, n


def _dimensionar(db_path: Path) -> dict[str, Any]:
    runner_t = _ThreadRunner()
    t0 = time.perf_counter()
    falhas_t, n_t = _rodar_pares(db_path, runner_t, "threads", PILOTO_THREADS)
    dur_t = time.perf_counter() - t0
    runner_t.close()

    runner_s = _SpawnRunner()
    t1 = time.perf_counter()
    falhas_s, n_s = _rodar_pares(db_path, runner_s, "spawn", PILOTO_SPAWN)
    dur_s = time.perf_counter() - t1
    runner_s.close()

    if falhas_t or falhas_s:
        raise AssertionError(
            f"piloto falhou: threads {falhas_t}/{n_t}, spawn {falhas_s}/{n_s}"
        )

    por_par_t = dur_t / max(n_t, 1)
    por_par_s = dur_s / max(n_s, 1)
    metade = ORCAMENTO_W3_S / 2.0
    n_threads = int(metade / max(por_par_t, 1e-6))
    n_spawn = int(metade / max(por_par_s, 1e-6))
    n_threads = max(20, min(TETO_THREADS, n_threads))
    n_spawn = max(8, min(TETO_PROCESSOS, n_spawn))
    info = {
        "piloto_threads_n": n_t,
        "piloto_threads_s": round(dur_t, 3),
        "piloto_threads_s_por_par": round(por_par_t, 4),
        "piloto_spawn_n": n_s,
        "piloto_spawn_s": round(dur_s, 3),
        "piloto_spawn_s_por_par": round(por_par_s, 4),
        "n_threads": n_threads,
        "n_spawn": n_spawn,
        "alvo_threads": TETO_THREADS,
        "alvo_spawn": TETO_PROCESSOS,
    }
    _emit("w3_dimensionamento", **info)
    return info


def _e0_select_then_insert(
    db_path: Path, apt: str, nome: str, data_valor: str
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        found = conn.execute(
            "SELECT 1 FROM visitantes_e0 WHERE apartamento=? AND nome=? AND data=?",
            (apt, nome, data_valor),
        ).fetchone()
    finally:
        conn.close()
    if found:
        return
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO visitantes_e0(apartamento, nome, data) VALUES (?, ?, ?)",
            (apt, nome, data_valor),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def w3(db_path: Path, dim: dict[str, Any]) -> None:
    n_threads = int(dim["n_threads"])
    n_spawn = int(dim["n_spawn"])
    inicio = time.perf_counter()

    for modo, runner, n in (
        ("threads", _ThreadRunner(), n_threads),
        ("spawn", _SpawnRunner(), n_spawn),
    ):
        try:
            t0 = time.perf_counter()
            falhas, total = _rodar_pares(db_path, runner, modo, n)
            dur = round(time.perf_counter() - t0, 3)
            _emit(
                "w3_celula",
                modo=modo,
                n=total,
                falhas=falhas,
                segundos=dur,
            )
            if falhas:
                raise AssertionError(f"W3 {modo}: {falhas}/{total} falhas")
        finally:
            runner.close()

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS visitantes_e0("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "apartamento TEXT NOT NULL,"
            "nome TEXT NOT NULL,"
            "data TEXT NOT NULL)"
        )
    finally:
        conn.close()

    falhas_e0 = 0
    n_e0 = 40
    for indice in range(n_e0):
        data_valor = (date(2050, 1, 1) + timedelta(days=indice)).isoformat()
        nome = f"E0 {indice:04d}"
        barrier = threading.Barrier(3)

        def alvo(quando: str = data_valor, quem: str = nome) -> None:
            barrier.wait()
            _e0_select_then_insert(db_path, "101", quem, quando)

        t1 = threading.Thread(target=alvo)
        t2 = threading.Thread(target=alvo)
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()
        ativas = sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM visitantes_e0 WHERE apartamento=? AND nome=? AND data=?",
            ("101", nome, data_valor),
        ).fetchone()[0]
        if ativas != 1:
            falhas_e0 += 1

    _emit("w3_controle_e0", n=n_e0, falhas_e0=falhas_e0)
    if falhas_e0 == 0:
        raise AssertionError(
            "W3 controle: SELECT-depois-INSERT não falhou — sem contenção"
        )
    _emit("w3_ok", segundos=round(time.perf_counter() - inicio, 3), **dim)


def _w4_autorizar(caminho: str, apt: str, nome: str, data_valor: str, destino: Any) -> None:
    os.environ[VAR_BANCO_DOMINIO] = caminho
    destino.put(_chamar_autorizar(Path(caminho), apt, nome, data_valor))


def _w4_listar(caminho: str, apt: str, destino: Any) -> None:
    os.environ[VAR_BANCO_DOMINIO] = caminho
    destino.put(listar_visitantes_do_apartamento(apt, caminho))


def w4(db_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    q_a: Any = ctx.Queue()
    p_a = ctx.Process(
        target=_w4_autorizar,
        args=(str(db_path), "101", "Beatriz Lima", "2030-09-01", q_a),
    )
    p_a.start()
    p_a.join(timeout=30)
    gravado_a = q_a.get(timeout=5)
    if gravado_a.get("status") != "autorizado":
        raise AssertionError(gravado_a)

    q_b: Any = ctx.Queue()
    p_b = ctx.Process(
        target=_w4_listar,
        args=(str(db_path), "101", q_b),
    )
    p_b.start()
    p_b.join(timeout=30)
    lista_b = q_b.get(timeout=5)
    if {"nome": "Beatriz Lima", "data": "2030-09-01"} not in lista_b:
        raise AssertionError(("processo B não viu o visitante de A", lista_b, gravado_a))

    restaurar(db_path)
    semente = listar_visitantes_do_apartamento("302", db_path)
    if semente != [{"nome": NOME_SEMENTE_302, "data": DATA_SEMENTE_302}]:
        raise AssertionError(("restaurar não devolveu semente 302", semente))
    extra_101 = listar_visitantes_do_apartamento("101", db_path)
    if extra_101:
        raise AssertionError(("101 deveria voltar sem visitantes", extra_101))
    _emit("w4_ok", gravado_a=gravado_a, semente_302=semente)


def w5(db_controle: Path) -> None:
    ctx = ContextoFalso("101")
    anterior = os.environ.get(VAR_BANCO_DOMINIO)
    impossivel = r"Z:\caminho\que\nao\existe\d.sqlite3"
    os.environ[VAR_BANCO_DOMINIO] = impossivel
    try:
        autorizar = autorizar_visitante("Ana Souza", "2030-08-04", ctx)
        listar = listar_meus_visitantes(ctx)
        if autorizar != {"status": "indisponivel_temporariamente"}:
            raise AssertionError(f"autorizar infra: {autorizar}")
        if autorizar.get("status") in {"autorizado", "ja_autorizado"}:
            raise AssertionError(f"autorizar mascarou infra: {autorizar}")
        if isinstance(listar, list):
            raise AssertionError(f"lista vazia sem status: {listar}")
        if listar != {"status": "indisponivel_temporariamente", "visitantes": []}:
            raise AssertionError(f"listar infra: {listar}")
        levantou = False
        try:
            listar_visitantes_do_apartamento("101")
        except Exception:
            levantou = True
        if not levantou:
            raise AssertionError("listar_visitantes_do_apartamento deveria levantar")
    finally:
        if anterior is None:
            os.environ.pop(VAR_BANCO_DOMINIO, None)
        else:
            os.environ[VAR_BANCO_DOMINIO] = anterior

    os.environ[VAR_BANCO_DOMINIO] = str(db_controle)
    semente = listar_visitantes_do_apartamento("302")
    esperado = [item for item in carregar_visitantes() if item["apartamento"] == "302"]
    formato = [{"nome": item["nome"], "data": item["data"]} for item in esperado]
    if semente != formato:
        raise AssertionError(f"controle semente: {semente}")
    _emit("w5_ok", status_infra="indisponivel_temporariamente", semente_302=semente)


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


def main() -> int:
    multiprocessing.freeze_support()
    inicio = time.perf_counter()
    pasta = _temp_dir()
    try:
        if caminho_banco.__name__ != "caminho_banco":
            raise AssertionError("contrato caminho_banco ausente")
        db_w1 = _usar_banco(pasta / "w1.sqlite3")
        w1(db_w1)
        w2()
        db_piloto = _usar_banco(pasta / "piloto.sqlite3")
        dim = _dimensionar(db_piloto)
        db_w3 = _usar_banco(pasta / "w3.sqlite3")
        w3(db_w3, dim)
        db_w4 = _usar_banco(pasta / "w4.sqlite3")
        w4(db_w4)
        db_w5 = _usar_banco(pasta / "w5.sqlite3")
        w5(db_w5)
        _assert_sem_sqlite_no_repo()
        _emit(
            "smoke_end",
            status="PASS",
            segundos=round(time.perf_counter() - inicio, 3),
            temp=str(pasta),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit(
            "smoke_end",
            status="FAIL",
            erro=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            segundos=round(time.perf_counter() - inicio, 3),
        )
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
