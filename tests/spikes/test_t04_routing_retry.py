"""T0.4 — router with Event(route=...), dict edges, loop edge back to an earlier node (2 inbound edges),
retry counter in state. Verifies A4."""

import json

from google.adk import Agent, Event, Workflow
from google.adk.workflow import START

from ._harness import StubLlm, request_text, run_workflow


def build_graph(replies: list[str], max_retries: int = 1):
    calls: list[str] = []

    def respond(req):
        calls.append(request_text(req))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    analyst = Agent(name="screen_analyst", model=StubLlm(responder=respond, requests=[]), instruction="Analyse.")

    def build_analyst_input(node_input, retries_used: int = 0, repair: str = ""):
        msg = "ANALYST INPUT"
        if repair:
            msg += f"\nYour previous answer was invalid ({repair})."
        return Event(output=msg)

    def validate_inventory(node_input, retries_used: int = 0):
        try:
            data = json.loads(node_input)
            assert data.get("ok")
            return Event(output=data, state={"inventory": data}, route="OK")
        except Exception as e:  # noqa: BLE001
            if retries_used < max_retries:
                return Event(output=None, state={"retries_used": retries_used + 1, "repair": type(e).__name__},
                             route="RETRY")
            return Event(output=None, state={"fail_reason": "analyst invalid"}, route="ABORT")

    def next_step(node_input):
        return Event(output=None, state={"result": {"ok": True}})

    def fail(node_input, fail_reason: str = ""):
        return Event(output=None, state={"result": {"ok": False, "reason": fail_reason}})

    wf = Workflow(
        name="retry_spike",
        edges=[
            (START, build_analyst_input, analyst, validate_inventory),
            (validate_inventory, {"OK": next_step, "RETRY": build_analyst_input, "ABORT": fail}),
        ],
    )
    return wf, calls


def test_retry_then_ok():
    wf, calls = build_graph(["not json", '{"ok": true}'])
    state, _ = run_workflow(wf)
    assert state["result"] == {"ok": True}
    assert state["retries_used"] == 1
    assert len(calls) == 2
    assert "previous answer was invalid" in calls[1]
    # Does the retry request also contain the first (failed) attempt? Record it for the spec.
    print("retry request contains first reply:", "not json" in calls[1])
    print("retry request text:\n", calls[1])


def test_invalid_twice_aborts():
    wf, calls = build_graph(["bad", "still bad"])
    state, _ = run_workflow(wf)
    assert state["result"] == {"ok": False, "reason": "analyst invalid"}
    assert len(calls) == 2
