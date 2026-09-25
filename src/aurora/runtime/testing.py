"""Modelo falso somente para teste.

Não chama provedor externo, não lê chave, não lê .env e não deve ser
usado em runtime de produção. Serve ao smoke E13 (R-A/R-B/R-C).
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

APPROVAL_NAME = "adk_request_confirmation"


class StubLlm(BaseLlm):
    """LLM determinístico de teste; nunca acessa rede nem credencial."""

    _reply: str = PrivateAttr()
    _tool_name: str | None = PrivateAttr(default=None)
    _tool_args: dict[str, str] = PrivateAttr(default_factory=dict)
    _transfer_to: str | None = PrivateAttr(default=None)

    def __init__(
        self,
        reply: str = "ok",
        tool_name: str | None = None,
        tool_args: dict[str, str] | None = None,
        transfer_to: str | None = None,
    ) -> None:
        super().__init__(model="stub-local-test")
        self._reply = reply
        self._tool_name = tool_name
        self._tool_args = tool_args or {}
        self._transfer_to = transfer_to

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
        if APPROVAL_NAME in responses or (
            self._tool_name and self._tool_name in responses
        ):
            yield self.text("concluído")
            return
        if self._transfer_to:
            yield self.call(
                "transfer_to_agent",
                "fc-transfer",
                {"agent_name": self._transfer_to},
            )
            return
        if self._tool_name:
            yield self.call(self._tool_name, "fc-stub", self._tool_args)
            return
        yield self.text(self._reply)

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
