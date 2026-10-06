"""E9-B3: mede contornos para o bug de ordem de eventos do ADK 2.9.2.

Hipóteses e critérios registrados antes da execução:

OPÇÃO 1 — agente único
  Hipótese: sem autor divergente, o fallback do roteador escolhe o agente raiz
  correto mesmo se os eventos persistidos forem relidos em ordem invertida.
  Sucesso: 0/40 falhas; refutação: qualquer falha.

OPÇÃO 2 — DatabaseSessionService com timestamps monotônicos
  Hipótese: timestamps estritamente crescentes na ordem de append fazem a
  releitura SQLite preservar a ordem funcional.
  Sucesso: 0/40 falhas; refutação: qualquer falha.

OPÇÃO 3 — versões anteriores
  Hipótese: alguma entre 2.9.1, 2.9.0, 2.8.0 e 2.2.0 não contém a combinação
  emissão confirmação->response + releitura ordenada por timestamp/id.
  Sucesso estático: ao menos uma versão sem a combinação; refutação: todas
  contêm a combinação. Só uma candidata livre autoriza o venv/teste dinâmico.

Para cada opção dinâmica com 0/40, negar, reenviar e ID inexistente são medidos
com N=20 cada. Stub local; nenhum provedor de modelo ou variável de ambiente.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, TextIO, Type
from uuid import uuid4

import google.adk
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.sessions.session import Session
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import PrivateAttr


APPROVAL_NAME = "adk_request_confirmation"
TOOL_NAME = "acao_confirmada"
ROOT_NAME = "principal"
SPECIALIST_NAME = "especialista"
USER_ID = "morador-teste"
N_APPROVAL = 40
N_REGRESSION = 20
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
RAW_PATH = BASE_DIR / "RESULTADO-BRUTO-CONTORNOS.txt"
PIP_PYTHON = Path(sys.base_prefix) / "python.exe"
OLD_ADK_DIR = BASE_DIR / "adk_antigo"
DOWNLOAD_DIR = OLD_ADK_DIR / "dl"
EXTRACT_DIR = OLD_ADK_DIR / "extracted"
OLD_VERSIONS = ("2.9.1", "2.9.0", "2.8.0", "2.2.0")


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


class MonotonicDatabaseSessionService(DatabaseSessionService):
    """Preserva ordem de append ajustando timestamps no objeto Event.

    Usa somente a API pública sobrescrevível ``append_event`` e o campo público
    ``Event.timestamp``. Não acessa métodos privados do ADK. O dicionário é
    deliberadamente local ao objeto e não é proteção de concorrência/reinício.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._last_emission_timestamp: dict[tuple[str, str, str], float] = {}
        self.timestamp_adjustments = 0

    async def append_event(self, session: Session, event: Event) -> Event:
        key = (session.app_name, session.user_id, session.id)
        previous = self._last_emission_timestamp.get(key)
        if previous is not None and event.timestamp <= previous:
            event.timestamp = previous + 0.000002
            self.timestamp_adjustments += 1
        self._last_emission_timestamp[key] = event.timestamp
        return await super().append_event(session=session, event=event)


@dataclass
class Probe:
    count: int = 0


@dataclass
class Harness:
    runner: Runner
    service: DatabaseSessionService
    probe: Probe
    app_name: str
    session_id: str


class StubLlm(BaseLlm):
    _role: str = PrivateAttr()
    _single_agent: bool = PrivateAttr()

    def __init__(self, role: str, *, single_agent: bool = False) -> None:
        super().__init__(model=f"stub-local-{role}")
        self._role = role
        self._single_agent = single_agent

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
        if self._role == ROOT_NAME and not self._single_agent:
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


def build_harness(
    *,
    service: DatabaseSessionService,
    topology: str,
    app_name: str,
    session_id: str,
) -> Harness:
    probe = Probe()

    def acao_confirmada(item: str, tool_context: ToolContext) -> dict[str, str]:
        del tool_context
        probe.count += 1
        return {"executado": item}

    tool = FunctionTool(func=acao_confirmada, require_confirmation=True)
    if topology == "single":
        root = LlmAgent(
            name=ROOT_NAME,
            description="Agente único.",
            model=StubLlm(ROOT_NAME, single_agent=True),
            tools=[tool],
        )
    else:
        specialist = LlmAgent(
            name=SPECIALIST_NAME,
            description="Especialista.",
            model=StubLlm(SPECIALIST_NAME),
            tools=[tool],
            disallow_transfer_to_parent=True,
        )
        root = LlmAgent(
            name=ROOT_NAME,
            description="Agente principal.",
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
        probe=probe,
        app_name=app_name,
        session_id=session_id,
    )


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


def new_db_path(option: str, scenario: str, index: int) -> Path:
    return BASE_DIR / (
        f"spike_b3_{option}_{scenario}_{index}_{uuid4().hex}.db"
    )


def remove_db(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


async def new_harness(
    *,
    option: str,
    scenario: str,
    index: int,
    topology: str,
    service_class: Type[DatabaseSessionService],
) -> tuple[Harness, Path]:
    db_path = new_db_path(option, scenario, index)
    service = service_class(
        db_url=f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )
    harness = build_harness(
        service=service,
        topology=topology,
        app_name=f"b3_{option}_{scenario}_{uuid4().hex}",
        session_id=f"session-{uuid4().hex}",
    )
    await service.create_session(
        app_name=harness.app_name,
        user_id=USER_ID,
        session_id=harness.session_id,
    )
    return harness, db_path


async def close_harness(harness: Harness, db_path: Path) -> None:
    await harness.runner.close()
    await harness.service.close()
    remove_db(db_path)


async def approval_trial(
    *,
    option: str,
    index: int,
    topology: str,
    service_class: Type[DatabaseSessionService],
) -> bool:
    harness, db_path = await new_harness(
        option=option,
        scenario="approve",
        index=index,
        topology=topology,
        service_class=service_class,
    )
    try:
        initial, initial_error = await run_message(
            harness, text_message("execute a ação protegida")
        )
        call_id = confirmation_id(initial)
        _, approval_error = await run_message(
            harness, confirmation_message(call_id, True)
        )
        passed = (
            initial_error is None
            and approval_error is None
            and harness.probe.count == 1
        )
        adjustments = getattr(harness.service, "timestamp_adjustments", 0)
        print(
            f"TRIAL option={option} scenario=approve index={index} "
            f"status={'PASS' if passed else 'FAIL'} counter={harness.probe.count} "
            f"initial_error={error_text(initial_error)} "
            f"approval_error={error_text(approval_error)} "
            f"timestamp_adjustments={adjustments}"
        )
        return passed
    finally:
        await close_harness(harness, db_path)


async def regression_trial(
    *,
    option: str,
    scenario: int,
    index: int,
    topology: str,
    service_class: Type[DatabaseSessionService],
) -> bool:
    harness, db_path = await new_harness(
        option=option,
        scenario=str(scenario),
        index=index,
        topology=topology,
        service_class=service_class,
    )
    try:
        initial, initial_error = await run_message(
            harness, text_message("execute a ação protegida")
        )
        call_id = confirmation_id(initial)
        errors: list[Exception | None] = [initial_error]
        if scenario == 3:
            _, denial_error = await run_message(
                harness, confirmation_message(call_id, False)
            )
            errors.append(denial_error)
            passed = all(error is None for error in errors) and harness.probe.count == 0
        else:
            _, approval_error = await run_message(
                harness, confirmation_message(call_id, True)
            )
            errors.append(approval_error)
            if scenario == 4:
                _, replay_error = await run_message(
                    harness, confirmation_message(call_id, True)
                )
                errors.append(replay_error)
                passed = (
                    all(error is None for error in errors)
                    and harness.probe.count == 1
                )
            else:
                _, unknown_error = await run_message(
                    harness, confirmation_message("id-inexistente", True)
                )
                errors.append(unknown_error)
                passed = (
                    initial_error is None
                    and approval_error is None
                    and isinstance(unknown_error, ValueError)
                    and harness.probe.count == 1
                )
        print(
            f"TRIAL option={option} scenario={scenario} index={index} "
            f"status={'PASS' if passed else 'FAIL'} counter={harness.probe.count} "
            f"errors={';'.join(error_text(error) for error in errors)}"
        )
        return passed
    finally:
        await close_harness(harness, db_path)


async def measure_option(
    *,
    option: str,
    topology: str,
    service_class: Type[DatabaseSessionService],
) -> dict[str, int]:
    approval_passes = [
        await approval_trial(
            option=option,
            index=index,
            topology=topology,
            service_class=service_class,
        )
        for index in range(1, N_APPROVAL + 1)
    ]
    approval_failures = approval_passes.count(False)
    result = {"approve_failures": approval_failures}
    print(
        f"SUMMARY option={option} scenario=approve "
        f"failures={approval_failures}/{N_APPROVAL}"
    )
    if approval_failures:
        print(f"REGRESSION_SKIPPED option={option} reason=approval_not_0_of_40")
        return result

    for scenario in (3, 4, 5):
        passes = [
            await regression_trial(
                option=option,
                scenario=scenario,
                index=index,
                topology=topology,
                service_class=service_class,
            )
            for index in range(1, N_REGRESSION + 1)
        ]
        failures = passes.count(False)
        result[f"scenario_{scenario}_failures"] = failures
        print(
            f"SUMMARY option={option} scenario={scenario} "
            f"failures={failures}/{N_REGRESSION}"
        )
    return result


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    rendered = subprocess.list2cmdline(args)
    print(f"COMMAND {rendered}")
    completed = subprocess.run(
        args,
        cwd=PROJECT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    print(f"COMMAND_EXIT code={completed.returncode}")
    return completed


def matching_wheel(version: str) -> Path:
    matches = sorted(DOWNLOAD_DIR.glob(f"google_adk-{version}-*.whl"))
    if len(matches) != 1:
        raise AssertionError(
            f"esperado 1 wheel google-adk {version}; encontrados={len(matches)}"
        )
    return matches[0]


def source_excerpt(path: Path, start: int, end: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for number in range(max(1, start), min(len(lines), end) + 1):
        print(f"SOURCE path={path.relative_to(BASE_DIR)} line={number} text={lines[number - 1]}")


def first_line(
    lines: list[str], needle: str, *, start: int = 1
) -> int | None:
    return next(
        (
            number
            for number, line in enumerate(lines, start=1)
            if number >= start and needle in line
        ),
        None,
    )


def inspect_version(version: str) -> bool:
    wheel = matching_wheel(version)
    destination = EXTRACT_DIR / version
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(destination)
    flow = destination / "google/adk/flows/llm_flows/base_llm_flow.py"
    database = destination / "google/adk/sessions/database_session_service.py"
    if not flow.exists() or not database.exists():
        raise AssertionError(f"fontes esperadas ausentes em {version}")

    flow_lines = flow.read_text(encoding="utf-8").splitlines()
    db_lines = database.read_text(encoding="utf-8").splitlines()
    async_block = first_line(
        flow_lines, "async def _postprocess_handle_function_calls_async"
    )
    if async_block is None:
        raise AssertionError(f"bloco async de tool ausente em {version}")
    confirmation_yield = first_line(
        flow_lines, "yield tool_confirmation_event", start=async_block
    )
    response_yield = first_line(
        flow_lines, "yield function_response_event", start=async_block
    )
    order_by = first_line(db_lines, "stmt = stmt.order_by")
    if order_by is None:
        order_by = first_line(db_lines, ".order_by(")
    reverse = first_line(db_lines, "reversed(storage_events)")
    nearby = (
        db_lines[order_by - 1 : min(len(db_lines), order_by + 8)]
        if order_by is not None
        else []
    )
    orders_timestamp = any("StorageEvent.timestamp" in line for line in nearby)
    orders_id = any("StorageEvent.id" in line for line in nearby)
    emits_confirmation_first = bool(
        confirmation_yield
        and response_yield
        and confirmation_yield < response_yield
    )
    bug_present = emits_confirmation_first and orders_timestamp and reverse is not None

    print(
        f"STATIC version={version} confirmation_yield={confirmation_yield} "
        f"response_yield={response_yield} async_block={async_block} "
        f"order_by={order_by} reverse={reverse} "
        f"orders_timestamp={orders_timestamp} orders_id={orders_id} "
        f"bug_present={bug_present}"
    )
    if confirmation_yield and response_yield:
        source_excerpt(flow, confirmation_yield - 2, response_yield + 1)
    if order_by:
        source_excerpt(database, order_by - 2, order_by + 7)
    if reverse:
        source_excerpt(database, reverse - 1, reverse + 1)
    return bug_present


def inspect_old_versions() -> dict[str, bool]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    index_result = run_command(
        [str(PIP_PYTHON), "-m", "pip", "index", "versions", "google-adk"]
    )
    if index_result.returncode:
        raise RuntimeError("pip index versions falhou")
    for version in OLD_VERSIONS:
        result = run_command(
            [
                str(PIP_PYTHON),
                "-m",
                "pip",
                "download",
                f"google-adk=={version}",
                "--no-deps",
                "-d",
                str(DOWNLOAD_DIR),
            ]
        )
        if result.returncode:
            raise RuntimeError(f"pip download falhou para {version}")
    return {version: inspect_version(version) for version in OLD_VERSIONS}


async def main() -> None:
    started = time.perf_counter()
    tee = Tee(RAW_PATH)
    try:
        print(
            f"SPIKE_STARTED utc={datetime.now(timezone.utc).isoformat()} "
            f"adk={google.adk.__version__} N_approval={N_APPROVAL} "
            f"N_regression={N_REGRESSION} model=stub-local network_model_calls=0"
        )
        for line in (__doc__ or "").splitlines()[2:22]:
            print(f"PREDECLARED {line}")
        print(
            "OPTION2_INTERNALS private_adk_methods=none "
            "public_override=DatabaseSessionService.append_event "
            "public_field=Event.timestamp increment_microseconds=2"
        )

        option1 = await measure_option(
            option="1_single_agent",
            topology="single",
            service_class=DatabaseSessionService,
        )
        option2 = await measure_option(
            option="2_monotonic_service",
            topology="multi",
            service_class=MonotonicDatabaseSessionService,
        )
        static = inspect_old_versions()
        candidates = [version for version, has_bug in static.items() if not has_bug]
        print(
            f"STATIC_SUMMARY "
            + " ".join(
                f"version_{version}_bug={has_bug}"
                for version, has_bug in static.items()
            )
            + f" candidates={','.join(candidates) or '<none>'}"
        )
        if candidates:
            print(
                "ISOLATED_TEST_REQUIRED candidates="
                f"{','.join(candidates)} action=conditional_venv_not_yet_executed"
            )
        else:
            print("ISOLATED_TEST_SKIPPED reason=no_static_candidate")

        leftovers = sorted(path.name for path in BASE_DIR.glob("spike_b3_*.db*"))
        elapsed = time.perf_counter() - started
        print(
            f"FINAL option1={option1} option2={option2} "
            f"database_leftovers={','.join(leftovers) or '<none>'}"
        )
        print(
            f"SPIKE_COMPLETED utc={datetime.now(timezone.utc).isoformat()} "
            f"elapsed_seconds={elapsed:.6f} raw_output={RAW_PATH.name}"
        )
        if candidates:
            raise SystemExit(2)
    finally:
        tee.close()


if __name__ == "__main__":
    asyncio.run(main())
