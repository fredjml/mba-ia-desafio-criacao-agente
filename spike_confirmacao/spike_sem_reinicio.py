"""Spike E9-B: confirmação de tool sem reinício e sem modelo remoto.

O ``StubLlm`` implementa ``BaseLlm`` inteiramente em memória. O stub do agente
principal emite deterministicamente a chamada ``transfer_to_agent``; o stub do
especialista emite a chamada da tool protegida. Depois de uma FunctionResponse,
ambos emitem apenas texto terminal. Nenhum método deste arquivo acessa rede,
credenciais ou variáveis de ambiente.

As verificações focais estão declaradas em ``expect`` e são avaliadas sem
interromper a matriz: uma falha é resultado do spike, enquanto erro inesperado
do próprio executor ainda faz o processo falhar.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator
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
DB_PATH = Path(__file__).with_name("spike.db").resolve()


@dataclass
class ExecutionProbe:
    count: int = 0
    agents: list[str] = field(default_factory=list)


class StubLlm(BaseLlm):
    """Modelo falso determinístico que nunca chama um provedor externo."""

    _role: str = PrivateAttr()
    _calls: list[str] = PrivateAttr(default_factory=list)

    def __init__(self, role: str) -> None:
        super().__init__(model=f"stub-local-{role}")
        self._role = role

    @property
    def calls(self) -> list[str]:
        return list(self._calls)

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
        self._calls.append(self._role)

        if self._role == ROOT_AGENT_NAME:
            if APPROVAL_FUNCTION_NAME in response_names:
                yield self._text_response("principal recebeu a retomada")
                return
            yield self._function_call_response(
                name="transfer_to_agent",
                call_id="fc-transfer",
                args={"agent_name": SPECIALIST_AGENT_NAME},
            )
            return

        if (
            APPROVAL_FUNCTION_NAME in response_names
            or TOOL_FUNCTION_NAME in response_names
        ):
            yield self._text_response("especialista concluiu")
            return

        yield self._function_call_response(
            name=TOOL_FUNCTION_NAME,
            call_id="fc-acao",
            args={"item": "reserva-teste"},
        )

    @staticmethod
    def _function_call_response(
        *, name: str, call_id: str, args: dict[str, str]
    ) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id=call_id,
                            name=name,
                            args=args,
                        )
                    )
                ],
            )
        )

    @staticmethod
    def _text_response(text: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=text)],
            )
        )


@dataclass
class Harness:
    runner: Runner
    session_service: InMemorySessionService | DatabaseSessionService
    probe: ExecutionProbe
    root_model: StubLlm
    specialist_model: StubLlm
    app_name: str
    session_id: str


def build_harness(
    *,
    session_service: InMemorySessionService | DatabaseSessionService,
    service_name: str,
    resumable: bool,
    case_name: str,
) -> Harness:
    probe = ExecutionProbe()

    def acao_confirmada(item: str, tool_context: ToolContext) -> dict[str, str]:
        """Executa a ação protegida somente após confirmação estruturada."""
        probe.count += 1
        active_agent = tool_context._invocation_context.agent
        probe.agents.append(active_agent.name if active_agent else "<none>")
        return {"executado": item}

    tool = FunctionTool(func=acao_confirmada, require_confirmation=True)
    root_model = StubLlm(ROOT_AGENT_NAME)
    specialist_model = StubLlm(SPECIALIST_AGENT_NAME)
    specialist = LlmAgent(
        name=SPECIALIST_AGENT_NAME,
        description="Especialista que chama a tool protegida.",
        model=specialist_model,
        tools=[tool],
        disallow_transfer_to_parent=True,
    )
    root = LlmAgent(
        name=ROOT_AGENT_NAME,
        description="Agente principal que transfere ao especialista.",
        model=root_model,
        sub_agents=[specialist],
    )
    app_name = (
        f"spike_{service_name}_{'resume' if resumable else 'sem_resume'}_"
        f"{case_name}"
    )
    app = App(
        name=app_name,
        root_agent=root,
        resumability_config=ResumabilityConfig(is_resumable=resumable),
    )
    return Harness(
        runner=Runner(app=app, session_service=session_service),
        session_service=session_service,
        probe=probe,
        root_model=root_model,
        specialist_model=specialist_model,
        app_name=app_name,
        session_id=f"session-{uuid4().hex}",
    )


async def prepare_session(harness: Harness) -> None:
    await harness.session_service.create_session(
        app_name=harness.app_name,
        user_id=USER_ID,
        session_id=harness.session_id,
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
    except Exception as exc:  # Resultado bruto é parte do spike.
        return events, exc


def text_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def confirmation_message(
    confirmation_id: str, *, confirmed: bool
) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=confirmation_id,
                    name=APPROVAL_FUNCTION_NAME,
                    response={"confirmed": confirmed},
                )
            )
        ],
    )


def find_confirmation(events: list[object]) -> tuple[str, str]:
    for event in events:
        for function_call in event.get_function_calls():
            if function_call.name == APPROVAL_FUNCTION_NAME:
                if not function_call.id:
                    raise AssertionError("adk_request_confirmation sem ID")
                return function_call.id, event.author
    raise AssertionError("adk_request_confirmation não foi emitida")


def describe_error(
    error: Exception | None, confirmation_id: str | None = None
) -> str:
    if error is None:
        return "none"
    message = str(error).replace("\n", " ")
    if confirmation_id:
        message = message.replace(confirmation_id, "<confirmation-id>")
    return f"{type(error).__name__}:{message}"


def emit(
    *,
    service: str,
    resumable: bool,
    scenario: int,
    status: str,
    details: str,
) -> None:
    print(
        f"RESULT service={service} resumable={'yes' if resumable else 'no'} "
        f"scenario={scenario} status={status} {details}"
    )


def expect(condition: bool) -> str:
    """Retorna PASS/FAIL sem ocultar as demais observações da matriz."""
    try:
        assert condition
    except AssertionError:
        return "FAIL"
    return "PASS"


async def exercise_combination(
    *,
    session_service: InMemorySessionService | DatabaseSessionService,
    service_name: str,
    resumable: bool,
) -> None:
    approval = build_harness(
        session_service=session_service,
        service_name=service_name,
        resumable=resumable,
        case_name="aprovacao",
    )
    await prepare_session(approval)
    initial_events, initial_error = await run_message(
        approval, text_message("execute a ação protegida")
    )
    confirmation_id, confirmation_author = find_confirmation(initial_events)
    emit(
        service=service_name,
        resumable=resumable,
        scenario=1,
        status=expect(initial_error is None and approval.probe.count == 0),
        details=(
            f"counter={approval.probe.count} "
            f"confirmation_author={confirmation_author} "
            f"error={describe_error(initial_error, confirmation_id)}"
        ),
    )

    root_calls_before = len(approval.root_model.calls)
    specialist_calls_before = len(approval.specialist_model.calls)
    _, approval_error = await run_message(
        approval, confirmation_message(confirmation_id, confirmed=True)
    )
    resumed_models = (
        approval.root_model.calls[root_calls_before:]
        + approval.specialist_model.calls[specialist_calls_before:]
    )
    emit(
        service=service_name,
        resumable=resumable,
        scenario=2,
        status=expect(approval_error is None and approval.probe.count == 1),
        details=(
            f"counter={approval.probe.count} "
            f"error={describe_error(approval_error, confirmation_id)}"
        ),
    )

    count_before_replay = approval.probe.count
    _, replay_error = await run_message(
        approval, confirmation_message(confirmation_id, confirmed=True)
    )
    replay_delta = approval.probe.count - count_before_replay
    emit(
        service=service_name,
        resumable=resumable,
        scenario=4,
        status="OBSERVED",
        details=(
            f"counter_before={count_before_replay} "
            f"counter_after={approval.probe.count} delta={replay_delta} "
            f"error={describe_error(replay_error, confirmation_id)}"
        ),
    )

    _, unknown_error = await run_message(
        approval, confirmation_message("id-inexistente", confirmed=True)
    )
    emit(
        service=service_name,
        resumable=resumable,
        scenario=5,
        status="OBSERVED",
        details=(
            f"counter={approval.probe.count} "
            f"error={describe_error(unknown_error)}"
        ),
    )
    emit(
        service=service_name,
        resumable=resumable,
        scenario=6,
        status="OBSERVED",
        details=(
            f"confirmation_author={confirmation_author} "
            f"resumed_models={','.join(resumed_models) or '<none>'} "
            f"tool_execution_agents="
            f"{','.join(approval.probe.agents) or '<none>'}"
        ),
    )
    await approval.runner.close()

    denial = build_harness(
        session_service=session_service,
        service_name=service_name,
        resumable=resumable,
        case_name="negacao",
    )
    await prepare_session(denial)
    denial_initial_events, denial_initial_error = await run_message(
        denial, text_message("execute a ação protegida")
    )
    denial_id, _ = find_confirmation(denial_initial_events)
    _, denial_error = await run_message(
        denial, confirmation_message(denial_id, confirmed=False)
    )
    emit(
        service=service_name,
        resumable=resumable,
        scenario=3,
        status=expect(
            denial_initial_error is None
            and denial_error is None
            and denial.probe.count == 0
        ),
        details=(
            f"counter={denial.probe.count} "
            f"error={describe_error(denial_error, denial_id)}"
        ),
    )
    await denial.runner.close()


async def main() -> None:
    print(
        f"SPIKE adk={google.adk.__version__} process=single "
        "model=stub-local network_model_calls=0"
    )

    memory_service = InMemorySessionService()
    await exercise_combination(
        session_service=memory_service,
        service_name="memory",
        resumable=False,
    )
    await exercise_combination(
        session_service=memory_service,
        service_name="memory",
        resumable=True,
    )

    database_service = DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
    )
    try:
        await exercise_combination(
            session_service=database_service,
            service_name="database",
            resumable=False,
        )
        await exercise_combination(
            session_service=database_service,
            service_name="database",
            resumable=True,
        )
    finally:
        await database_service.close()

    print(f"ARTIFACT database={DB_PATH.name} keep_out_of_git=yes")
    print("HYPOTHESIS restart_behavior=untested_slice_C")
    print("SPIKE_COMPLETED")


if __name__ == "__main__":
    asyncio.run(main())
