"""Diagnóstico E9-B2: confirmação com DatabaseSessionService + resumability.

O modelo é um stub local determinístico. Não há leitura de ambiente, credenciais
ou chamadas de rede. Cada caso Database usa um SQLite novo e o remove ao final.

Hipóteses e critérios de refutação (declarados antes da execução):

H1 — empate/ordem de timestamp:
  hipótese: a leitura do SQLite reordena eventos próximos e impede o ADK de
  localizar/consumir a primeira aprovação. A ordem funcional esperada é a
  ordem de emissão do ADK: adk_request_confirmation antes do FunctionResponse
  que carrega requested_tool_confirmations.
  refutação: falhas sem empate nem evento fora de ordem e com a confirmação na
  posição esperada, ou passes com a mesma anomalia sem associação numérica.
H2 — dependência de tempo:
  hipótese: a taxa de falha diminui quando cresce a pausa entre pedido e
  aprovação.
  refutação: taxas sem tendência com 0, 0,05, 0,25 e 1 segundo.
H3 — estado do objeto:
  hipótese: estado em memória do Runner/DatabaseSessionService causa a falha.
  refutação: Runner+serviço novos sobre o mesmo DB não reduzem a taxa contra
  Runner+serviço reaproveitados.
H4 — invocation_id/ramo:
  hipótese: a aprovação falha quando não resolve o invocation_id do pedido ou
  entra no ramo de nova invocação em vez do ramo de retomada.
  refutação: nas falhas, IDs coincidem e o wrapper observa o ramo resumed.
Controle:
  InMemorySessionService + resumability deve executar a primeira aprovação uma
  vez; falhas semelhantes enfraquecem uma causa específica do SQLite.

Instrumentação: monkeypatch somente em memória de cinco métodos de Runner para
registrar resolução, runtime e agente escolhido. Cada wrapper delega
imediatamente ao método original e não altera argumentos nem retorno.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Callable, TextIO
from uuid import uuid4

import google.adk
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import PrivateAttr


APPROVAL_FUNCTION_NAME = "adk_request_confirmation"
TOOL_FUNCTION_NAME = "acao_confirmada"
ROOT_AGENT_NAME = "principal"
SPECIALIST_AGENT_NAME = "especialista"
USER_ID = "morador-teste"
N = 20
PAUSES = (0.0, 0.05, 0.25, 1.0)
BASE_DIR = Path(__file__).resolve().parent
RAW_OUTPUT_PATH = BASE_DIR / "RESULTADO-BRUTO-DIAGNOSTICO-DB.txt"


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
        self._file = path.open("w", encoding="utf-8", newline="\n")
        sys.stdout = TeeStream(self._stdout, self._file)
        sys.stderr = TeeStream(self._stderr, self._file)

    @staticmethod
    def line(text: str = "") -> None:
        print(text, flush=True)

    def close(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self._file.close()


OUT: Tee
ACTIVE_TRACE: list[dict[str, str | None]] | None = None


def install_runner_observers() -> None:
    """Instala wrappers transparentes de observação somente neste processo."""
    original_resolve = Runner._resolve_invocation_id
    original_resolve_from_fr = Runner._resolve_invocation_id_from_fr
    original_find_agent = Runner._find_agent_to_run
    original_new = Runner._setup_context_for_new_invocation
    original_resumed = Runner._setup_context_for_resumed_invocation

    def observed_resolve(
        self: Runner,
        session: object,
        new_message: types.Content | None,
        invocation_id: str | None,
    ) -> str | None:
        result = original_resolve(self, session, new_message, invocation_id)
        if ACTIVE_TRACE is not None:
            ACTIVE_TRACE.append(
                {
                    "stage": "resolve",
                    "branch": None,
                    "invocation_id": result,
                    "agent": None,
                }
            )
        return result

    def observed_resolve_from_fr(
        self: Runner, session: object, new_message: types.Content
    ) -> str | None:
        result = original_resolve_from_fr(self, session, new_message)
        if ACTIVE_TRACE is not None:
            ACTIVE_TRACE.append(
                {
                    "stage": "resolve_from_fr",
                    "branch": "node_resumed" if result else "node_new",
                    "invocation_id": result,
                    "agent": None,
                }
            )
        return result

    def observed_find_agent(
        self: Runner, session: object, root_agent: object
    ) -> object:
        result = original_find_agent(self, session, root_agent)
        if ACTIVE_TRACE is not None:
            ACTIVE_TRACE.append(
                {
                    "stage": "find_agent",
                    "branch": "node_runtime",
                    "invocation_id": None,
                    "agent": result.name,
                }
            )
        return result

    async def observed_new(
        self: Runner, *args: object, **kwargs: object
    ) -> object:
        result = await original_new(self, *args, **kwargs)
        if ACTIVE_TRACE is not None:
            ACTIVE_TRACE.append(
                {
                    "stage": "setup",
                    "branch": "new",
                    "invocation_id": result.invocation_id,
                    "agent": result.agent.name if result.agent else None,
                }
            )
        return result

    async def observed_resumed(
        self: Runner, *args: object, **kwargs: object
    ) -> object:
        result = await original_resumed(self, *args, **kwargs)
        if ACTIVE_TRACE is not None:
            ACTIVE_TRACE.append(
                {
                    "stage": "setup",
                    "branch": "resumed",
                    "invocation_id": result.invocation_id,
                    "agent": result.agent.name if result.agent else None,
                }
            )
        return result

    Runner._resolve_invocation_id = observed_resolve
    Runner._resolve_invocation_id_from_fr = observed_resolve_from_fr
    Runner._find_agent_to_run = observed_find_agent
    Runner._setup_context_for_new_invocation = observed_new
    Runner._setup_context_for_resumed_invocation = observed_resumed


@dataclass
class ExecutionProbe:
    count: int = 0
    agents: list[str] = field(default_factory=list)


class StubLlm(BaseLlm):
    """Modelo falso determinístico, sem provedor externo."""

    _role: str = PrivateAttr()

    def __init__(self, role: str) -> None:
        super().__init__(model=f"stub-local-{role}")
        self._role = role

    @staticmethod
    def _function_response_names(request: LlmRequest) -> set[str]:
        names: set[str] = set()
        for content in request.contents:
            for part in content.parts or []:
                if part.function_response and part.function_response.name:
                    names.add(part.function_response.name)
        return names

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        response_names = self._function_response_names(llm_request)
        if self._role == ROOT_AGENT_NAME:
            if APPROVAL_FUNCTION_NAME in response_names:
                yield self._text_response("principal recebeu a retomada")
                return
            yield self._function_call_response(
                "transfer_to_agent", "fc-transfer", {"agent_name": SPECIALIST_AGENT_NAME}
            )
            return
        if APPROVAL_FUNCTION_NAME in response_names or TOOL_FUNCTION_NAME in response_names:
            yield self._text_response("especialista concluiu")
            return
        yield self._function_call_response(
            TOOL_FUNCTION_NAME, "fc-acao", {"item": "reserva-teste"}
        )

    @staticmethod
    def _function_call_response(
        name: str, call_id: str, args: dict[str, str]
    ) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id=call_id, name=name, args=args
                        )
                    )
                ],
            )
        )

    @staticmethod
    def _text_response(text: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)])
        )


@dataclass
class Harness:
    runner: Runner
    service: InMemorySessionService | DatabaseSessionService
    probe: ExecutionProbe
    app_name: str
    session_id: str


@dataclass
class Trial:
    config: str
    index: int
    failed: bool
    error: str
    equal_timestamps: int
    out_of_order: int
    confirmation_order_ok: bool
    request_invocation_id: str
    approval_invocation_id: str
    invocation_match: bool
    resumed_branch: bool
    resumed_agent: str


def build_harness(
    *,
    service: InMemorySessionService | DatabaseSessionService,
    app_name: str,
    session_id: str,
    probe: ExecutionProbe,
) -> Harness:
    def acao_confirmada(item: str, tool_context: ToolContext) -> dict[str, str]:
        probe.count += 1
        active_agent = tool_context._invocation_context.agent
        probe.agents.append(active_agent.name if active_agent else "<none>")
        return {"executado": item}

    specialist = LlmAgent(
        name=SPECIALIST_AGENT_NAME,
        description="Especialista que chama a tool protegida.",
        model=StubLlm(SPECIALIST_AGENT_NAME),
        tools=[FunctionTool(func=acao_confirmada, require_confirmation=True)],
        disallow_transfer_to_parent=True,
    )
    root = LlmAgent(
        name=ROOT_AGENT_NAME,
        description="Agente principal que transfere ao especialista.",
        model=StubLlm(ROOT_AGENT_NAME),
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


def text_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def confirmation_message(confirmation_id: str) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=confirmation_id,
                    name=APPROVAL_FUNCTION_NAME,
                    response={"confirmed": True},
                )
            )
        ],
    )


async def run_message(
    harness: Harness, message: types.Content
) -> tuple[list[object], Exception | None]:
    events: list[object] = []
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


def compact_error(error: Exception | None) -> str:
    if error is None:
        return "none"
    return f"{type(error).__name__}:{str(error).replace(chr(10), ' ')}"


def part_list(items: list[object], attribute: str) -> str:
    values: list[str] = []
    for item in items:
        name = getattr(item, "name", None) or "<none>"
        item_id = getattr(item, "id", None) or "<none>"
        values.append(f"{name}:{item_id}")
    return ",".join(values) or "<none>"


async def snapshot(harness: Harness, label: str) -> tuple[object, dict[str, int | bool]]:
    session = await harness.service.get_session(
        app_name=harness.app_name,
        user_id=USER_ID,
        session_id=harness.session_id,
    )
    if session is None:
        raise AssertionError("sessão não encontrada")
    OUT.line(
        f"SESSION label={label} app={harness.app_name} session={harness.session_id} "
        f"events={len(session.events)}"
    )
    timestamps: list[float] = []
    tool_index = confirmation_index = requested_index = -1
    for order, event in enumerate(session.events):
        calls = event.get_function_calls()
        responses = event.get_function_responses()
        requested = event.actions.requested_tool_confirmations or {}
        timestamps.append(event.timestamp)
        for call in calls:
            if call.name == TOOL_FUNCTION_NAME and tool_index < 0:
                tool_index = order
            if call.name == APPROVAL_FUNCTION_NAME and confirmation_index < 0:
                confirmation_index = order
        if requested and requested_index < 0:
            requested_index = order
        OUT.line(
            f"EVENT order={order} id={event.id} invocation_id={event.invocation_id} "
            f"author={event.author} timestamp={event.timestamp!r} "
            f"function_calls={part_list(calls, 'function_call')} "
            f"function_responses={part_list(responses, 'function_response')} "
            f"requested_tool_confirmations={','.join(sorted(requested)) or '<none>'}"
        )
    equal = sum(a == b for a, b in zip(timestamps, timestamps[1:]))
    out_of_order = sum(a > b for a, b in zip(timestamps, timestamps[1:]))
    order_ok = (
        tool_index >= 0
        and confirmation_index > tool_index
        and requested_index > confirmation_index
    )
    OUT.line(
        f"ORDER label={label} equal_adjacent={equal} out_of_order_adjacent={out_of_order} "
        f"tool_index={tool_index} requested_index={requested_index} "
        f"confirmation_index={confirmation_index} confirmation_order_ok={order_ok}"
    )
    return session, {
        "equal": equal,
        "out_of_order": out_of_order,
        "order_ok": order_ok,
    }


def find_confirmation(events: list[object]) -> tuple[str, str]:
    for event in events:
        for call in event.get_function_calls():
            if call.name == APPROVAL_FUNCTION_NAME:
                if not call.id:
                    raise AssertionError("adk_request_confirmation sem ID")
                return call.id, event.invocation_id
    raise AssertionError("adk_request_confirmation não emitida")


def approval_invocation_id(session: object, confirmation_id: str) -> str:
    for event in reversed(session.events):
        for response in event.get_function_responses():
            if response.id == confirmation_id:
                return event.invocation_id
    return "<not-found>"


def database_path() -> Path:
    return BASE_DIR / f"spike_diag_{uuid4().hex}.db"


def remove_database_files(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


async def run_trial(
    *,
    config: str,
    index: int,
    service_factory: Callable[[], InMemorySessionService | DatabaseSessionService],
    pause: float,
    recreate_for_approval: bool,
    db_path: Path | None,
) -> Trial:
    global ACTIVE_TRACE
    app_name = f"diag_{config.lower()}_{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"
    probe = ExecutionProbe()
    service = service_factory()
    harness = build_harness(
        service=service,
        app_name=app_name,
        session_id=session_id,
        probe=probe,
    )
    trace: list[dict[str, str | None]] = []
    ACTIVE_TRACE = trace
    try:
        await service.create_session(
            app_name=app_name, user_id=USER_ID, session_id=session_id
        )
        initial_events, initial_error = await run_message(
            harness, text_message("execute a ação protegida")
        )
        confirmation_id, request_invocation = find_confirmation(initial_events)
        _, initial_stats = await snapshot(harness, f"{config}.{index}.after_request")
        if pause:
            await asyncio.sleep(pause)

        if recreate_for_approval:
            await harness.runner.close()
            if isinstance(service, DatabaseSessionService):
                await service.close()
            service = service_factory()
            harness = build_harness(
                service=service,
                app_name=app_name,
                session_id=session_id,
                probe=probe,
            )

        trace.clear()
        _, approval_error = await run_message(
            harness, confirmation_message(confirmation_id)
        )
        session, final_stats = await snapshot(
            harness, f"{config}.{index}.after_approval"
        )
        approval_invocation = approval_invocation_id(session, confirmation_id)
        resumed_entries = [
            entry
            for entry in trace
            if (
                (entry["stage"] == "setup" and entry["branch"] == "resumed")
                or entry["branch"] == "node_resumed"
            )
        ]
        new_entries = [
            entry
            for entry in trace
            if entry["stage"] == "setup" and entry["branch"] == "new"
        ]
        selected_agents = [
            entry for entry in trace if entry["stage"] == "find_agent"
        ]
        resumed_agent = (
            str(selected_agents[-1]["agent"]) if selected_agents else "<none>"
        )
        failed = (
            initial_error is not None
            or approval_error is not None
            or probe.count != 1
        )
        OUT.line(
            f"TRIAL config={config} index={index} status={'FAIL' if failed else 'PASS'} "
            f"pause={pause} recreate={recreate_for_approval} counter={probe.count} "
            f"initial_error={compact_error(initial_error)} "
            f"approval_error={compact_error(approval_error)} "
            f"request_invocation_id={request_invocation} "
            f"approval_invocation_id={approval_invocation} "
            f"invocation_match={request_invocation == approval_invocation} "
            f"resumed_branch={bool(resumed_entries)} new_branch={bool(new_entries)} "
            f"resumed_agent={resumed_agent} trace={json.dumps(trace, sort_keys=True)}"
        )
        return Trial(
            config=config,
            index=index,
            failed=failed,
            error=compact_error(initial_error or approval_error),
            equal_timestamps=int(final_stats["equal"]),
            out_of_order=int(final_stats["out_of_order"]),
            confirmation_order_ok=bool(final_stats["order_ok"]),
            request_invocation_id=request_invocation,
            approval_invocation_id=approval_invocation,
            invocation_match=request_invocation == approval_invocation,
            resumed_branch=bool(resumed_entries),
            resumed_agent=resumed_agent,
        )
    finally:
        ACTIVE_TRACE = None
        await harness.runner.close()
        if isinstance(service, DatabaseSessionService):
            await service.close()
        if db_path is not None:
            remove_database_files(db_path)


async def execute_config(
    *,
    config: str,
    pause: float,
    service_kind: str,
    recreate_for_approval: bool = False,
) -> list[Trial]:
    results: list[Trial] = []
    for index in range(1, N + 1):
        db_path = database_path() if service_kind == "database" else None
        if db_path is None:
            factory = InMemorySessionService
        else:
            db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
            factory = lambda url=db_url: DatabaseSessionService(db_url=url)
        results.append(
            await run_trial(
                config=config,
                index=index,
                service_factory=factory,
                pause=pause,
                recreate_for_approval=recreate_for_approval,
                db_path=db_path,
            )
        )
    return results


def summarize(config: str, trials: list[Trial]) -> None:
    failures = sum(trial.failed for trial in trials)
    fail_equal = sum(
        trial.equal_timestamps > 0 for trial in trials if trial.failed
    )
    pass_equal = sum(
        trial.equal_timestamps > 0 for trial in trials if not trial.failed
    )
    fail_out = sum(trial.out_of_order > 0 for trial in trials if trial.failed)
    pass_out = sum(trial.out_of_order > 0 for trial in trials if not trial.failed)
    bad_order = sum(not trial.confirmation_order_ok for trial in trials)
    id_mismatch = sum(not trial.invocation_match for trial in trials)
    not_resumed = sum(not trial.resumed_branch for trial in trials)
    wrong_agent = sum(trial.resumed_agent != SPECIALIST_AGENT_NAME for trial in trials)
    OUT.line(
        f"SUMMARY config={config} failures={failures}/{len(trials)} "
        f"fail_equal_timestamp={fail_equal}/{failures} "
        f"pass_equal_timestamp={pass_equal}/{len(trials) - failures} "
        f"fail_out_of_order={fail_out}/{failures} "
        f"pass_out_of_order={pass_out}/{len(trials) - failures} "
        f"confirmation_bad_order={bad_order}/{len(trials)} "
        f"invocation_id_mismatch={id_mismatch}/{len(trials)} "
        f"not_resumed_branch={not_resumed}/{len(trials)} "
        f"wrong_resumed_agent={wrong_agent}/{len(trials)}"
    )


async def main() -> None:
    global OUT
    started = time.perf_counter()
    OUT = Tee(RAW_OUTPUT_PATH)
    try:
        OUT.line(
            f"SPIKE_STARTED utc={datetime.now(timezone.utc).isoformat()} "
            f"adk={google.adk.__version__} process=single model=stub-local "
            f"network_model_calls=0 N={N}"
        )
        OUT.line(
            "INSTRUMENTATION in_memory_monkeypatch=yes methods="
            "Runner._resolve_invocation_id,"
            "Runner._resolve_invocation_id_from_fr,"
            "Runner._find_agent_to_run,"
            "Runner._setup_context_for_new_invocation,"
            "Runner._setup_context_for_resumed_invocation behavior=delegate_only"
        )
        for line in (__doc__ or "").splitlines()[5:26]:
            OUT.line(f"PREDECLARED {line}")
        install_runner_observers()

        all_results: dict[str, list[Trial]] = {}
        for pause in PAUSES:
            label = str(pause).replace(".", "_")
            config = f"DB_DELAY_{label}"
            all_results[config] = await execute_config(
                config=config, pause=pause, service_kind="database"
            )

        all_results["DB_SAME_OBJECT"] = await execute_config(
            config="DB_SAME_OBJECT", pause=0.0, service_kind="database"
        )
        all_results["DB_NEW_OBJECT"] = await execute_config(
            config="DB_NEW_OBJECT",
            pause=0.0,
            service_kind="database",
            recreate_for_approval=True,
        )

        for pause in PAUSES:
            label = str(pause).replace(".", "_")
            config = f"MEMORY_DELAY_{label}"
            all_results[config] = await execute_config(
                config=config, pause=pause, service_kind="memory"
            )

        OUT.line("FINAL_SUMMARIES_BEGIN")
        for config, trials in all_results.items():
            summarize(config, trials)
        elapsed = time.perf_counter() - started
        leftovers = sorted(path.name for path in BASE_DIR.glob("spike_diag_*.db*"))
        OUT.line(
            f"CLEANUP database_leftovers={','.join(leftovers) or '<none>'}"
        )
        OUT.line(
            f"SPIKE_COMPLETED utc={datetime.now(timezone.utc).isoformat()} "
            f"elapsed_seconds={elapsed:.6f} raw_output={RAW_OUTPUT_PATH.name}"
        )
    finally:
        OUT.close()


if __name__ == "__main__":
    asyncio.run(main())
