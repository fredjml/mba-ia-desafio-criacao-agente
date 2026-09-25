"""Roteiro de smoke E13 — escrito ANTES da implementação da fábrica.

Critérios (não ajustar se falharem; diagnosticar):
R-A (AC-19): processo 1 cria sessão, manda texto com StubLlm, imprime
    eventos (id, author, timestamp, texto). Processo 2 reabre a MESMA
    sessão no mesmo SQLite (objetos novos, fábrica de src/), imprime e
    compara lista idêntica; depois aceita uma nova mensagem.
R-B (regressão confirmação, N=5): tool require_confirmation=True no
    especialista (topologia E9-C) grava uma linha em arquivo de efeitos
    em TEMP. P1 deixa pendente e termina; P2 aprova → efeitos=1; P3
    reenvia a mesma aprovação → continua 1; negar em outra sessão → 0.
R-C (controle, não é critério): o mesmo R-A num único objeto em memória,
    sem reinício, só para mostrar a diferença.

Banco e efeitos só em $TEMP. Não cria var/ nem .sqlite3 no repositório.
Usa aurora.runtime.fabrica, nunca o código dos spikes.
Modelo falso apenas; sem chave e sem .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from aurora.runtime.fabrica import montar_runtime
from aurora.runtime.testing import StubLlm

APPROVAL_NAME = "adk_request_confirmation"
TOOL_NAME = "acao_confirmada"
SPECIALIST_NAME = "especialista"
USER_ID = "morador-smoke"
N_RB = 5
REPO = Path(__file__).resolve().parents[3]


def _temp_dir() -> Path:
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    path = root / f"aurora-e13-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _text_message(value: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=value)])


def _confirmation_message(call_id: str, confirmed: bool) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=APPROVAL_NAME,
                    response={"confirmed": confirmed},
                )
            )
        ],
    )


def _event_text(event: Event) -> str:
    content = event.content
    if content is None or not content.parts:
        return ""
    return " ".join(part.text or "" for part in content.parts)


def _event_record(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "author": event.author,
        "timestamp": event.timestamp,
        "text": _event_text(event),
    }


def _print_events(label: str, records: list[dict[str, Any]]) -> None:
    print(f"EVENTS label={label} count={len(records)}")
    for order, record in enumerate(records):
        print(
            f"EVENT order={order} id={record['id']} author={record['author']} "
            f"timestamp={record['timestamp']!r} text={record['text']!r}"
        )


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_effects(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _agente_texto() -> LlmAgent:
    return LlmAgent(
        name="principal",
        description="Agente de smoke só-texto.",
        model=StubLlm(reply="pong"),
    )


def _agente_confirmacao(effects_path: Path) -> LlmAgent:
    def acao_confirmada(item: str, tool_context: ToolContext) -> dict[str, str]:
        del tool_context
        with effects_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"EXECUTION item={item}\n")
        return {"executado": item}

    # Topologia da E9-C: a confirmação só é consumida no especialista.
    specialist = LlmAgent(
        name=SPECIALIST_NAME,
        description="Especialista com tool de confirmação.",
        model=StubLlm(tool_name=TOOL_NAME, tool_args={"item": "reserva-teste"}),
        tools=[FunctionTool(func=acao_confirmada, require_confirmation=True)],
        disallow_transfer_to_parent=True,
    )
    return LlmAgent(
        name="principal",
        description="Agente principal do smoke.",
        model=StubLlm(transfer_to=SPECIALIST_NAME),
        sub_agents=[specialist],
    )


async def _fechar(runtime: Any) -> None:
    await runtime.runner.close()
    await runtime.session_service.close()


async def _rodar(runtime: Any, session_id: str, message: types.Content) -> list[Event]:
    events: list[Event] = []
    async for event in runtime.runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        events.append(event)
    return events


async def _listar_sessao(runtime: Any, app_name: str, session_id: str) -> list[dict[str, Any]]:
    session = await runtime.session_service.get_session(
        app_name=app_name,
        user_id=USER_ID,
        session_id=session_id,
        config=None,
    )
    if session is None:
        raise RuntimeError("sessão ausente após persistência")
    return [_event_record(event) for event in session.events]


def _confirmation_id(events: list[Event]) -> str:
    for event in events:
        for call in event.get_function_calls():
            if call.name == APPROVAL_NAME and call.id:
                return call.id
    raise AssertionError("adk_request_confirmation não emitida")


async def ra_phase1(state_path: Path) -> None:
    state = _read_state(state_path)
    runtime = montar_runtime(
        _agente_texto(),
        session_db=state["db_path"],
        app_name=state["app_name"],
    )
    try:
        await runtime.session_service.create_session(
            app_name=state["app_name"],
            user_id=USER_ID,
            session_id=state["session_id"],
        )
        await _rodar(runtime, state["session_id"], _text_message("ping"))
        records = await _listar_sessao(runtime, state["app_name"], state["session_id"])
        state["events_p1"] = records
        _write_state(state_path, state)
        _print_events("R-A-P1", records)
    finally:
        await _fechar(runtime)


async def ra_phase2(state_path: Path) -> None:
    state = _read_state(state_path)
    runtime = montar_runtime(
        _agente_texto(),
        session_db=state["db_path"],
        app_name=state["app_name"],
    )
    try:
        records = await _listar_sessao(runtime, state["app_name"], state["session_id"])
        _print_events("R-A-P2", records)
        identical = records == state["events_p1"]
        print(f"R-A events_identical={str(identical).lower()}")
        if not identical:
            raise AssertionError("R-A: eventos após reinício diferem")
        await _rodar(runtime, state["session_id"], _text_message("segunda mensagem"))
        after = await _listar_sessao(runtime, state["app_name"], state["session_id"])
        usable = len(after) > len(records)
        print(f"R-A second_message_ok={str(usable).lower()} events_after={len(after)}")
        if not usable:
            raise AssertionError("R-A: sessão não aceitou nova mensagem")
        _print_events("R-A-P2-AFTER", after)
    finally:
        await _fechar(runtime)


async def rb_phase1(state_path: Path) -> None:
    state = _read_state(state_path)
    runtime = montar_runtime(
        _agente_confirmacao(Path(state["effects_path"])),
        session_db=state["db_path"],
        app_name=state["app_name"],
    )
    try:
        await runtime.session_service.create_session(
            app_name=state["app_name"],
            user_id=USER_ID,
            session_id=state["session_id"],
        )
        events = await _rodar(
            runtime,
            state["session_id"],
            _text_message("executar acao"),
        )
        state["confirmation_id"] = _confirmation_id(events)
        _write_state(state_path, state)
        print(
            f"R-B-P1 confirmation_id={state['confirmation_id']} "
            f"effects={len(_read_effects(Path(state['effects_path'])))}"
        )
    finally:
        await _fechar(runtime)


async def rb_respond(state_path: Path, confirmed: bool) -> None:
    state = _read_state(state_path)
    runtime = montar_runtime(
        _agente_confirmacao(Path(state["effects_path"])),
        session_db=state["db_path"],
        app_name=state["app_name"],
    )
    try:
        await _rodar(
            runtime,
            state["session_id"],
            _confirmation_message(state["confirmation_id"], confirmed),
        )
        print(
            f"R-B-RESPOND confirmed={confirmed} "
            f"effects={len(_read_effects(Path(state['effects_path'])))}"
        )
    finally:
        await _fechar(runtime)


async def rc_control(state_path: Path) -> None:
    state = _read_state(state_path)
    runtime = montar_runtime(
        _agente_texto(),
        session_db=state["db_path"],
        app_name=state["app_name"],
    )
    try:
        await runtime.session_service.create_session(
            app_name=state["app_name"],
            user_id=USER_ID,
            session_id=state["session_id"],
        )
        await _rodar(runtime, state["session_id"], _text_message("ping"))
        first = await _listar_sessao(runtime, state["app_name"], state["session_id"])
        _print_events("R-C-BEFORE", first)
        await _rodar(runtime, state["session_id"], _text_message("segunda mensagem"))
        second = await _listar_sessao(runtime, state["app_name"], state["session_id"])
        _print_events("R-C-AFTER", second)
        print(
            "R-C control_same_object=true "
            f"events_before={len(first)} events_after={len(second)} "
            f"grew={str(len(second) > len(first)).lower()}"
        )
    finally:
        await _fechar(runtime)


def _child(mode: str, state_path: Path, decision: str = "") -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "aurora.runtime.smoke_runtime",
        "--mode",
        mode,
        "--state",
        str(state_path),
    ]
    if decision:
        command.extend(["--decision", decision])
    return command


def _run_child(label: str, mode: str, state_path: Path, decision: str = "") -> int:
    command = _child(mode, state_path, decision)
    completed = subprocess.run(
        command,
        cwd=str(REPO),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(f"CHILD_BEGIN label={label}")
    print(f"COMMAND {subprocess.list2cmdline(command)}")
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    print(f"COMMAND_EXIT code={completed.returncode}")
    print(f"CHILD_END label={label}")
    return completed.returncode


def _seed(workdir: Path, prefix: str) -> Path:
    token = uuid4().hex
    state_path = workdir / f"{prefix}-{token}.json"
    _write_state(
        state_path,
        {
            "db_path": str(workdir / f"{prefix}-{token}.sqlite3"),
            "effects_path": str(workdir / f"{prefix}-{token}.effects"),
            "app_name": f"aurora-smoke-{prefix}",
            "session_id": f"session-{token}",
        },
    )
    return state_path


def _cleanup(paths: list[Path]) -> None:
    for path in paths:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            path.rmdir()


def _repo_sqlite_leftovers() -> list[str]:
    leftovers: list[str] = []
    for folder in (REPO, REPO / "src", REPO / "var"):
        if not folder.exists():
            continue
        for path in folder.glob("*.sqlite3"):
            leftovers.append(str(path))
        if folder == REPO / "src":
            leftovers.extend(str(path) for path in folder.rglob("*.sqlite3"))
    if (REPO / "var").exists():
        leftovers.append(str(REPO / "var"))
    return leftovers


def orchestrate() -> int:
    workdir = _temp_dir()
    print(f"SMOKE_STARTED workdir={workdir} n_rb={N_RB} model=stub-local")
    failures: Counter[str] = Counter()
    try:
        ra_state = _seed(workdir, "ra")
        code1 = _run_child("R-A.P1", "ra_phase1", ra_state)
        code2 = _run_child("R-A.P2", "ra_phase2", ra_state)
        ra_ok = code1 == 0 and code2 == 0
        print(f"R-A status={'PASS' if ra_ok else 'FAIL'} codes={code1},{code2}")
        if not ra_ok:
            failures["ra"] += 1

        rb_fail = 0
        for index in range(1, N_RB + 1):
            approve_state = _seed(workdir, f"rb-ok-{index}")
            deny_state = _seed(workdir, f"rb-deny-{index}")
            codes = [
                _run_child(f"R-B.{index}.p1", "rb_phase1", approve_state),
                _run_child(f"R-B.{index}.p2", "rb_respond", approve_state, "approve"),
                _run_child(f"R-B.{index}.p3", "rb_respond", approve_state, "approve"),
                _run_child(f"R-B.{index}.deny.p1", "rb_phase1", deny_state),
                _run_child(f"R-B.{index}.deny.p2", "rb_respond", deny_state, "deny"),
            ]
            approve_effects = len(
                _read_effects(Path(_read_state(approve_state)["effects_path"]))
            )
            deny_effects = len(
                _read_effects(Path(_read_state(deny_state)["effects_path"]))
            )
            passed = (
                all(code == 0 for code in codes)
                and approve_effects == 1
                and deny_effects == 0
            )
            fingerprint = (
                f"codes={codes}|approve_effects={approve_effects}|"
                f"deny_effects={deny_effects}"
            )
            print(
                f"R-B trial={index} status={'PASS' if passed else 'FAIL'} "
                f"approve_effects={approve_effects} deny_effects={deny_effects} "
                f"codes={','.join(map(str, codes))}"
            )
            if not passed:
                rb_fail += 1
                failures[fingerprint] += 1
                if failures[fingerprint] >= 2:
                    print("STOP reason=two_equal_failures scenario=R-B")
                    raise RuntimeError("R-B: duas falhas iguais sem hipótese nova")

        print(f"R-B summary failures={rb_fail}/{N_RB}")

        rc_state = _seed(workdir, "rc")
        rc_code = _run_child("R-C", "rc_control", rc_state)
        print(f"R-C status={'PASS' if rc_code == 0 else 'FAIL'} code={rc_code}")

        leftovers = _repo_sqlite_leftovers()
        print(f"SCOPE leftovers={','.join(leftovers) or '<none>'}")
        smoke_ok = ra_ok and rb_fail == 0 and rc_code == 0 and not leftovers
        print(f"SMOKE status={'PASS' if smoke_ok else 'FAIL'}")
        return 0 if smoke_ok else 1
    finally:
        _cleanup([workdir])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "orchestrate",
            "ra_phase1",
            "ra_phase2",
            "rb_phase1",
            "rb_respond",
            "rc_control",
        ),
        default="orchestrate",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--decision", choices=("approve", "deny"), default="approve")
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> int:
    if args.mode == "orchestrate":
        return orchestrate()
    if args.state is None:
        raise SystemExit("--state é obrigatório nas fases")
    if args.mode == "ra_phase1":
        await ra_phase1(args.state)
    elif args.mode == "ra_phase2":
        await ra_phase2(args.state)
    elif args.mode == "rb_phase1":
        await rb_phase1(args.state)
    elif args.mode == "rb_respond":
        await rb_respond(args.state, confirmed=args.decision == "approve")
    else:
        await rc_control(args.state)
    return 0


def main() -> int:
    args = parse_args()
    if args.mode == "orchestrate":
        return orchestrate()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
