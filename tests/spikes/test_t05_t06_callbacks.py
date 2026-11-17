"""T0.5/T0.6 offline part — Agent nodes with model callbacks (stub LLMs, no network).

Verifies A5 (callbacks fire on Agent nodes), A6 (on_model_error_callback stub lets JoinNode complete),
image injection only for vision agents, and a lock-guarded budget that refuses calls past the cap under
parallel execution. Live Groq/Gemini checks: tests/spikes/live_models.py.
"""

import json
import threading

from google.adk import Agent, Event, Workflow
from google.adk.models.llm_response import LlmResponse
from google.adk.workflow import JoinNode, START
from google.genai import types

from ._harness import StubLlm, run_workflow

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c40000000049454e44ae426082"
)


class Budget:
    def __init__(self, max_calls: int):
        self.max, self.used, self.lock, self.log = max_calls, 0, threading.Lock(), []

    def consume(self) -> bool:
        with self.lock:
            if self.used >= self.max:
                return False
            self.used += 1
            return True


def stub_response(payload: dict) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]))


def make_callbacks(budget: Budget, image_parts: list | None):
    def before(callback_context, llm_request):
        if not budget.consume():
            return stub_response({"error": "budget_exhausted"})
        if image_parts:
            llm_request.contents.append(types.Content(role="user", parts=image_parts))
        return None

    def after(callback_context, llm_response):
        budget.log.append((callback_context.agent_name, llm_response.usage_metadata))
        return None

    def on_error(callback_context, llm_request, error):
        budget.log.append((callback_context.agent_name, f"error:{type(error).__name__}: {str(error)[:300]}"))
        return stub_response({"error": "model_error", "detail": type(error).__name__})

    return dict(before_model_callback=before, after_model_callback=after, on_model_error_callback=on_error)


def build(budget: Budget, failing: set[str]):
    image_parts = [types.Part.from_bytes(data=PNG, mime_type="image/png") for _ in range(4)]
    stubs: dict[str, StubLlm] = {}

    def agent(name, vision=False):
        def respond(req, name=name):
            if name in failing:
                raise RuntimeError("provider down")
            return json.dumps({"candidates": [], "agent": name})

        stubs[name] = StubLlm(responder=respond, requests=[], delay=0.1)
        return Agent(name=name, model=stubs[name], instruction=f"{name}",
                     **make_callbacks(budget, image_parts if vision else None))

    analyst = agent("screen_analyst", vision=True)
    specialists = [agent(n) for n in ("ds", "ia", "vl", "st", "br")]
    panel = Workflow(name="specialist_panel", edges=[(START, tuple(specialists), JoinNode(name="join_panel"))])

    def merge(node_input):
        return Event(output=None, state={"joined": node_input})

    wf = Workflow(name="cb_spike", edges=[(START, analyst, panel, merge)])
    return wf, stubs


def image_count(req) -> int:
    return sum(1 for c in req.contents for p in (c.parts or []) if p.inline_data)


def test_error_stub_lets_join_complete_and_images_only_on_vision():
    budget = Budget(9)
    wf, stubs = build(budget, failing={"vl", "br"})
    state, _ = run_workflow(wf)
    joined = state["joined"]
    print("join with 2 failed branches:", joined)
    assert set(joined) == {"ds", "ia", "vl", "st", "br"}  # A6
    assert json.loads(joined["vl"])["error"] == "model_error"
    assert image_count(stubs["screen_analyst"].requests[0]) == 4
    for n in ("ds", "ia", "vl", "st", "br"):
        assert all(image_count(r) == 0 for r in stubs[n].requests), n
    assert budget.used == 6


def test_budget_refuses_calls_past_cap_in_parallel():
    budget = Budget(3)  # analyst + 2 of the 5 parallel specialists
    wf, stubs = build(budget, failing=set())
    state, _ = run_workflow(wf)
    refused = [k for k, v in state["joined"].items() if json.loads(v).get("error") == "budget_exhausted"]
    print("refused:", refused)
    assert budget.used == 3 and len(refused) == 3
    assert sum(len(s.requests) for s in stubs.values()) == 3  # refused calls never reach the model
