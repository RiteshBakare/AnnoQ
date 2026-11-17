"""Shared helpers for the M0 ADK spikes: run a workflow offline and a scripted stub LLM."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk import Runner
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types


async def _run(node: Any, text: str = "start") -> tuple[dict, list]:
    svc = InMemorySessionService()
    runner = Runner(node=node, app_name="spike", session_service=svc)
    session = await svc.create_session(app_name="spike", user_id="u")
    events = []
    async for ev in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(ev)
    final = await svc.get_session(app_name="spike", user_id="u", session_id=session.id)
    return dict(final.state), events


def run_workflow(node: Any, text: str = "start") -> tuple[dict, list]:
    """Run a node/workflow to completion; return (final session state, events)."""
    return asyncio.run(_run(node, text))


class StubLlm(BaseLlm):
    """Offline model: `responder(request) -> str` produces the reply text; raises propagate."""

    model: str = "stub"
    responder: Callable[[LlmRequest], str]
    requests: list = []
    delay: float = 0.0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.responder(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def request_text(req: LlmRequest) -> str:
    return "\n".join(p.text for c in req.contents for p in (c.parts or []) if p.text)
