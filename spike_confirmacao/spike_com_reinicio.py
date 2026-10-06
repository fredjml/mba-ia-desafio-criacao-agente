"""E9-C: confirmação persistida, reinício real e timestamps monotônicos.

Hipóteses e critérios registrados antes da primeira execução:

R0: o serviço endurecido não causa regressão sem reinício.
    Sucesso: 0/40 falhas, com exatamente uma linha de efeito por tentativa.
R1: a leitura pública do último timestamp persistido permite aprovar em processo
    novo depois de a fase 1 terminar normalmente.
    Sucesso: 0/40 falhas, com exatamente uma linha de efeito.
R2: o commit por evento sobrevive a TerminateProcess logo após a confirmação
    ser comprovadamente relida do banco.
    Sucesso: 0/40 falhas, com exatamente uma linha de efeito.
R3: o ADK persiste o consumo e ignora reenvio em um terceiro processo.
    Sucesso: 0/20 falhas; o arquivo continua com exatamente uma linha.
R4: negar em processo novo não executa a tool.
    Sucesso: 0/20 falhas; o arquivo tem zero linhas.
R5: o DatabaseSessionService original reproduz a perda de ordem após reinício.
    Controle: medir e reportar a taxa de falha em 40 tentativas.
R6: InMemorySessionService perde a sessão entre processos.
    Controle: registrar o erro real e zero linhas de efeito.
R7: duas aprovações idênticas concorrentes medem o risco at-least-once.
    Observacional: registrar uma ou duas linhas e todos os erros em 20 tentativas.

Qualquer falha em R0-R4 refuta o contorno para o respectivo cenário. Duas
falhas iguais sem hipótese nova interrompem a matriz. Modelo stub local:
nenhuma chamada de modelo, rede, credencial, variável de ambiente ou .env.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, TextIO
from uuid import uuid4

import google.adk
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.adk.sessions.session import Session
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import PrivateAttr

from spike_servico_ordenado import OrderedDatabaseSessionService


APPROVAL_NAME = "adk_request_confirmation"
TOOL_NAME = "acao_confirmada"
ROOT_NAME = "principal"
SPECIALIST_NAME = "especialista"
USER_ID = "morador-teste"
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
RAW_PATH = BASE_DIR / "RESULTADO-BRUTO-REINICIO-FINAL.txt"
N_40 = 40
N_20 = 20


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
    """Modelo falso determinístico; não acessa provedor externo."""

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

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        responses = self.response_names(llm_request)
        if APPROVAL_NAME in responses or TOOL_NAME in responses:
            yield self.text("concluído")
            return
        if self._role == ROOT_NAME:
            yield self.call(
                "transfer_to_agent",
                "fc-transfer",
                {"agent_name": SPECIALIST_NAME},
            )
            return
        yield self.call(TOOL_NAME, "fc-acao", {"item": "reserva-teste"})

    @staticmethod
    def call(name: str, call_id: str, args: dict[str, str]) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name=name, id=call_id, args=args
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


SessionService = (
    OrderedDatabaseSessionService
    | DatabaseSessionService
    | InMemorySessionService
)


@dataclass
class Harness:
    runner: Runner
    service: SessionService
    app_name: str
    session_id: str


@dataclass
class TrialResult:
    passed: bool
    effects: int
    errors: tuple[str, ...]
    elapsed: float
    details: str

    @property
    def fingerprint(self) -> str:
        return f"effects={self.effects}|errors={'|'.join(self.errors)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_effect(path: Path, item: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"EXECUTION utc={utc_now()} item={item}\n")
        stream.flush()


def build_harness(
    service: SessionService,
    *,
    app_name: str,
    session_id: str,
    effects_path: Path,
) -> Harness:
    def acao_confirmada(item: str, tool_context: ToolContext) -> dict[str, str]:
        del tool_context
        write_effect(effects_path, item)
        return {"executado": item}

    tool = FunctionTool(func=acao_confirmada, require_confirmation=True)
    specialist = LlmAgent(
        name=SPECIALIST_NAME,
        description="Especialista que chama a tool protegida.",
        model=StubLlm(SPECIALIST_NAME),
        tools=[tool],
        disallow_transfer_to_parent=True,
    )
    root = LlmAgent(
        name=ROOT_NAME,
        description="Agente principal que transfere ao especialista.",
        model=StubLlm(ROOT_NAME),
        sub_agents=[specialist],
    )
    app = App(
        name=app_name,
        root_agent=root,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return Harness(
        runner=Runner(app=app, session_service=service),
        service=service,
        app_name=app_name,
        session_id=session_id,
    )


def make_service(kind: str, db_path: Path) -> SessionService:
    if kind == "memory":
        return InMemorySessionService()
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    if kind == "ordered":
        return OrderedDatabaseSessionService(db_url=db_url)
    if kind == "original":
        return DatabaseSessionService(db_url=db_url)
    raise ValueError(f"serviço desconhecido: {kind}")


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


async def run_message(
    harness: Harness, message: types.Content
) -> tuple[list[Event], Exception | None]:
    events: list[Event] = []
    try:
        async for event in harness.runner.run_async(
            user_id=USER_ID,
            session_id=harness.session_id,
            new_message=message,
        ):
            events.append(event)
        return events, None
    except Exception as exc:
        return events, exc


def confirmation_id(events: list[Event]) -> str:
    for event in events:
        for call in event.get_function_calls():
            if call.name == APPROVAL_NAME and call.id:
                return call.id
    raise AssertionError("adk_request_confirmation não emitida")


def error_text(error: Exception | None) -> str:
    if error is None:
        return "none"
    return f"{type(error).__name__}:{str(error).replace(chr(10), ' ')}"


def effect_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)


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
    return (
        f"EVENT order={order} id={event.id} author={event.author} "
        f"timestamp={event.timestamp!r} invocation_id={event.invocation_id} "
        f"calls={calls} responses={responses} requested={requested}"
    )


async def dump_session(service: SessionService, state: dict[str, str], label: str) -> None:
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
        f"SERVICE label={label} adjustments="
        f"{getattr(service, 'timestamp_adjustments', 0)} persisted_reads="
        f"{getattr(service, 'persisted_timestamp_reads', 0)}"
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


async def close_harness(harness: Harness) -> None:
    await harness.runner.close()
    await harness.service.close()


async def phase1(state_path: Path, wait_for_kill: bool) -> None:
    state = read_state(state_path)
    service = make_service(state["service"], Path(state["db_path"]))
    harness = build_harness(
        service,
        app_name=state["app_name"],
        session_id=state["session_id"],
        effects_path=Path(state["effects_path"]),
    )
    close_normally = True
    try:
        await service.create_session(
            app_name=harness.app_name,
            user_id=USER_ID,
            session_id=harness.session_id,
        )
        events, error = await run_message(
            harness, text_message("execute a ação protegida")
        )
        call_id = confirmation_id(events)
        await dump_session(service, state, "phase1_pending")
        persisted = await service.get_session(
            app_name=state["app_name"],
            user_id=USER_ID,
            session_id=state["session_id"],
            config=None,
        )
        if persisted is None or not any(
            call.name == APPROVAL_NAME and call.id == call_id
            for event in persisted.events
            for call in event.get_function_calls()
        ):
            raise AssertionError("confirmação não foi relida antes do sinal")
        state["confirmation_id"] = call_id
        state["phase1_error"] = error_text(error)
        state["phase1_status"] = "pending_persisted"
        write_state(state_path, state)
        print(
            f"PHASE1 status=pending_persisted error={error_text(error)} "
            f"effects={effect_count(Path(state['effects_path']))}"
        )
        if wait_for_kill:
            close_normally = False
            print("PHASE1_WAITING_FOR_KILL confirmed_persisted=true", flush=True)
            while True:
                await asyncio.sleep(60)
    finally:
        if close_normally:
            await close_harness(harness)


async def response_phase(
    state_path: Path, *, confirmed: bool, phase_name: str
) -> None:
    state = read_state(state_path)
    service = make_service(state["service"], Path(state["db_path"]))
    harness = build_harness(
        service,
        app_name=state["app_name"],
        session_id=state["session_id"],
        effects_path=Path(state["effects_path"]),
    )
    try:
        events, error = await run_message(
            harness,
            confirmation_message(state["confirmation_id"], confirmed),
        )
        state[f"{phase_name}_error"] = error_text(error)
        state[f"{phase_name}_event_count"] = str(len(events))
        write_state(state_path, state)
        print(
            f"{phase_name.upper()} confirmed={confirmed} "
            f"error={error_text(error)} events={len(events)} "
            f"effects={effect_count(Path(state['effects_path']))}"
        )
        await dump_session(service, state, f"{phase_name}_after_response")
    finally:
        await close_harness(harness)


def created_paths(
    scenario: str, index: int
) -> tuple[Path, Path, Path]:
    token = uuid4().hex
    db = BASE_DIR / f"spike_e9c_{scenario}_{index}_{token}.db"
    effects = BASE_DIR / f"efeitos_{scenario}_{index}_{token}.log"
    state = BASE_DIR / f"estado_{scenario}_{index}_{token}.json"
    return db, effects, state


def seed_state(
    scenario: str, index: int, service: str
) -> tuple[dict[str, str], Path]:
    db, effects, state_path = created_paths(scenario, index)
    state = {
        "scenario": scenario,
        "index": str(index),
        "service": service,
        "db_path": str(db),
        "effects_path": str(effects),
        "app_name": f"e9c_{scenario.lower()}_{index}_{uuid4().hex}",
        "session_id": f"session-{uuid4().hex}",
    }
    write_state(state_path, state)
    return state, state_path


def child_command(
    mode: str,
    state_path: Path,
    *,
    decision: str | None = None,
    wait_for_kill: bool = False,
) -> list[str]:
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
    if wait_for_kill:
        command.append("--wait-for-kill")
    return command


def emit_child_output(label: str, command: list[str], output: str, code: int) -> None:
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
    *,
    decision: str | None = None,
) -> tuple[int, str]:
    command = child_command(mode, state_path, decision=decision)
    completed = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    emit_child_output(label, command, completed.stdout, completed.returncode)
    return completed.returncode, completed.stdout


def run_killed_phase1(label: str, state_path: Path) -> tuple[int, str]:
    command = child_command(
        "phase1", state_path, wait_for_kill=True
    )
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
        if state.get("phase1_status") == "pending_persisted":
            break
        if process.poll() is not None:
            break
        time.sleep(0.02)
    else:
        process.kill()
        output, _ = process.communicate()
        emit_child_output(label, command, output, process.returncode)
        raise TimeoutError("fase1 não sinalizou confirmação persistida em 30 s")

    if process.poll() is None:
        process.kill()
    output, _ = process.communicate()
    emit_child_output(label, command, output, process.returncode)
    print(
        f"KILL_PROOF label={label} method=Popen.kill "
        f"phase1_status={read_state(state_path).get('phase1_status')} "
        f"returncode={process.returncode}"
    )
    return process.returncode or 0, output


def cleanup_trial(state_path: Path) -> None:
    state = read_state(state_path) if state_path.exists() else {}
    candidates = [
        Path(value)
        for key, value in state.items()
        if key in {"db_path", "effects_path"}
    ]
    candidates.extend(
        [
            Path(str(state.get("db_path", "")) + "-wal"),
            Path(str(state.get("db_path", "")) + "-shm"),
            state_path.with_suffix(state_path.suffix + ".tmp"),
            state_path,
        ]
    )
    removed: list[str] = []
    for candidate in candidates:
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate.name)
    print(f"CLEANUP removed={','.join(removed) or '<none>'}")


async def direct_trial(
    scenario: str,
    index: int,
    *,
    concurrent: bool = False,
) -> TrialResult:
    started = time.perf_counter()
    state, state_path = seed_state(scenario, index, "ordered")
    service = make_service("ordered", Path(state["db_path"]))
    harness = build_harness(
        service,
        app_name=state["app_name"],
        session_id=state["session_id"],
        effects_path=Path(state["effects_path"]),
    )
    errors: list[str] = []
    try:
        await service.create_session(
            app_name=harness.app_name,
            user_id=USER_ID,
            session_id=harness.session_id,
        )
        initial, initial_error = await run_message(
            harness, text_message("execute a ação protegida")
        )
        call_id = confirmation_id(initial)
        errors.append(error_text(initial_error))
        if concurrent:
            responses = await asyncio.gather(
                run_message(harness, confirmation_message(call_id, True)),
                run_message(harness, confirmation_message(call_id, True)),
            )
            errors.extend(error_text(error) for _, error in responses)
        else:
            _, approval_error = await run_message(
                harness, confirmation_message(call_id, True)
            )
            errors.append(error_text(approval_error))
        await dump_session(service, state, f"{scenario}_{index}_final")
        effects = effect_count(Path(state["effects_path"]))
        passed = concurrent or (all(error == "none" for error in errors) and effects == 1)
        elapsed = time.perf_counter() - started
        print(
            f"TRIAL scenario={scenario} index={index} "
            f"status={'OBSERVED' if concurrent else ('PASS' if passed else 'FAIL')} "
            f"effects={effects} errors={';'.join(errors)} "
            f"elapsed_seconds={elapsed:.6f}"
        )
        return TrialResult(
            passed=passed,
            effects=effects,
            errors=tuple(errors),
            elapsed=elapsed,
            details=f"concurrent={concurrent}",
        )
    finally:
        await close_harness(harness)
        cleanup_trial(state_path)


def restart_trial(
    scenario: str,
    index: int,
    *,
    service: str,
    killed: bool = False,
    decision: str = "approve",
    replay: bool = False,
) -> TrialResult:
    started = time.perf_counter()
    state, state_path = seed_state(scenario, index, service)
    process_codes: list[int] = []
    try:
        if killed:
            code1, _ = run_killed_phase1(f"{scenario}.{index}.phase1", state_path)
        else:
            code1, _ = run_child(
                f"{scenario}.{index}.phase1", "phase1", state_path
            )
        process_codes.append(code1)
        code2, _ = run_child(
            f"{scenario}.{index}.phase2",
            "phase2",
            state_path,
            decision=decision,
        )
        process_codes.append(code2)
        if replay:
            code3, _ = run_child(
                f"{scenario}.{index}.phase3",
                "phase3",
                state_path,
                decision="approve",
            )
            process_codes.append(code3)

        final_state = read_state(state_path)
        effects = effect_count(Path(state["effects_path"]))
        errors = tuple(
            final_state.get(key, "missing")
            for key in ("phase1_error", "phase2_error")
        ) + (
            (final_state.get("phase3_error", "missing"),) if replay else ()
        )
        expected_effects = 0 if decision == "deny" or service == "memory" else 1
        if service == "memory":
            passed = effects == 0 and errors[1] != "none"
        else:
            process_exit_ok = (
                process_codes[0] != 0
                and all(code == 0 for code in process_codes[1:])
                if killed
                else all(code == 0 for code in process_codes)
            )
            passed = (
                process_exit_ok
                and all(error == "none" for error in errors)
                and effects == expected_effects
            )
        elapsed = time.perf_counter() - started
        print(
            f"TRIAL scenario={scenario} index={index} "
            f"status={'PASS' if passed else 'FAIL'} service={service} "
            f"killed={killed} decision={decision} replay={replay} "
            f"effects={effects} errors={';'.join(errors)} "
            f"process_codes={','.join(map(str, process_codes))} "
            f"elapsed_seconds={elapsed:.6f}"
        )
        return TrialResult(
            passed=passed,
            effects=effects,
            errors=errors,
            elapsed=elapsed,
            details=(
                f"service={service},killed={killed},decision={decision},"
                f"replay={replay},codes={process_codes}"
            ),
        )
    finally:
        cleanup_trial(state_path)


async def measure_required(
    scenario: str,
    count: int,
    factory: object,
) -> list[TrialResult]:
    results: list[TrialResult] = []
    failure_fingerprints: dict[str, int] = {}
    for index in range(1, count + 1):
        produced = factory(index)  # type: ignore[operator]
        result = await produced if inspect.isawaitable(produced) else produced
        results.append(result)
        if not result.passed:
            occurrences = failure_fingerprints.get(result.fingerprint, 0) + 1
            failure_fingerprints[result.fingerprint] = occurrences
            print(
                f"DIAG scenario={scenario} index={index} "
                f"fingerprint={result.fingerprint} occurrences={occurrences}"
            )
            if occurrences >= 2:
                print(
                    f"STOP scenario={scenario} reason=two_equal_failures "
                    f"completed={len(results)}/{count}"
                )
                raise RuntimeError(
                    f"{scenario}: duas falhas iguais sem hipótese nova"
                )
    failures = sum(not result.passed for result in results)
    average = sum(result.elapsed for result in results) / len(results)
    print(
        f"SUMMARY scenario={scenario} N={len(results)} failures={failures}/{count} "
        f"average_seconds={average:.6f}"
    )
    return results


def summarize_observational(
    scenario: str, results: list[TrialResult]
) -> None:
    one = sum(result.effects == 1 for result in results)
    two = sum(result.effects == 2 for result in results)
    other = len(results) - one - two
    error_trials = sum(any(error != "none" for error in result.errors) for result in results)
    average = sum(result.elapsed for result in results) / len(results)
    print(
        f"SUMMARY scenario={scenario} N={len(results)} effects_1={one} "
        f"effects_2={two} effects_other={other} trials_with_errors={error_trials} "
        f"average_seconds={average:.6f}"
    )


async def orchestrate() -> None:
    started = time.perf_counter()
    tee = Tee(RAW_PATH)
    try:
        print(
            f"SPIKE_STARTED utc={utc_now()} adk={google.adk.__version__} "
            f"python={sys.version.split()[0]} model=stub-local "
            f"network_model_calls=0 N40={N_40} N20={N_20}"
        )
        for line in (__doc__ or "").splitlines()[2:29]:
            print(f"PREDECLARED {line}")
        print(
            "SERVICE_CONTRACT public_apis=get_session,append_event,"
            "Session.events,Event.timestamp internal_apis=none "
            "lock_scope=read_adjust_append writer_premise=single_process_per_session "
            "increment_microseconds=2"
        )

        await measure_required(
            "R0",
            N_40,
            lambda index: direct_trial("R0", index),
        )
        await measure_required(
            "R1",
            N_40,
            lambda index: restart_trial(
                "R1", index, service="ordered"
            ),
        )
        await measure_required(
            "R2",
            N_40,
            lambda index: restart_trial(
                "R2", index, service="ordered", killed=True
            ),
        )
        await measure_required(
            "R3",
            N_20,
            lambda index: restart_trial(
                "R3", index, service="ordered", replay=True
            ),
        )
        await measure_required(
            "R4",
            N_20,
            lambda index: restart_trial(
                "R4", index, service="ordered", decision="deny"
            ),
        )

        baseline = [
            restart_trial("R5", index, service="original")
            for index in range(1, N_40 + 1)
        ]
        baseline_failures = sum(not result.passed for result in baseline)
        print(
            f"SUMMARY scenario=R5 N={N_40} failures={baseline_failures}/{N_40} "
            f"failure_rate_percent={baseline_failures / N_40 * 100:.2f} "
            f"average_seconds={sum(r.elapsed for r in baseline) / N_40:.6f}"
        )

        memory = restart_trial("R6", 1, service="memory")
        print(
            f"SUMMARY scenario=R6 N=1 expected_loss={'PASS' if memory.passed else 'FAIL'} "
            f"effects={memory.effects} errors={';'.join(memory.errors)} "
            f"average_seconds={memory.elapsed:.6f}"
        )

        concurrent = [
            await direct_trial("R7", index, concurrent=True)
            for index in range(1, N_20 + 1)
        ]
        summarize_observational("R7", concurrent)

        elapsed = time.perf_counter() - started
        leftovers = sorted(
            path.name
            for pattern in ("spike_e9c_*", "efeitos_*", "estado_*")
            for path in BASE_DIR.glob(pattern)
            if path.is_file()
        )
        print(f"FINAL leftovers={','.join(leftovers) or '<none>'}")
        print(
            f"SPIKE_COMPLETED utc={utc_now()} elapsed_seconds={elapsed:.6f} "
            f"raw_output={RAW_PATH.name}"
        )
    finally:
        tee.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("orchestrate", "phase1", "phase2", "phase3"),
        default="orchestrate",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--decision", choices=("approve", "deny"), default="approve")
    parser.add_argument("--wait-for-kill", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.mode == "orchestrate":
        await orchestrate()
        return
    if args.state is None:
        raise SystemExit("--state é obrigatório nas fases")
    if args.mode == "phase1":
        await phase1(args.state, args.wait_for_kill)
    elif args.mode == "phase2":
        await response_phase(
            args.state,
            confirmed=args.decision == "approve",
            phase_name="phase2",
        )
    else:
        await response_phase(
            args.state,
            confirmed=True,
            phase_name="phase3",
        )


if __name__ == "__main__":
    asyncio.run(main())
