"""T0.2 — graph basics: START -> fn -> fn, Event(output, state), Runner, final state. Verifies A1."""

from google.adk import Event, Workflow
from google.adk.workflow import START

from ._harness import run_workflow


def first(node_input):
    return Event(output={"msg": f"got:{node_input}"}, state={"seen": 1})


def second(node_input, seen: int):
    # `seen` is bound from session state; node_input is the previous node's output.
    return Event(output=None, state={"result": {"input": node_input, "seen": seen + 1}})


def test_linear_graph_and_state_binding():
    wf = Workflow(name="basics", edges=[(START, first, second)])
    state, _ = run_workflow(wf, "hello")
    assert state["seen"] == 1
    assert state["result"]["seen"] == 2
    # A8: the previous node's dict output arrives as a dict.
    assert isinstance(state["result"]["input"], dict)
    print("start input seen by first node:", state["result"]["input"])
