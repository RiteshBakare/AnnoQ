"""T2.1/T2.2: every agent template renders under StrictUndefined; input templates match snapshots.

Update snapshots after an intentional prompt change:  UPDATE_SNAPSHOTS=1 pytest tests/test_templates.py
(then re-run the golden-set dry runs and compare question quality, not just that it parses).
"""

import os
from pathlib import Path
from typing import get_args

import pytest
from jinja2 import UndefinedError

from src.config import load_settings
from src.image_utils import load_flow
from src.nodes import analyst_input, auditor_input, curator_input, curator_targets, max_candidates, panel_input
from src.prompts import Prompts, render_agent
from src.schemas import ElementType, FlowInventory

SNAP = Path(__file__).parent / "snapshots"

INV = FlowInventory.model_validate({
    "flow_name": "Checkout", "flow_summary": "Cart to order.",
    "screens": [
        {"id": "S1", "file": "01_cart.png", "name": "Cart", "purpose": "Review items", "complexity": "medium",
         "elements": [{"id": "S1.E1", "type": "list", "label": "Cart items", "data_bearing": True,
                       "interactive": True, "needs_clarification": True, "notes": "3 rows"},
                      {"id": "S1.E2", "type": "card", "label": "Order summary", "data_bearing": True}]},
        {"id": "S2", "file": "02_pay.png", "name": "Payment", "purpose": "Pay", "complexity": "low",
         "elements": [{"id": "S2.E1", "type": "card", "label": "Order summary", "data_bearing": True,
                       "needs_clarification": True},
                      {"id": "S2.E2", "type": "button", "label": "Pay now", "interactive": True,
                       "needs_clarification": True}]}],
    "transitions": [{"from_screen": "S1", "to_screen": "S2", "trigger": "S1.E3"}],
    "recurring_components": [{"id": "RC1", "name": "Order summary", "appears_on": ["S1", "S2"],
                              "element_refs": ["S1.E2", "S2.E1"]}],
})
CANDS = [{"category": "DATA_SOURCE", "scope": "FLOW", "screens": ["S1", "S2"], "element_refs": ["RC1"],
          "question": "Which endpoint returns the order summary?", "answer_hint": "endpoint, method, fields",
          "owner": "tech", "priority": "P1", "source_agents": ["DS"]}]
SETTINGS = load_settings(env_file=None, environ={})


def check_snapshot(name: str, text: str) -> None:
    path = SNAP / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        SNAP.mkdir(exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    assert text + "\n" == path.read_text(encoding="utf-8"), f"snapshot {path.name} changed"


@pytest.mark.parametrize("name", ["specialist_base", "ds", "ia", "vl", "st", "br", "curator", "auditor"])
def test_agent_templates_render(name):
    text = render_agent(name)
    assert "Return ONLY valid JSON" in text and "{%" not in text and "{{" not in text


def test_specialists_extend_base_with_distinct_lenses():
    lenses = {n: render_agent(n) for n in ("ds", "ia", "vl", "st", "br")}
    for name, text in lenses.items():
        assert "ASK ONCE ACROSS SCREENS" in text and "NO API catalog" in text
        assert f"({name.upper()})" in text
    assert len(set(lenses.values())) == 5


def test_analyst_template_lists_element_types_and_is_strict():
    text = render_agent("analyst", element_types=list(get_args(ElementType)))
    assert "stepper, chart, other" in text
    with pytest.raises(UndefinedError):
        render_agent("analyst")


def test_analyst_input_snapshot(make_flow_dir):
    flow = load_flow(make_flow_dir(["01_cart.png", "02_place-order.png", "003.png"]))
    check_snapshot("analyst_input", analyst_input(flow, ticket_context="Summary: Checkout v2"))
    retry = analyst_input(flow, repair="screens: expected 3, got 2")
    assert retry.endswith("Your previous answer was invalid (screens: expected 3, got 2). "
                          "Fix it and return valid JSON only.")
    assert "Jira ticket context" not in retry


def test_panel_input_snapshot():
    text = panel_input(INV, ticket_context="")
    check_snapshot("panel_input", text)
    assert '"max_candidates": 6' in text and '- RC1 "Order summary" on S1, S2' in text
    assert '"data_bearing":false' not in text  # defaults stripped to save tokens
    single = FlowInventory.model_validate({**INV.model_dump(), "screens": INV.model_dump()["screens"][:1],
                                           "recurring_components": [], "transitions": []})
    assert 'Single-screen flow: use scope="SCREEN" only.' in panel_input(single)


def test_curator_input_snapshot():
    check_snapshot("curator_input", curator_input(INV, CANDS, SETTINGS))
    assert curator_targets(INV, SETTINGS) == {
        "per_screen": {"S1": "4-6", "S2": "2-3"}, "flow_level": "2-8", "total": "8-17",
        "hard_caps": {"per_screen": 8, "flow": 8, "total": 30}}


def test_auditor_input_snapshot():
    qs = [{**CANDS[0], "n": 1}]
    check_snapshot("auditor_input", auditor_input(qs, INV, uncovered=["S2.E2"]))


def test_max_candidates():
    assert [max_candidates(n) for n in (1, 2, 4, 5, 8)] == [4, 6, 10, 12, 12]


def test_input_templates_strict(tmp_path):
    p = Prompts()
    for name in ("analyst_input", "panel_input", "curator_input", "auditor_input"):
        with pytest.raises(UndefinedError):
            p.render_input(name)
