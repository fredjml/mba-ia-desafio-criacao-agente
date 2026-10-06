"""E11 — tipo de agente/especialista compatível com resume.

HIPÓTESES E CRITÉRIOS REGISTRADOS ANTES DA PRIMEIRA EXECUÇÃO:

H1: LlmAgent principal com dois especialistas LlmAgent e App resumable retoma
    a confirmação no especialista autor, mesmo em processo novo.
    Sucesso: para CADA especialista, N=20 com 0 falhas na aprovação
    (exatamente 1 efeito), reenvio (continua 1) e negação (0).
    Refutação: qualquer diferença, erro ou autor/executor incorreto.
H2: uma mensagem de texto nova não confirma nem executa a tool pendente, e a
    confirmação original continua utilizável depois.
    Observacional, N=10: registrar efeitos após a mensagem, autores, erros e
    quantas confirmações originais ainda aprovam exatamente uma vez.
H3: um Custom Agent mínimo pode retomar entre dois passos se gravar checkpoint
    explícito em agent_state antes da interrupção.
    Sucesso: N=20, depois de Popen.kill, passo 1 não repete (1 linha) e passo 2
    executa (1 linha), sem erro. Refutação: qualquer contagem diferente/erro.
H4: comparar evidência e custo de código. Proposta esperada, não presumida:
    escolher o tipo com melhor suporte medido a resume, confirmação,
    transferência e menor dependência de API interna/experimental.

Duas falhas iguais sem hipótese nova interrompem a matriz. Modelo StubLlm
determinístico, SQLite local novo por tentativa, zero rede/credencial/.env.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Callable, TextIO
from uuid import uuid4

import google.adk
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.base_agent import BaseAgentState
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import PrivateAttr
from typing_extensions import override

from spike_servico_ordenado import OrderedDatabaseSessionService


APPROVAL_NAME = "adk_request_confirmation"
ROOT_NAME = "principal"
RESERVATIONS = "especialista_reservas"
VISITORS = "especialista_visitantes"
SPECIALISTS = (RESERVATIONS, VISITORS)
USER_ID = "morador-spike"
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
RAW_PATH = BASE_DIR / "RESULTADO-BRUTO-RESUME.txt"
N_H1 = 20
N_H2 = 10
N_H3 = 20


class TeeStream:
    def __init__(self, console: TextIO, artifact: TextIO) -> None:
        self.console = console
        self.artifact = artifact

    def write(self, data: str) -> int:
        self.console.write(data)
        self.artifact.write(data)
        return len(data)

    def flush(self) -> None:
        self.console.flush()
        self.artifact.flush()


class Tee:
    def __init__(self, path: Path) -> None:
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self._artifact = path.open("w", encoding="utf-8", newline="\n")
        sys.stdout = TeeStream(self._stdout, self._artifact)
        sys.stderr = TeeStream(self._stderr, self._artifact)

    def close(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self._artifact.close()


class StubLlm(BaseLlm):
    """Modelo falso determinístico; nunca acessa provedor externo."""

    _role: str = PrivateAttr()

    def __init__(self, role: str) -> None:
        super().__init__(model=f"stub-local-{role}")
        self._role = role

    @staticmethod
    def response_names(request: LlmRequest) -> set[str]:
        return {
            part.function_response.name
            for content in request.contents
            for part in (content.parts or [])
            if part.function_response and part.function_response.name
        }

    @staticmethod
    def all_text(request: LlmRequest) -> str:
        return " ".join(
            part.text or ""
            for content in request.contents
            for part in (content.parts or [])
        ).lower()

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        responses = self.response_names(llm_request)
        if APPROVAL_NAME in responses or responses.intersection(
            {"registrar_reserva", "autorizar_visitante"}
        ):
            yield self.text("concluído")
            return
        if self._role == ROOT_NAME:
            target = (
                VISITORS if "visitante" in self.all_text(llm_request) else RESERVATIONS
            )
            yield self.call(
                "transfer_to_agent",
                "fc-transfer",
                {"agent_name": target},
            )
            return
        if self._role == VISITORS:
            yield self.call(
                "autorizar_visitante",
                "fc-visitante",
                {"item": "visitante-teste"},
            )
            return
        yield self.call(
            "registrar_reserva",
            "fc-reserva",
            {"item": "reserva-teste"},
        )

    @staticmethod
    def call(name: str, call_id: str, args: dict[str, str]) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name=name,
                            id=call_id,
                            args=args,
                        )
                    )
                ],
            )
        )

    @staticmethod
    def text(value: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=value)])
        )


# RESUME_SUPPORT_BEGIN
class CustomStepState(BaseAgentState):
    next_step: int = 1
# RESUME_SUPPORT_END


class TwoStepCustomAgent(BaseAgent):
    effects_path: Path

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # RESUME_SUPPORT_BEGIN
        saved = self._load_agent_state(ctx, CustomStepState)
        next_step = saved.next_step if saved else 1
        # RESUME_SUPPORT_END

        if next_step <= 1:
            write_effect(self.effects_path, "custom", "step=1")
            # RESUME_SUPPORT_BEGIN
            ctx.set_agent_state(
                self.name,
                agent_state=CustomStepState(next_step=2),
            )
            yield self._create_agent_state_event(ctx)
            # RESUME_SUPPORT_END

        write_effect(self.effects_path, "custom", "step=2")
        # RESUME_SUPPORT_BEGIN
        ctx.set_agent_state(self.name, end_of_agent=True)
        yield self._create_agent_state_event(ctx)
        # RESUME_SUPPORT_END


@dataclass
class Harness:
    runner: Runner
    service: OrderedDatabaseSessionService
    app_name: str
    session_id: str


@dataclass
class TrialResult:
    passed: bool
    fingerprint: str
    elapsed: float
    details: dict[str, object]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_effect(path: Path, agent: str, detail: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"EXECUTION utc={utc_now()} agent={agent} {detail}\n")
        stream.flush()


def read_effects(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def text_message(value: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=value)])


def confirmation_message(call_id: str, confirmed: bool) -> types.Content:
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


def error_text(error: BaseException | None) -> str:
    if error is None:
        return "none"
    return f"{type(error).__name__}:{str(error).replace(chr(10), ' ')}"


def make_service(db_path: Path) -> OrderedDatabaseSessionService:
    return OrderedDatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )


def build_llm_harness(state: dict[str, str]) -> Harness:
    effects_path = Path(state["effects_path"])

    def registrar_reserva(item: str, tool_context: ToolContext) -> dict[str, str]:
        del tool_context
        write_effect(
            effects_path,
            RESERVATIONS,
            f"tool=registrar_reserva item={item}",
        )
        return {"executado": item}

    def autorizar_visitante(item: str, tool_context: ToolContext) -> dict[str, str]:
        del tool_context
        write_effect(
            effects_path,
            VISITORS,
            f"tool=autorizar_visitante item={item}",
        )
        return {"executado": item}

    reservations = LlmAgent(
        name=RESERVATIONS,
        description="Especialista de reservas.",
        model=StubLlm(RESERVATIONS),
        tools=[
            FunctionTool(func=registrar_reserva, require_confirmation=True)
        ],
        disallow_transfer_to_parent=True,
    )
    visitors = LlmAgent(
        name=VISITORS,
        description="Especialista de visitantes.",
        model=StubLlm(VISITORS),
        tools=[
            FunctionTool(func=autorizar_visitante, require_confirmation=True)
        ],
        disallow_transfer_to_parent=True,
    )
    root = LlmAgent(
        name=ROOT_NAME,
        description="Agente principal.",
        model=StubLlm(ROOT_NAME),
        sub_agents=[reservations, visitors],
    )
    service = make_service(Path(state["db_path"]))
    app = App(
        name=state["app_name"],
        root_agent=root,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return Harness(
        runner=Runner(app=app, session_service=service),
        service=service,
        app_name=state["app_name"],
        session_id=state["session_id"],
    )


def build_custom_harness(state: dict[str, str]) -> Harness:
    service = make_service(Path(state["db_path"]))
    agent = TwoStepCustomAgent(
        name="especialista_custom",
        description="Custom Agent mínimo de dois passos.",
        effects_path=Path(state["effects_path"]),
    )
    app = App(
        name=state["app_name"],
        root_agent=agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return Harness(
        runner=Runner(app=app, session_service=service),
        service=service,
        app_name=state["app_name"],
        session_id=state["session_id"],
    )


async def close_harness(harness: Harness) -> None:
    await harness.runner.close()
    await harness.service.close()


async def run_message(
    harness: Harness,
    *,
    message: types.Content | None = None,
    invocation_id: str | None = None,
) -> tuple[list[Event], BaseException | None]:
    events: list[Event] = []
    try:
        async for event in harness.runner.run_async(
            user_id=USER_ID,
            session_id=harness.session_id,
            new_message=message,
            invocation_id=invocation_id,
        ):
            events.append(event)
        return events, None
    except BaseException as exc:
        return events, exc


def event_description(event: Event, order: int) -> str:
    calls = ",".join(
        f"{call.name}:{call.id}" for call in event.get_function_calls()
    ) or "<none>"
    responses = ",".join(
        f"{response.name}:{response.id}" for response in event.get_function_responses()
    ) or "<none>"
    requested = (
        ",".join((event.actions.requested_tool_confirmations or {}).keys())
        if event.actions
        else ""
    ) or "<none>"
    agent_state = (
        json.dumps(event.actions.agent_state, sort_keys=True)
        if event.actions and event.actions.agent_state is not None
        else "<none>"
    )
    return (
        f"EVENT order={order} id={event.id} author={event.author} "
        f"timestamp={event.timestamp!r} invocation_id={event.invocation_id} "
        f"calls={calls} responses={responses} requested={requested} "
        f"agent_state={agent_state} end_of_agent={event.actions.end_of_agent}"
    )


async def dump_session(
    service: OrderedDatabaseSessionService,
    state: dict[str, str],
    label: str,
) -> None:
    session = await service.get_session(
        app_name=state["app_name"],
        user_id=USER_ID,
        session_id=state["session_id"],
        config=None,
    )
    if session is None:
        print(f"SESSION label={label} status=missing")
        return
    print(f"SESSION label={label} status=found events={len(session.events)}")
    for order, event in enumerate(session.events):
        print(event_description(event, order))
    print(
        f"SERVICE label={label} adjustments={service.timestamp_adjustments} "
        f"persisted_reads={service.persisted_timestamp_reads}"
    )


def read_state(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def seed_state(scenario: str, index: int, specialist: str = "") -> Path:
    token = uuid4().hex
    state_path = BASE_DIR / f"estado_e11_{scenario}_{index}_{token}.json"
    state = {
        "scenario": scenario,
        "index": str(index),
        "specialist": specialist,
        "db_path": str(BASE_DIR / f"spike_e11_{scenario}_{index}_{token}.db"),
        "effects_path": str(BASE_DIR / f"efeitos_e11_{scenario}_{index}_{token}.log"),
        "app_name": f"e11_{scenario.lower()}_{index}_{token}",
        "session_id": f"session-{token}",
    }
    write_state(state_path, state)
    return state_path


def confirmation_details(events: list[Event]) -> tuple[str, str]:
    for event in events:
        for call in event.get_function_calls():
            if call.name == APPROVAL_NAME and call.id:
                return call.id, event.author
    raise AssertionError("adk_request_confirmation não emitida")


async def llm_phase1(state_path: Path) -> None:
    state = read_state(state_path)
    harness = build_llm_harness(state)
    try:
        await harness.service.create_session(
            app_name=harness.app_name,
            user_id=USER_ID,
            session_id=harness.session_id,
        )
        keyword = (
            "visitante" if state["specialist"] == VISITORS else "reserva"
        )
        events, error = await run_message(
            harness,
            message=text_message(f"pedido de {keyword}"),
        )
        call_id, author = confirmation_details(events)
        state["confirmation_id"] = call_id
        state["confirmation_author"] = author
        state["phase1_error"] = error_text(error)
        write_state(state_path, state)
        print(
            f"PHASE1 specialist={state['specialist']} confirmation_id={call_id} "
            f"confirmation_author={author} error={error_text(error)} "
            f"effects={len(read_effects(Path(state['effects_path'])))}"
        )
        await dump_session(harness.service, state, "llm_phase1_pending")
    finally:
        await close_harness(harness)


async def llm_response(
    state_path: Path,
    *,
    confirmed: bool,
    phase_name: str,
) -> None:
    state = read_state(state_path)
    harness = build_llm_harness(state)
    try:
        events, error = await run_message(
            harness,
            message=confirmation_message(state["confirmation_id"], confirmed),
        )
        authors = ",".join(event.author for event in events) or "<none>"
        state[f"{phase_name}_error"] = error_text(error)
        state[f"{phase_name}_authors"] = authors
        write_state(state_path, state)
        print(
            f"{phase_name.upper()} confirmed={confirmed} authors={authors} "
            f"error={error_text(error)} "
            f"effects={len(read_effects(Path(state['effects_path'])))}"
        )
        await dump_session(harness.service, state, f"{phase_name}_after_response")
    finally:
        await close_harness(harness)


async def llm_new_text(state_path: Path) -> None:
    state = read_state(state_path)
    harness = build_llm_harness(state)
    try:
        events, error = await run_message(
            harness,
            message=text_message("mensagem nova sem aprovar a pendência"),
        )
        authors = ",".join(event.author for event in events) or "<none>"
        state["new_text_error"] = error_text(error)
        state["new_text_authors"] = authors
        state["new_text_effects"] = str(
            len(read_effects(Path(state["effects_path"])))
        )
        write_state(state_path, state)
        print(
            f"NEW_TEXT authors={authors} error={error_text(error)} "
            f"effects={state['new_text_effects']}"
        )
        await dump_session(harness.service, state, "after_new_text")
    finally:
        await close_harness(harness)


async def custom_phase1(state_path: Path) -> None:
    state = read_state(state_path)
    harness = build_custom_harness(state)
    try:
        await harness.service.create_session(
            app_name=harness.app_name,
            user_id=USER_ID,
            session_id=harness.session_id,
        )
        async for event in harness.runner.run_async(
            user_id=USER_ID,
            session_id=harness.session_id,
            new_message=text_message("execute os dois passos"),
        ):
            print(event_description(event, 0))
            if (
                event.actions.agent_state is not None
                and event.actions.agent_state.get("next_step") == 2
            ):
                state["invocation_id"] = event.invocation_id
                state["h3_checkpoint"] = "persisted"
                write_state(state_path, state)
                print(
                    "CUSTOM_CHECKPOINT step1_persisted=true "
                    f"invocation_id={event.invocation_id} "
                    f"effects={len(read_effects(Path(state['effects_path'])))}",
                    flush=True,
                )
                while True:
                    await asyncio.sleep(60)
    finally:
        await close_harness(harness)


async def custom_phase2(state_path: Path) -> None:
    state = read_state(state_path)
    harness = build_custom_harness(state)
    try:
        events, error = await run_message(
            harness,
            invocation_id=state["invocation_id"],
        )
        state["custom_phase2_error"] = error_text(error)
        write_state(state_path, state)
        print(
            f"CUSTOM_RESUME error={error_text(error)} events={len(events)} "
            f"effects={len(read_effects(Path(state['effects_path'])))}"
        )
        for order, event in enumerate(events):
            print(event_description(event, order))
        await dump_session(harness.service, state, "custom_after_resume")
    finally:
        await close_harness(harness)


def child_command(mode: str, state_path: Path, decision: str = "") -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--mode",
        mode,
        "--state",
        str(state_path),
    ]
    if decision:
        command.extend(["--decision", decision])
    return command


def emit_child(label: str, command: list[str], output: str, code: int) -> None:
    print(f"CHILD_BEGIN label={label}")
    print(f"COMMAND {subprocess.list2cmdline(command)}")
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    print(f"COMMAND_EXIT code={code}")
    print(f"CHILD_END label={label}")


def run_child(
    label: str,
    mode: str,
    state_path: Path,
    decision: str = "",
) -> tuple[int, str]:
    command = child_command(mode, state_path, decision)
    completed = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    emit_child(label, command, completed.stdout, completed.returncode)
    return completed.returncode, completed.stdout


def run_killed_custom(state_path: Path, label: str) -> tuple[int, str]:
    command = child_command("custom_phase1", state_path)
    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            state = read_state(state_path)
        except (json.JSONDecodeError, OSError):
            state = {}
        if state.get("h3_checkpoint") == "persisted":
            break
        if process.poll() is not None:
            break
        time.sleep(0.02)
    else:
        process.kill()
        output, _ = process.communicate()
        emit_child(label, command, output, process.returncode)
        raise TimeoutError("Custom Agent não persistiu checkpoint em 30 s")
    if process.poll() is None:
        process.kill()
    output, _ = process.communicate()
    emit_child(label, command, output, process.returncode)
    print(
        f"KILL_PROOF label={label} method=Popen.kill "
        f"checkpoint={read_state(state_path).get('h3_checkpoint')} "
        f"returncode={process.returncode}"
    )
    return process.returncode or 0, output


def cleanup(state_path: Path) -> None:
    state = read_state(state_path) if state_path.exists() else {}
    candidates = [
        Path(state[key])
        for key in ("db_path", "effects_path")
        if state.get(key)
    ]
    if state.get("db_path"):
        candidates.extend(
            [
                Path(state["db_path"] + "-wal"),
                Path(state["db_path"] + "-shm"),
            ]
        )
    candidates.extend(
        [state_path.with_suffix(state_path.suffix + ".tmp"), state_path]
    )
    removed: list[str] = []
    for candidate in candidates:
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate.name)
    print(f"CLEANUP removed={','.join(removed) or '<none>'}")


def enforce_stop(
    scenario: str,
    failures: Counter[str],
    result: TrialResult,
    completed: int,
    expected: int,
) -> None:
    if result.passed:
        return
    failures[result.fingerprint] += 1
    print(
        f"DIAG scenario={scenario} completed={completed}/{expected} "
        f"fingerprint={result.fingerprint} "
        f"occurrences={failures[result.fingerprint]} details={result.details}"
    )
    if failures[result.fingerprint] >= 2:
        print(
            f"STOP scenario={scenario} reason=two_equal_failures "
            f"completed={completed}/{expected}"
        )
        raise RuntimeError(f"{scenario}: duas falhas iguais sem hipótese nova")


def h1_trial(specialist: str, index: int, decision: str) -> TrialResult:
    started = time.perf_counter()
    label = f"H1_{specialist}_{decision}_{index}"
    state_path = seed_state(label, index, specialist)
    try:
        codes = [run_child(f"{label}.phase1", "llm_phase1", state_path)[0]]
        codes.append(
            run_child(
                f"{label}.phase2",
                "llm_response",
                state_path,
                decision,
            )[0]
        )
        if decision == "approve":
            codes.append(
                run_child(
                    f"{label}.phase3",
                    "llm_replay",
                    state_path,
                    "approve",
                )[0]
            )
        state = read_state(state_path)
        effects = read_effects(Path(state["effects_path"]))
        expected = 1 if decision == "approve" else 0
        tool_agent_ok = (
            expected == 0
            or all(f"agent={specialist} " in effect for effect in effects)
        )
        author_ok = state.get("confirmation_author") == specialist
        errors = [
            state.get("phase1_error", "missing"),
            state.get("phase2_error", "missing"),
        ]
        if decision == "approve":
            errors.append(state.get("phase3_error", "missing"))
        passed = (
            all(code == 0 for code in codes)
            and all(error == "none" for error in errors)
            and len(effects) == expected
            and author_ok
            and tool_agent_ok
        )
        details = {
            "specialist": specialist,
            "decision": decision,
            "effects": len(effects),
            "author": state.get("confirmation_author"),
            "tool_agent_ok": tool_agent_ok,
            "errors": errors,
            "codes": codes,
        }
        fingerprint = (
            f"effects={len(effects)}|author_ok={author_ok}|"
            f"tool_agent_ok={tool_agent_ok}|errors={errors}|codes={codes}"
        )
        print(
            f"TRIAL scenario=H1 specialist={specialist} index={index} "
            f"decision={decision} status={'PASS' if passed else 'FAIL'} "
            f"effects={len(effects)} confirmation_author="
            f"{state.get('confirmation_author')} tool_agent_ok={tool_agent_ok} "
            f"errors={';'.join(errors)} codes={','.join(map(str, codes))} "
            f"elapsed_seconds={time.perf_counter() - started:.6f}"
        )
        return TrialResult(
            passed=passed,
            fingerprint=fingerprint,
            elapsed=time.perf_counter() - started,
            details=details,
        )
    finally:
        cleanup(state_path)


def h2_trial(index: int) -> TrialResult:
    started = time.perf_counter()
    specialist = SPECIALISTS[(index - 1) % len(SPECIALISTS)]
    state_path = seed_state("H2", index, specialist)
    try:
        codes = [
            run_child(f"H2.{index}.phase1", "llm_phase1", state_path)[0],
            run_child(f"H2.{index}.new_text", "llm_new_text", state_path)[0],
        ]
        state_after_text = read_state(state_path)
        effects_after_text = int(state_after_text.get("new_text_effects", "-1"))
        codes.append(
            run_child(
                f"H2.{index}.approve_original",
                "llm_response",
                state_path,
                "approve",
            )[0]
        )
        state = read_state(state_path)
        final_effects = len(read_effects(Path(state["effects_path"])))
        usable = (
            state.get("phase2_error") == "none"
            and final_effects == 1
        )
        accidental = effects_after_text != 0
        errors = [
            state.get("phase1_error", "missing"),
            state.get("new_text_error", "missing"),
            state.get("phase2_error", "missing"),
        ]
        passed = all(code == 0 for code in codes)
        details = {
            "specialist": specialist,
            "pending_usable": usable,
            "accidental_execution": accidental,
            "new_text_authors": state.get("new_text_authors"),
            "errors": errors,
            "effects_after_text": effects_after_text,
            "final_effects": final_effects,
        }
        print(
            f"TRIAL scenario=H2 index={index} status=OBSERVED "
            f"specialist={specialist} pending_usable={usable} "
            f"accidental_execution={accidental} "
            f"new_text_authors={state.get('new_text_authors')} "
            f"errors={';'.join(errors)} effects_after_text={effects_after_text} "
            f"final_effects={final_effects} codes={','.join(map(str, codes))} "
            f"elapsed_seconds={time.perf_counter() - started:.6f}"
        )
        return TrialResult(
            passed=passed,
            fingerprint=f"errors={errors}|codes={codes}",
            elapsed=time.perf_counter() - started,
            details=details,
        )
    finally:
        cleanup(state_path)


def h3_trial(index: int) -> TrialResult:
    started = time.perf_counter()
    state_path = seed_state("H3", index)
    try:
        code1, _ = run_killed_custom(state_path, f"H3.{index}.phase1")
        code2, _ = run_child(
            f"H3.{index}.phase2",
            "custom_phase2",
            state_path,
        )
        state = read_state(state_path)
        effects = read_effects(Path(state["effects_path"]))
        step1 = sum("step=1" in line for line in effects)
        step2 = sum("step=2" in line for line in effects)
        error = state.get("custom_phase2_error", "missing")
        passed = code1 != 0 and code2 == 0 and error == "none" and step1 == step2 == 1
        details = {
            "step1": step1,
            "step2": step2,
            "error": error,
            "codes": [code1, code2],
        }
        fingerprint = (
            f"step1={step1}|step2={step2}|error={error}|"
            f"codes_nonzero_zero={code1 != 0},{code2}"
        )
        print(
            f"TRIAL scenario=H3 index={index} "
            f"status={'PASS' if passed else 'FAIL'} step1={step1} step2={step2} "
            f"error={error} codes={code1},{code2} "
            f"elapsed_seconds={time.perf_counter() - started:.6f}"
        )
        return TrialResult(
            passed=passed,
            fingerprint=fingerprint,
            elapsed=time.perf_counter() - started,
            details=details,
        )
    finally:
        cleanup(state_path)


def average(results: list[TrialResult]) -> float:
    return sum(result.elapsed for result in results) / len(results)


def resume_support_loc() -> int:
    source = Path(__file__).read_text(encoding="utf-8").splitlines()
    active = False
    count = 0
    for line in source:
        stripped = line.strip()
        if stripped == "# RESUME_SUPPORT_BEGIN":
            active = True
            continue
        if stripped == "# RESUME_SUPPORT_END":
            active = False
            continue
        if active and stripped and not stripped.startswith("#"):
            count += 1
    return count


async def orchestrate() -> None:
    started = time.perf_counter()
    tee = Tee(RAW_PATH)
    try:
        print(
            f"SPIKE_STARTED utc={utc_now()} adk={google.adk.__version__} "
            f"python={sys.version.split()[0]} model=stub-local "
            f"network_model_calls=0 H1_N={N_H1} H2_N={N_H2} H3_N={N_H3}"
        )
        for line in (__doc__ or "").splitlines()[2:24]:
            print(f"PREDECLARED {line}")
        print(
            "SOURCE_READING "
            "base_agent=google/adk/agents/base_agent.py:83-92,206-247,325-440 "
            "invocation_context=google/adk/agents/invocation_context.py:"
            "267-273,299-384 "
            "resumability_config=google/adk/apps/_configs.py:28-46 "
            "runner=google/adk/runners.py:1030-1073,1173-1259,1390-1457,"
            "1899-1977 router=google/adk/agents/_agent_router.py:81-160"
        )

        h1_results: dict[tuple[str, str], list[TrialResult]] = {}
        for specialist in SPECIALISTS:
            for decision in ("approve", "deny"):
                key = (specialist, decision)
                h1_results[key] = []
                failures: Counter[str] = Counter()
                for index in range(1, N_H1 + 1):
                    result = h1_trial(specialist, index, decision)
                    h1_results[key].append(result)
                    enforce_stop(
                        f"H1_{specialist}_{decision}",
                        failures,
                        result,
                        index,
                        N_H1,
                    )
                failed = sum(not result.passed for result in h1_results[key])
                print(
                    f"SUMMARY scenario=H1 specialist={specialist} "
                    f"decision={decision} N={N_H1} failures={failed}/{N_H1} "
                    f"average_seconds={average(h1_results[key]):.6f}"
                )

        h2_results = [h2_trial(index) for index in range(1, N_H2 + 1)]
        usable = sum(bool(result.details["pending_usable"]) for result in h2_results)
        accidental = sum(
            bool(result.details["accidental_execution"]) for result in h2_results
        )
        error_trials = sum(
            any(error != "none" for error in result.details["errors"])
            for result in h2_results
        )
        author_patterns = Counter(
            str(result.details["new_text_authors"]) for result in h2_results
        )
        print(
            f"SUMMARY scenario=H2 N={N_H2} pending_usable={usable}/{N_H2} "
            f"accidental_execution={accidental}/{N_H2} "
            f"trials_with_errors={error_trials}/{N_H2} "
            f"author_patterns={dict(author_patterns)} "
            f"average_seconds={average(h2_results):.6f}"
        )

        h3_results: list[TrialResult] = []
        h3_failures: Counter[str] = Counter()
        for index in range(1, N_H3 + 1):
            result = h3_trial(index)
            h3_results.append(result)
            enforce_stop(
                "H3",
                h3_failures,
                result,
                index,
                N_H3,
            )
        h3_failed = sum(not result.passed for result in h3_results)
        print(
            f"SUMMARY scenario=H3 N={N_H3} failures={h3_failed}/{N_H3} "
            f"custom_resume_support_loc={resume_support_loc()} "
            f"average_seconds={average(h3_results):.6f}"
        )

        leftovers = sorted(
            path.name
            for pattern in ("spike_e11_*", "efeitos_e11_*", "estado_e11_*")
            for path in BASE_DIR.glob(pattern)
            if path.is_file()
        )
        print(f"FINAL leftovers={','.join(leftovers) or '<none>'}")
        print(
            f"SPIKE_COMPLETED utc={utc_now()} "
            f"elapsed_seconds={time.perf_counter() - started:.6f} "
            f"raw_output={RAW_PATH.name}"
        )
    finally:
        tee.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "orchestrate",
            "llm_phase1",
            "llm_response",
            "llm_replay",
            "llm_new_text",
            "custom_phase1",
            "custom_phase2",
        ),
        default="orchestrate",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--decision", choices=("approve", "deny"), default="approve")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.mode == "orchestrate":
        await orchestrate()
        return
    if args.state is None:
        raise SystemExit("--state é obrigatório nas fases")
    if args.mode == "llm_phase1":
        await llm_phase1(args.state)
    elif args.mode == "llm_response":
        await llm_response(
            args.state,
            confirmed=args.decision == "approve",
            phase_name="phase2",
        )
    elif args.mode == "llm_replay":
        await llm_response(
            args.state,
            confirmed=True,
            phase_name="phase3",
        )
    elif args.mode == "llm_new_text":
        await llm_new_text(args.state)
    elif args.mode == "custom_phase1":
        await custom_phase1(args.state)
    else:
        await custom_phase2(args.state)


if __name__ == "__main__":
    asyncio.run(main())
