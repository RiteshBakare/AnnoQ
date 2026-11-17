"""T0.3 — nested `specialist_panel` Workflow (3 stub agents + JoinNode) used as a node. Verifies A2, A3, A7, A8."""

import asyncio
import json

from google.adk import Agent, Event, Workflow
from google.adk.workflow import JoinNode, START

from ._harness import StubLlm, request_text, run_workflow


class Tracker:
    def __init__(self):
        self.live = 0
        self.peak = 0


def make_agent(name: str, tracker: Tracker, seen: dict) -> Agent:
    def respond(req):
        seen[name] = request_text(req)
        return json.dumps({"candidates": [{"source": name}]})

    class TrackedLlm(StubLlm):
        async def generate_content_async(self, llm_request, stream=False):
            tracker.live += 1
            tracker.peak = max(tracker.peak, tracker.live)
            await asyncio.sleep(0.2)
            tracker.live -= 1
            async for r in super().generate_content_async(llm_request, stream):
                yield r

    return Agent(name=name, model=TrackedLlm(responder=respond, requests=[]), instruction=f"You are {name}.")


def build_input(node_input):
    return Event(output="PANEL INPUT: inventory JSON here")


def merge(node_input):
    return Event(output=None, state={"joined": {k: [type(v).__name__, v] for k, v in node_input.items()}})


def run_panel(max_concurrency):
    tracker, seen = Tracker(), {}
    agents = [make_agent(n, tracker, seen) for n in ("ds", "ia", "vl")]
    panel = Workflow(
        name="specialist_panel",
        edges=[(START, tuple(agents), JoinNode(name="join_panel"))],
        max_concurrency=max_concurrency,
    )
    wf = Workflow(name="parent", edges=[(START, build_input, panel, merge)])
    state, _ = run_workflow(wf)
    return state, tracker, seen


def test_nested_panel_join_shape_and_input():
    state, tracker, seen = run_panel(max_concurrency=None)
    joined = state["joined"]
    print("join output:", joined)
    # A3: dict keyed by agent name.
    assert set(joined) == {"ds", "ia", "vl"}
    # A2: every agent received the parent node's output as its user message.
    for name, text in seen.items():
        assert "PANEL INPUT" in text, (name, text)
    assert tracker.peak == 3  # unlimited -> all parallel


def test_panel_max_concurrency_throttles():
    _, tracker, _ = run_panel(max_concurrency=2)
    print("peak concurrency with max_concurrency=2:", tracker.peak)
    assert tracker.peak <= 2  # A7
