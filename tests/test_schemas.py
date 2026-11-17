import pytest
from pydantic import ValidationError

from src.schemas import AuditPatch, Candidate, FlowInventory

CAND = dict(category="DATA_SOURCE", scope="FLOW", screens=["S1", "S2"], element_refs=["RC1"],
            question="Where does the saved address come from?", answer_hint="endpoint, method, field",
            owner="tech", priority="P1")


def test_candidate_valid_and_constraints():
    assert Candidate(**CAND).source_agents == []
    for bad in ({"screens": []}, {"element_refs": []}, {"question": "x" * 301}, {"category": "OTHER"},
                {"owner": "qa"}, {"priority": "P3"}):
        with pytest.raises(ValidationError):
            Candidate(**{**CAND, **bad})


def test_audit_patch_add_max_three():
    assert AuditPatch().verdict == "ok"
    with pytest.raises(ValidationError):
        AuditPatch(add=[CAND] * 4)


def test_flow_inventory_minimal_and_defaults():
    inv = FlowInventory.model_validate({
        "flow_name": "Shop", "flow_summary": "Checkout", "screens": [{
            "id": "S1", "file": "1.png", "name": "Cart", "purpose": "Review", "complexity": "low",
            "elements": [{"id": "S1.E1", "type": "button", "label": "Continue"}]}]})
    assert inv.recurring_components == [] and inv.screens[0].elements[0].states_seen == []
    # mutable defaults are not shared between instances
    inv.screens[0].elements[0].states_seen.append("default")
    assert FlowInventory(flow_name="a", flow_summary="b", screens=[]).transitions == []
