"""Roteiro de smoke E14 — escrito ANTES da implementação das tools.

VALIDAÇÃO (não ajustar se falhar; diagnosticar e reportar):
V1 funcional (contexto falso com .state["apartamento"]):
    consultar_disponibilidade → {area, data, disponivel};
    reservar_area → reservada | ja_reservada | recusada | area_invalida
        | data_invalida | sessao_invalida;
    cancelar_reserva → cancelada | nao_encontrada | ja_cancelada;
    listar_minhas_reservas → lista {codigo, area, data} ativas da sessão;
    listar_reservas_do_apartamento → mesmo formato da rota GET.
    AFIRMA: nenhuma resposta serializada contém número de outro
    apartamento nem código de outra reserva (AC-14/15/17);
    cancelar código alheio == cancelar código inexistente.

V2 política de confirmação (ADK 2.9.2 FunctionTool):
    callable True para salao-de-festas e churrasqueira, False para quadra;
    consultar/cancelar/listar sem confirmação.
    Cita arquivo:linha do ADK instalado: o callable recebe os args da chamada.

V3 concorrência (D7/AC-24/25/26) com as tools reais de src/, não o spike:
    C1 dois apts, mesma área+data → 1 reservada + 1 recusada; 1 linha ativa;
    C2 mesmo apt duas vezes → 1 linha; segunda = ja_reservada mesmo código;
    C3 disputa contra semente → recusada, sem vazamento;
    C4 cancelar e reservar de novo → novo código não repete nenhum anterior;
    C5 duas áreas diferentes ao mesmo tempo → 2 códigos distintos.
    Modos: 2 threads e 2 processos (spawn), largada sincronizada.
    DIMENSIONA ANTES (LL-15): piloto pequeno, N cabendo em ~5 min.
    Sem pausa artificial. Registra N e tempo.

V4 persistência entre processos (AC-20):
    reserva do processo A visível no B; código cancelado não é reutilizado;
    restaurar(db) devolve o estado semente (RSV-1377 no 101).

V5 controle (não é critério): SELECT-depois-INSERT sem restrição falha
    sob disputa — prova que o teste gera contenção real.

V6 banco inacessível (AURORA_DOMAIN_DB em caminho impossível):
    as 4 tools devolvem status indisponivel_temporariamente
    (nenhuma devolve recusada, nao_encontrada, disponivel true/false
    ou lista vazia sem status);
    listar_reservas_do_apartamento levanta;
    controle: com banco normal, resultados de negócio iguais.

Banco só em $TEMP. Não cria var/ nem .sqlite3 no repositório.
Sem modelo, sem chave, sem HTTP, sem ler .env.
"""

from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from aurora.dados.carregar import (
    carregar_apartamentos,
    carregar_areas,
    carregar_reservas,
)
from aurora.dados.dominio import (
    VAR_BANCO_DOMINIO,
    caminho_banco,
    garantir_banco,
    restaurar,
)
from aurora.tools.reservas import (
    CHAVE_APARTAMENTO,
    STATUS_INDISPONIVEL,
    cancelar_reserva,
    consultar_disponibilidade,
    criar_tools_reservas,
    listar_minhas_reservas,
    listar_reservas_do_apartamento,
    reservar_area,
    reservar_exige_confirmacao,
)

# Teto de referência E10; o piloto pode baixar, nunca subir acima disto.
TETO_THREADS = 200
TETO_PROCESSOS = 40
TETO_C4 = 20
ORCAMENTO_V3_S = 280.0
PILOTO_THREADS = 8
PILOTO_SPAWN = 4

REPO = Path(__file__).resolve().parents[3]
CODIGO_SEMENTE_101 = "RSV-1377"
AREA_SEMENTE_101 = "quadra"
DATA_SEMENTE_101 = "2030-03-09"
CODIGOS_SEMENTE = {"RSV-1377", "RSV-4821", "RSV-2950"}


class ContextoFalso:
    """Objeto mínimo com .state; não é ToolContext do ADK."""

    def __init__(self, apartamento: str | None) -> None:
        self.state: dict[str, str] = {}
        if apartamento is not None:
            self.state[CHAVE_APARTAMENTO] = apartamento


def _temp_dir() -> Path:
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    path = root / f"aurora-e14-{uuid4().hex}"
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


def _codigos_por_apartamento(db_path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(db_path)
    try:
        donos: dict[str, set[str]] = {}
        for codigo, apt in conn.execute("SELECT codigo, apartamento FROM reservas"):
            donos.setdefault(str(apt), set()).add(str(codigo))
        return donos
    finally:
        conn.close()


def _linhas_ativas(db_path: Path, area: str, data_valor: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT codigo, apartamento, area, data, ativa FROM reservas "
            "WHERE area=? AND data=? AND ativa=1 ORDER BY codigo",
            (area, data_valor),
        ).fetchall()
        return [
            {
                "codigo": row[0],
                "apartamento": row[1],
                "area": row[2],
                "data": row[3],
                "ativa": row[4],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _todos_codigos(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {str(row[0]) for row in conn.execute("SELECT codigo FROM reservas")}
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
    donos = _codigos_por_apartamento(db_path)
    for apt in apartamentos:
        if apt != apt_sessao and f'"{apt}"' in texto:
            raise AssertionError(
                f"{rotulo}: vazou apartamento {apt} nas respostas de {apt_sessao}: {texto}"
            )
    for dono, codigos in donos.items():
        if dono == apt_sessao:
            continue
        for codigo in codigos:
            if f'"{codigo}"' in texto:
                raise AssertionError(
                    f"{rotulo}: vazou código {codigo} do {dono} nas respostas de {apt_sessao}: {texto}"
                )


def _chamar_reservar(db_path: Path, apt: str, area: str, data_valor: str) -> dict[str, Any]:
    os.environ[VAR_BANCO_DOMINIO] = str(db_path)
    return reservar_area(area, data_valor, ContextoFalso(apt))


def _chamar_cancelar(db_path: Path, apt: str, codigo: str) -> dict[str, Any]:
    os.environ[VAR_BANCO_DOMINIO] = str(db_path)
    return cancelar_reserva(codigo, ContextoFalso(apt))


# --- V3 runners (origem: spike_concorrencia/spike_concorrencia.py; sem importar o spike) ---


def _perform_reservar(job: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _chamar_reservar(
            Path(job["db_path"]),
            job["apartamento"],
            job["area"],
            job["data"],
        )
        return {"result": result, "infra_error": None}
    except BaseException as exc:  # noqa: BLE001 — a fronteira do smoke registra
        return {
            "result": {"status": "recusada"},
            "infra_error": f"{type(exc).__name__}: {exc}",
        }


class _ThreadRunner:
    def run(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        barrier = threading.Barrier(3)
        results: list[dict[str, Any] | None] = [None, None]

        def target(index: int) -> None:
            barrier.wait()
            results[index] = _perform_reservar(jobs[index])

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
        output_queue.put(_perform_reservar(job))


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
                        "result": {"status": "recusada"},
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


def _data_caso(cenario: str, indice: int, modo: str) -> str:
    modo_off = 0 if modo == "threads" else 4000
    cenario_off = {"C1": 0, "C2": 800, "C3": 0, "C4": 1600, "C5": 2400}[cenario]
    return (date(2040, 1, 1) + timedelta(days=modo_off + cenario_off + indice)).isoformat()


def _avaliar_cenario(
    db_path: Path,
    cenario: str,
    responses: list[dict[str, Any]],
    area: str,
    data_valor: str,
    segunda_area: str,
    apt_a: str,
    apt_b: str,
) -> bool:
    public = [item["result"] for item in responses]
    statuses = sorted(item.get("status", "") for item in public)
    rows = _linhas_ativas(db_path, area, data_valor)
    if any(item.get("infra_error") for item in responses):
        return False

    if cenario == "C1":
        return statuses == ["recusada", "reservada"] and len(rows) == 1

    if cenario == "C2":
        reserved = [item for item in public if item.get("status") == "reservada"]
        repeated = [item for item in public if item.get("status") == "ja_reservada"]
        return (
            len(reserved) == 1
            and len(repeated) == 1
            and len(rows) == 1
            and reserved[0].get("codigo") == repeated[0].get("codigo")
        )

    if cenario == "C3":
        _afirmar_sem_vazamento(public, apt_a, db_path, "V3-C3-a")
        _afirmar_sem_vazamento(public, apt_b, db_path, "V3-C3-b")
        return (
            statuses == ["recusada", "recusada"]
            and len(rows) == 1
            and rows[0]["codigo"] == CODIGO_SEMENTE_101
        )

    if cenario == "C4":
        reserved = [item for item in public if item.get("status") == "reservada"]
        initial_ok = (
            statuses == ["recusada", "reservada"]
            and len(rows) == 1
            and len(reserved) == 1
        )
        if not initial_ok or not reserved:
            return False
        winning_code = str(reserved[0]["codigo"])
        winning_row = next((row for row in rows if row["codigo"] == winning_code), None)
        if winning_row is None:
            return False
        old_codes = _todos_codigos(db_path)
        cancel = _chamar_cancelar(db_path, str(winning_row["apartamento"]), winning_code)
        other = apt_b if winning_row["apartamento"] == apt_a else apt_a
        after = _chamar_reservar(db_path, other, area, data_valor)
        rows_after = _linhas_ativas(db_path, area, data_valor)
        return (
            cancel.get("status") == "cancelada"
            and after.get("status") == "reservada"
            and after.get("codigo") not in old_codes
            and len(rows_after) == 1
        )

    if cenario == "C5":
        second_rows = _linhas_ativas(db_path, segunda_area, data_valor)
        codes = [
            item.get("codigo")
            for item in public
            if item.get("status") == "reservada"
        ]
        return (
            statuses == ["reservada", "reservada"]
            and len(codes) == 2
            and len(set(codes)) == 2
            and len(rows) == 1
            and len(second_rows) == 1
        )

    return False


def _jobs_cenario(
    db_path: Path,
    cenario: str,
    indice: int,
    modo: str,
    areas: list[str],
    apartamentos: list[str],
) -> tuple[list[dict[str, Any]], str, str, str, str, str]:
    apt_a, apt_b = apartamentos[0], apartamentos[1]
    area = areas[indice % len(areas)]
    data_valor = _data_caso(cenario, indice, modo)
    segunda_area = areas[(indice + 1) % len(areas)]
    if cenario == "C2":
        apt_b = apt_a
    if cenario == "C3":
        area, data_valor = AREA_SEMENTE_101, DATA_SEMENTE_101
        outros = [item for item in apartamentos if item != "101"]
        apt_a, apt_b = outros[0], outros[1]
    jobs = [
        {
            "db_path": str(db_path),
            "apartamento": apt_a,
            "area": area,
            "data": data_valor,
        },
        {
            "db_path": str(db_path),
            "apartamento": apt_b,
            "area": segunda_area if cenario == "C5" else area,
            "data": data_valor,
        },
    ]
    return jobs, area, data_valor, segunda_area, apt_a, apt_b


def _rodar_pares(
    db_path: Path,
    runner: _ThreadRunner | _SpawnRunner,
    cenario: str,
    modo: str,
    n: int,
    areas: list[str],
    apartamentos: list[str],
) -> tuple[int, int]:
    falhas = 0
    primeira: dict[str, Any] | None = None
    for indice in range(n):
        jobs, area, data_valor, segunda_area, apt_a, apt_b = _jobs_cenario(
            db_path, cenario, indice, modo, areas, apartamentos
        )
        responses = runner.run(jobs)
        ok = _avaliar_cenario(
            db_path, cenario, responses, area, data_valor, segunda_area, apt_a, apt_b
        )
        if not ok:
            falhas += 1
            if primeira is None:
                primeira = {
                    "cenario": cenario,
                    "indice": indice,
                    "responses": responses,
                    "ativas": _linhas_ativas(db_path, area, data_valor),
                }
    if primeira is not None:
        _emit("v3_primeira_falha", **primeira)
    return falhas, n


def _dimensionar(
    db_path: Path,
    areas: list[str],
    apartamentos: list[str],
) -> dict[str, Any]:
    runner_t = _ThreadRunner()
    t0 = time.perf_counter()
    falhas_t, n_t = _rodar_pares(
        db_path, runner_t, "C1", "threads", PILOTO_THREADS, areas, apartamentos
    )
    dur_t = time.perf_counter() - t0
    runner_t.close()

    runner_s = _SpawnRunner()
    t1 = time.perf_counter()
    falhas_s, n_s = _rodar_pares(
        db_path, runner_s, "C1", "spawn", PILOTO_SPAWN, areas, apartamentos
    )
    dur_s = time.perf_counter() - t1
    runner_s.close()

    if falhas_t or falhas_s:
        raise AssertionError(
            f"piloto falhou: threads {falhas_t}/{n_t}, spawn {falhas_s}/{n_s}"
        )

    por_par_t = dur_t / max(n_t, 1)
    por_par_s = dur_s / max(n_s, 1)
    metade = ORCAMENTO_V3_S / 2.0
    # 5 cenários em threads; em spawn C4 é mais caro (cancelar+reservar extra).
    n_threads = int(metade / (por_par_t * 5))
    n_spawn = int(metade / (por_par_s * 5.5))
    n_threads = max(20, min(TETO_THREADS, n_threads))
    n_spawn = max(10, min(TETO_PROCESSOS, n_spawn))
    n_c4 = max(5, min(TETO_C4, n_spawn // 2))
    info = {
        "piloto_threads_n": n_t,
        "piloto_threads_s": round(dur_t, 3),
        "piloto_threads_s_por_par": round(por_par_t, 4),
        "piloto_spawn_n": n_s,
        "piloto_spawn_s": round(dur_s, 3),
        "piloto_spawn_s_por_par": round(por_par_s, 4),
        "n_threads": n_threads,
        "n_spawn": n_spawn,
        "n_c4": n_c4,
    }
    _emit("v3_dimensionamento", **info)
    return info


def v1(db_path: Path) -> None:
    ctx101 = ContextoFalso("101")
    ctx102 = ContextoFalso("102")
    ctx_vazio = ContextoFalso(None)
    ctx_fantasma = ContextoFalso("999")

    disp_ocupada = consultar_disponibilidade(
        AREA_SEMENTE_101, DATA_SEMENTE_101, ctx102
    )
    disp_livre = consultar_disponibilidade("quadra", "2030-06-01", ctx102)
    disp_area_invalida = consultar_disponibilidade("heliporto", "2030-06-01", ctx102)
    disp_data_invalida = consultar_disponibilidade("quadra", "09-03-2030", ctx102)

    if disp_ocupada != {
        "area": AREA_SEMENTE_101,
        "data": DATA_SEMENTE_101,
        "disponivel": False,
    }:
        raise AssertionError(f"ocupada: {disp_ocupada}")
    if disp_livre != {"area": "quadra", "data": "2030-06-01", "disponivel": True}:
        raise AssertionError(f"livre: {disp_livre}")
    if disp_area_invalida.get("disponivel") is not False:
        raise AssertionError(f"área inválida deveria ser indisponível: {disp_area_invalida}")
    if disp_data_invalida.get("disponivel") is not False:
        raise AssertionError(f"data inválida deveria ser indisponível: {disp_data_invalida}")

    area_invalida = reservar_area("heliporto", "2030-06-02", ctx102)
    data_invalida = reservar_area("quadra", "2030/06/02", ctx102)
    data_inexistente = reservar_area("quadra", "2030-02-30", ctx102)
    sessao_vazia = reservar_area("quadra", "2030-06-02", ctx_vazio)
    sessao_fantasma = reservar_area("quadra", "2030-06-02", ctx_fantasma)
    if area_invalida != {"status": "area_invalida"}:
        raise AssertionError(area_invalida)
    if data_invalida != {"status": "data_invalida"}:
        raise AssertionError(data_invalida)
    if data_inexistente != {"status": "data_invalida"}:
        raise AssertionError(data_inexistente)
    if sessao_vazia != {"status": "sessao_invalida"}:
        raise AssertionError(sessao_vazia)
    if sessao_fantasma != {"status": "sessao_invalida"}:
        raise AssertionError(sessao_fantasma)

    recusada = reservar_area(AREA_SEMENTE_101, DATA_SEMENTE_101, ctx102)
    if recusada != {"status": "recusada"}:
        raise AssertionError(f"recusa semente: {recusada}")

    reserva_102 = reservar_area("quadra", "2030-06-02", ctx102)
    if reserva_102.get("status") != "reservada":
        raise AssertionError(reserva_102)
    for chave in ("codigo", "area", "data", "taxa"):
        if chave not in reserva_102:
            raise AssertionError(f"faltou {chave}: {reserva_102}")
    if reserva_102["area"] != "quadra" or reserva_102["data"] != "2030-06-02":
        raise AssertionError(reserva_102)
    if float(reserva_102["taxa"]) != 0:
        raise AssertionError(f"quadra taxa 0: {reserva_102}")

    repetida = reservar_area("quadra", "2030-06-02", ctx102)
    if repetida.get("status") != "ja_reservada":
        raise AssertionError(repetida)
    if repetida.get("codigo") != reserva_102["codigo"]:
        raise AssertionError((repetida, reserva_102))

    salao = reservar_area("salao-de-festas", "2030-06-03", ctx102)
    if salao.get("status") != "reservada" or float(salao.get("taxa", -1)) != 150.0:
        raise AssertionError(salao)

    minhas = listar_minhas_reservas(ctx102)
    codigos_102 = {item["codigo"] for item in minhas}
    if reserva_102["codigo"] not in codigos_102 or salao["codigo"] not in codigos_102:
        raise AssertionError(minhas)
    for item in minhas:
        if set(item) != {"codigo", "area", "data"}:
            raise AssertionError(item)

    via_repo = listar_reservas_do_apartamento("102")
    if via_repo != minhas:
        raise AssertionError((via_repo, minhas))

    semente_101 = listar_reservas_do_apartamento("101")
    if semente_101 != [
        {"codigo": CODIGO_SEMENTE_101, "area": AREA_SEMENTE_101, "data": DATA_SEMENTE_101}
    ]:
        raise AssertionError(semente_101)

    cancelada = cancelar_reserva(reserva_102["codigo"], ctx102)
    if cancelada != {"status": "cancelada", "codigo": reserva_102["codigo"]}:
        raise AssertionError(cancelada)
    ja_cancelada = cancelar_reserva(reserva_102["codigo"], ctx102)
    if ja_cancelada != {"status": "ja_cancelada"}:
        raise AssertionError(ja_cancelada)

    alheia = cancelar_reserva(CODIGO_SEMENTE_101, ctx102)
    inexistente = cancelar_reserva("RSV-0000", ctx102)
    if alheia != {"status": "nao_encontrada"}:
        raise AssertionError(alheia)
    if inexistente != {"status": "nao_encontrada"}:
        raise AssertionError(inexistente)
    if alheia != inexistente:
        raise AssertionError((alheia, inexistente))

    ainda_101 = listar_reservas_do_apartamento("101")
    if ainda_101 != semente_101:
        raise AssertionError("cancelar alheia alterou o 101")

    respostas_102 = [
        disp_ocupada,
        disp_livre,
        recusada,
        reserva_102,
        repetida,
        salao,
        minhas,
        cancelada,
        ja_cancelada,
        alheia,
        inexistente,
        area_invalida,
        data_invalida,
        sessao_vazia,
    ]
    _afirmar_sem_vazamento(respostas_102, "102", db_path, "V1-102")

    _emit(
        "v1_ok",
        recusa_semente=recusada,
        cancelar_alheia=alheia,
        cancelar_inexistente=inexistente,
        identicas=alheia == inexistente,
        sem_vazamento=True,
    )


def v2() -> None:
    adk_tool = inspect.getsourcefile(
        __import__("google.adk.tools.function_tool", fromlist=["FunctionTool"]).FunctionTool
    )
    from google.adk.tools.function_tool import FunctionTool

    src = inspect.getsource(FunctionTool.check_require_confirmation)
    linha = FunctionTool.check_require_confirmation.__code__.co_firstlineno
    if "args_to_call" not in src or "_prepare_invocation_args" not in src:
        raise AssertionError("ADK não passa args da chamada ao callable")
    _emit(
        "v2_adk_api",
        arquivo=adk_tool,
        linha_check_require_confirmation=linha,
        docstring_init=(
            "function_tool.py:110-113: callable that takes the function's "
            "arguments and returns a boolean"
        ),
        evidencia=(
            f"{adk_tool}:{linha} check_require_confirmation prepara "
            "args_to_call via _prepare_invocation_args(args, tool_context) "
            "e invoca o callable com esses argumentos da chamada"
        ),
    )

    if not reservar_exige_confirmacao(area="salao-de-festas", data="2030-06-10"):
        raise AssertionError("salao deveria exigir confirmação")
    if not reservar_exige_confirmacao(area="churrasqueira", data="2030-06-10"):
        raise AssertionError("churrasqueira deveria exigir confirmação")
    if reservar_exige_confirmacao(area="quadra", data="2030-06-10"):
        raise AssertionError("quadra NÃO exige confirmação")

    tools = criar_tools_reservas()
    por_nome = {tool.name: tool for tool in tools}
    esperados = {
        "consultar_disponibilidade",
        "reservar_area",
        "cancelar_reserva",
        "listar_minhas_reservas",
    }
    if set(por_nome) != esperados:
        raise AssertionError(set(por_nome))
    reservar_tool = por_nome["reservar_area"]
    if reservar_tool._require_confirmation is not reservar_exige_confirmacao:
        raise AssertionError("reservar_area sem callable de confirmação")
    for nome in (
        "consultar_disponibilidade",
        "cancelar_reserva",
        "listar_minhas_reservas",
    ):
        if por_nome[nome]._require_confirmation:
            raise AssertionError(f"{nome} não deveria exigir confirmação")
    _emit("v2_ok", tools=sorted(por_nome), confirmacao_reservar="callable")


def v3(db_path: Path, dim: dict[str, Any]) -> None:
    areas = [item["id"] for item in carregar_areas()]
    apartamentos = [item["numero"] for item in carregar_apartamentos()]
    n_threads = int(dim["n_threads"])
    n_spawn = int(dim["n_spawn"])
    n_c4 = int(dim["n_c4"])
    inicio = time.perf_counter()

    for modo, runner, n_base in (
        ("threads", _ThreadRunner(), n_threads),
        ("spawn", _SpawnRunner(), n_spawn),
    ):
        try:
            for cenario in ("C1", "C2", "C3", "C4", "C5"):
                n = n_c4 if cenario == "C4" and modo == "spawn" else n_base
                t0 = time.perf_counter()
                falhas, total = _rodar_pares(
                    db_path, runner, cenario, modo, n, areas, apartamentos
                )
                dur = round(time.perf_counter() - t0, 3)
                _emit(
                    "v3_celula",
                    modo=modo,
                    cenario=cenario,
                    n=total,
                    falhas=falhas,
                    segundos=dur,
                )
                if falhas:
                    raise AssertionError(f"V3 {modo}/{cenario}: {falhas}/{total} falhas")
        finally:
            runner.close()

    _emit("v3_ok", segundos=round(time.perf_counter() - inicio, 3), **dim)


def _v4_reservar(caminho: str, apt: str, area: str, data_valor: str, destino: Any) -> None:
    os.environ[VAR_BANCO_DOMINIO] = caminho
    destino.put(_chamar_reservar(Path(caminho), apt, area, data_valor))


def _v4_listar(caminho: str, apt: str, destino: Any) -> None:
    os.environ[VAR_BANCO_DOMINIO] = caminho
    destino.put(listar_reservas_do_apartamento(apt, caminho))


def v4(db_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    q_a: Any = ctx.Queue()
    p_a = ctx.Process(
        target=_v4_reservar,
        args=(str(db_path), "102", "quadra", "2030-07-01", q_a),
    )
    p_a.start()
    p_a.join(timeout=30)
    reserva_a = q_a.get(timeout=5)
    if reserva_a.get("status") != "reservada":
        raise AssertionError(reserva_a)

    q_b: Any = ctx.Queue()
    p_b = ctx.Process(
        target=_v4_listar,
        args=(str(db_path), "102", q_b),
    )
    p_b.start()
    p_b.join(timeout=30)
    lista_b = q_b.get(timeout=5)
    if reserva_a["codigo"] not in {item["codigo"] for item in lista_b}:
        raise AssertionError(("processo B não viu a reserva de A", lista_b, reserva_a))

    cancel = _chamar_cancelar(db_path, "102", reserva_a["codigo"])
    if cancel.get("status") != "cancelada":
        raise AssertionError(cancel)
    antigos = _todos_codigos(db_path)

    q_c: Any = ctx.Queue()
    p_c = ctx.Process(
        target=_v4_reservar,
        args=(str(db_path), "201", "quadra", "2030-07-01", q_c),
    )
    p_c.start()
    p_c.join(timeout=30)
    reserva_c = q_c.get(timeout=5)
    if reserva_c.get("status") != "reservada":
        raise AssertionError(reserva_c)
    if reserva_c["codigo"] in antigos:
        raise AssertionError(f"código reutilizado: {reserva_c} antigos={antigos}")
    if reserva_c["codigo"] == reserva_a["codigo"]:
        raise AssertionError("reusou código cancelado")

    restaurar(db_path)
    semente = listar_reservas_do_apartamento("101", db_path)
    if semente != [
        {
            "codigo": CODIGO_SEMENTE_101,
            "area": AREA_SEMENTE_101,
            "data": DATA_SEMENTE_101,
        }
    ]:
        raise AssertionError(("restaurar não devolveu semente", semente))
    extra_102 = listar_reservas_do_apartamento("102", db_path)
    if extra_102:
        raise AssertionError(("102 deveria voltar sem reservas", extra_102))
    _emit(
        "v4_ok",
        codigo_a=reserva_a["codigo"],
        codigo_novo=reserva_c["codigo"],
        semente_101=semente,
    )


def _e0_select_then_insert(
    db_path: Path, apt: str, area: str, data_valor: str, codigo: str
) -> None:
    # Origem: spike_concorrencia ReservationStore.unsafe_select_then_insert (E0).
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        found = conn.execute(
            "SELECT codigo FROM reservas_e0 WHERE area=? AND data=? AND ativa=1",
            (area, data_valor),
        ).fetchone()
    finally:
        conn.close()
    if found:
        return
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO reservas_e0(codigo, apartamento, area, data, ativa) "
            "VALUES (?, ?, ?, ?, 1)",
            (codigo, apt, area, data_valor),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def v5(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reservas_e0("
            "codigo TEXT PRIMARY KEY,"
            "apartamento TEXT NOT NULL,"
            "area TEXT NOT NULL,"
            "data TEXT NOT NULL,"
            "ativa INTEGER NOT NULL)"
        )
    finally:
        conn.close()

    falhas = 0
    n = 40
    for indice in range(n):
        data_valor = (date(2050, 1, 1) + timedelta(days=indice)).isoformat()
        barrier = threading.Barrier(3)

        def alvo(apt: str, codigo: str, quando: str = data_valor) -> None:
            barrier.wait()
            _e0_select_then_insert(db_path, apt, "quadra", quando, codigo)

        t1 = threading.Thread(target=alvo, args=("101", f"E0-A-{indice}"))
        t2 = threading.Thread(target=alvo, args=("102", f"E0-B-{indice}"))
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()
        ativas = sqlite3.connect(db_path).execute(
            "SELECT COUNT(*) FROM reservas_e0 WHERE area=? AND data=? AND ativa=1",
            ("quadra", data_valor),
        ).fetchone()[0]
        if ativas != 1:
            falhas += 1

    _emit("v5_controle", n=n, falhas_e0=falhas)
    if falhas == 0:
        raise AssertionError(
            "V5: SELECT-depois-INSERT não falhou — o teste não gerou contenção"
        )


def v6(db_controle: Path) -> None:
    import aurora.tools as aurora_tools

    if STATUS_INDISPONIVEL != "indisponivel_temporariamente":
        raise AssertionError(STATUS_INDISPONIVEL)
    if getattr(aurora_tools, "STATUS_INDISPONIVEL", None) != STATUS_INDISPONIVEL:
        raise AssertionError("STATUS_INDISPONIVEL não exportado em tools/__init__.py")

    ctx = ContextoFalso("102")
    anterior = os.environ.get(VAR_BANCO_DOMINIO)
    impossivel = r"Z:\caminho\que\nao\existe\d.sqlite3"
    os.environ[VAR_BANCO_DOMINIO] = impossivel
    try:
        consultar = consultar_disponibilidade("quadra", "2030-06-01", ctx)
        reservar = reservar_area("quadra", "2030-08-01", ctx)
        cancelar = cancelar_reserva("RSV-0000", ctx)
        listar = listar_minhas_reservas(ctx)
        if consultar != {"status": STATUS_INDISPONIVEL}:
            raise AssertionError(f"consultar infra: {consultar}")
        if "disponivel" in consultar:
            raise AssertionError(f"consultar não pode ter disponivel: {consultar}")
        if reservar != {"status": STATUS_INDISPONIVEL}:
            raise AssertionError(f"reservar infra: {reservar}")
        if reservar.get("status") == "recusada":
            raise AssertionError(f"reservar mascarou infra como recusada: {reservar}")
        if cancelar != {"status": STATUS_INDISPONIVEL}:
            raise AssertionError(f"cancelar infra: {cancelar}")
        if cancelar.get("status") == "nao_encontrada":
            raise AssertionError(
                f"cancelar mascarou infra como nao_encontrada: {cancelar}"
            )
        if isinstance(listar, list):
            raise AssertionError(f"lista vazia sem status: {listar}")
        if listar != {"status": STATUS_INDISPONIVEL, "reservas": []}:
            raise AssertionError(f"listar infra: {listar}")
        levantou = False
        try:
            listar_reservas_do_apartamento("102")
        except Exception:
            levantou = True
        if not levantou:
            raise AssertionError("listar_reservas_do_apartamento deveria levantar")
    finally:
        if anterior is None:
            os.environ.pop(VAR_BANCO_DOMINIO, None)
        else:
            os.environ[VAR_BANCO_DOMINIO] = anterior

    os.environ[VAR_BANCO_DOMINIO] = str(db_controle)
    ctx102 = ContextoFalso("102")
    recusada = reservar_area(AREA_SEMENTE_101, DATA_SEMENTE_101, ctx102)
    if recusada != {"status": "recusada"}:
        raise AssertionError(f"controle recusada: {recusada}")
    inexistente = cancelar_reserva("RSV-0000", ctx102)
    if inexistente != {"status": "nao_encontrada"}:
        raise AssertionError(f"controle nao_encontrada: {inexistente}")
    disp_ocupada = consultar_disponibilidade(
        AREA_SEMENTE_101, DATA_SEMENTE_101, ctx102
    )
    if disp_ocupada != {
        "area": AREA_SEMENTE_101,
        "data": DATA_SEMENTE_101,
        "disponivel": False,
    }:
        raise AssertionError(f"controle ocupada: {disp_ocupada}")
    disp_livre = consultar_disponibilidade("quadra", "2030-09-01", ctx102)
    if disp_livre != {"area": "quadra", "data": "2030-09-01", "disponivel": True}:
        raise AssertionError(f"controle livre: {disp_livre}")
    minhas = listar_minhas_reservas(ctx102)
    if not isinstance(minhas, list):
        raise AssertionError(f"controle listar deveria ser lista: {minhas}")
    semente = listar_reservas_do_apartamento("101")
    if semente != [
        {
            "codigo": CODIGO_SEMENTE_101,
            "area": AREA_SEMENTE_101,
            "data": DATA_SEMENTE_101,
        }
    ]:
        raise AssertionError(f"controle semente: {semente}")
    _emit(
        "v6_ok",
        status_infra=STATUS_INDISPONIVEL,
        controle_recusada=recusada,
        controle_nao_encontrada=inexistente,
        controle_disponivel_ocupada=disp_ocupada["disponivel"],
        controle_disponivel_livre=disp_livre["disponivel"],
    )


def _assert_sem_sqlite_no_repo() -> None:
    achados = [
        str(path.relative_to(REPO))
        for path in REPO.rglob("*.sqlite3")
        if ".venv" not in path.parts
        and "spike_" not in path.parts
    ]
    var_dir = REPO / "var"
    if achados:
        raise AssertionError(f"sqlite3 no repositório: {achados}")
    if var_dir.exists():
        raise AssertionError("var/ criado no repositório")


def main() -> int:
    multiprocessing.freeze_support()
    inicio = time.perf_counter()
    pasta = _temp_dir()
    falhou = False
    try:
        if caminho_banco.__name__ != "caminho_banco":
            raise AssertionError("contrato caminho_banco ausente")
        db_v1 = _usar_banco(pasta / "v1.sqlite3")
        v1(db_v1)
        v2()
        db_piloto = _usar_banco(pasta / "piloto.sqlite3")
        areas = [item["id"] for item in carregar_areas()]
        apts = [item["numero"] for item in carregar_apartamentos()]
        dim = _dimensionar(db_piloto, areas, apts)
        db_v3 = _usar_banco(pasta / "v3.sqlite3")
        v3(db_v3, dim)
        db_v4 = _usar_banco(pasta / "v4.sqlite3")
        v4(db_v4)
        db_v5 = pasta / "v5.sqlite3"
        os.environ[VAR_BANCO_DOMINIO] = str(db_v5)
        garantir_banco(db_v5)
        v5(db_v5)
        db_v6 = _usar_banco(pasta / "v6.sqlite3")
        v6(db_v6)
        _assert_sem_sqlite_no_repo()
        _emit(
            "smoke_end",
            status="PASS",
            segundos=round(time.perf_counter() - inicio, 3),
            temp=str(pasta),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        falhou = True
        _emit(
            "smoke_end",
            status="FAIL",
            erro=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
            segundos=round(time.perf_counter() - inicio, 3),
        )
        return 1
    finally:
        if not falhou:
            pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
