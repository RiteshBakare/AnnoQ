"""T3.5: post-processing (spec §11), pure functions, offline."""

import json

import pytest

from src.config import load_settings
from src.image_utils import Flow, ScreenImage
from src.postprocess import (
    InvalidOutput, FlowIndex, apply_caps, apply_patch, coverage, dedupe_across_screens, fallback_curate, fit_to_size,
    floor_warnings, merge_candidates, order_and_number, parse_curated, validate_candidate, validate_inventory)
from src.schemas import Candidate, FlowInventory, Question


def el(id, type="button", label="x", flagged=True):
    return {"id": id, "type": type, "label": label, "needs_clarification": flagged}


INV_DICT = {
    "flow_name": "Shopping", "flow_summary": "Cart to success.",
    "screens": [
        {"id": "S1", "file": "01_cart.png", "name": "Cart", "purpose": "p", "complexity": "medium",
         "elements": [el("S1.E1", "list", "Cart items"), el("S1.E2", "card", "Order summary"),
                      el("S1.E3", "button", "Proceed to checkout")]},
        {"id": "S2", "file": "02_ship.png", "name": "Shipping", "purpose": "p", "complexity": "high",
         "elements": [el("S2.E1", "card", "Deliver to"), el("S2.E2", "input", "Full name"),
                      el("S2.E3", "input", "Pincode"), el("S2.E4", "button", "Continue")]},
        {"id": "S3", "file": "03_place.png", "name": "Place order", "purpose": "p", "complexity": "high",
         "elements": [el("S3.E1", "card", "Deliver to", False), el("S3.E2", "card", "Order summary", False),
                      el("S3.E3", "button", "Place order")]},
        {"id": "S4", "file": "04_done.png", "name": "Success", "purpose": "p", "complexity": "low",
         "elements": [el("S4.E1", "card", "Delivering to", False), el("S4.E2", "text", "Order #", True)]},
    ],
    "transitions": [{"from_screen": "S1", "to_screen": "S2", "trigger": "S1.E3"}],
    "recurring_components": [
        {"id": "RC1", "name": "Address block", "appears_on": ["S2", "S3", "S4"],
         "element_refs": ["S2.E1", "S3.E1", "S4.E1"]},
        {"id": "RC2", "name": "Order summary", "appears_on": ["S1", "S3"], "element_refs": ["S1.E2", "S3.E2"]}],
}
INV = FlowInventory.model_validate(INV_DICT)
SETTINGS = load_settings(env_file=None, environ={})


def flow(n=4):
    files = ["01_cart.png", "02_ship.png", "03_place.png", "04_done.png"][:n]
    return Flow(hint="shopping", sha="0" * 64, screens=[
        ScreenImage(id=f"S{k}", file=f, hint=f"Hint {k}", mime_type="image/png", data=b"", sha256="", width=1,
                    height=1, resized=False) for k, f in enumerate(files, 1)])


def cand(question="Where does the data come from?", category="DATA_SOURCE", screens=("S1",), refs=("S1.E1",),
         priority="P1", owner="tech", sources=("DS",), hint="endpoint, method, field") -> Candidate:
    return Candidate(category=category, scope="FLOW" if len(screens) > 1 else "SCREEN", screens=list(screens),
                     element_refs=list(refs), question=question, answer_hint=hint, owner=owner, priority=priority,
                     source_agents=list(sources))


# ------------------------------------------------------------------ T3.1 inventory

def test_validate_inventory_ok_and_soft_repairs():
    data = json.loads(json.dumps(INV_DICT))
    data["screens"][0]["name"] = ""
    data["screens"][1]["file"] = "wrong.png"
    data["screens"][2]["elements"][0]["type"] = "dropdown"
    data["screens"][3]["complexity"] = "trivial"
    data["recurring_components"].append({"id": "RC3", "name": "Ghost", "appears_on": ["S2", "S9"],
                                         "element_refs": ["S2.E1", "S9.E1"]})
    data["transitions"].append({"from_screen": "S4", "to_screen": "S7", "trigger": "x"})
    inv, warnings = validate_inventory("```json\n" + json.dumps(data) + "\n```", flow())
    assert inv.screens[0].name == "Hint 1" and inv.screens[1].file == "02_ship.png"
    assert inv.screens[2].elements[0].type == "other" and inv.screens[3].complexity == "medium"
    assert [rc.id for rc in inv.recurring_components] == ["RC1", "RC2"]
    assert len(inv.transitions) == 1 and len(warnings) == 3


@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d["screens"].pop(), "screens must be exactly ['S1', 'S2', 'S3', 'S4']"),
    (lambda d: d["screens"][1].update(id="S5"), "screens must be exactly"),
    (lambda d: d["screens"][0]["elements"][0].update(id="S2.E9"), "must look like S1.E<number>"),
    (lambda d: d["screens"][0]["elements"][1].update(id="S1.E1"), "duplicate element id S1.E1"),
    (lambda d: d.pop("screens"), "schema"),
])
def test_validate_inventory_fatal(mutate, msg):
    data = json.loads(json.dumps(INV_DICT))
    mutate(data)
    with pytest.raises(InvalidOutput, match=msg.replace("[", r"\[").replace("]", r"\]")):
        validate_inventory(data, flow())


@pytest.mark.parametrize("raw", ["not json at all", '{"error": "budget_exhausted"}', "[1, 2]"])
def test_validate_inventory_unusable(raw):
    with pytest.raises(InvalidOutput):
        validate_inventory(raw, flow())


# ------------------------------------------------------------------ T3.1 candidates

def raw(**kw):
    base = {"category": "DATA_SOURCE", "scope": "SCREEN", "screens": ["S2"], "element_refs": ["S2.E2"],
            "question": "Which service validates the full name", "answer_hint": "service", "owner": "tech",
            "priority": "P1"}
    return {**base, **kw}


def test_candidate_coercions():
    c = validate_candidate(raw(scope="SCREEN", screens=["S2", "S3"]), INV, "VL")
    assert (c.scope, c.screens, c.question[-1], c.source_agents) == ("SCREEN", ["S2"], "?", ["VL"])
    rc = validate_candidate(raw(scope="FLOW", screens=["S1", "S2", "S3", "S4"], element_refs=["RC1"]), INV)
    assert (rc.scope, rc.screens) == ("FLOW", ["S2", "S3", "S4"])  # clamped to RC1.appears_on
    mixed = validate_candidate(raw(element_refs="S1.E1, S9.E9, S3.E3"), INV)  # string refs, unknown removed
    assert mixed.element_refs == ["S1.E1", "S3.E3"] and mixed.screens == ["S1", "S3"] and mixed.scope == "FLOW"
    flow_only = validate_candidate(raw(element_refs=["FLOW"], screens=[]), INV)
    assert flow_only.screens == ["S1", "S2", "S3", "S4"]
    d = validate_candidate(raw(owner=None, priority="high", category="validation", answer_hint="w " * 150), INV)
    assert (d.owner, d.priority, d.category) == ("product", "P2", "VALIDATION")
    assert len(d.answer_hint) <= 200 and d.answer_hint.endswith("...")


@pytest.mark.parametrize("bad, msg", [
    (raw(category="OTHER"), "unknown category"),
    (raw(question="Why?"), "question length"),
    (raw(question="x" * 320), "question length"),
    (raw(element_refs=["S9.E1"]), "no known element_refs"),
    (raw(owner="qa"), "owner"),
    ("not a dict", "not a JSON object"),
])
def test_candidate_rejected(bad, msg):
    with pytest.raises(InvalidOutput, match=msg):
        validate_candidate(bad, INV)


def test_single_screen_flow_forces_screen_scope():
    one = FlowInventory.model_validate({**INV_DICT, "screens": INV_DICT["screens"][:1], "recurring_components": [],
                                        "transitions": []})
    c = validate_candidate(raw(scope="FLOW", screens=["S1", "S2"], element_refs=["S1.E1", "FLOW"]), one)
    assert (c.scope, c.screens) == ("SCREEN", ["S1"])
    qs, merged = dedupe_across_screens([c, c.model_copy()], one)
    assert merged == 0 and all(q.scope == "SCREEN" for q in qs)


def test_merge_candidates_tolerates_failures():
    good = json.dumps({"candidates": [raw(), raw(category="BOGUS")]})
    joined = {"data_source_agent": good, "interaction_agent": {"candidates": [raw(category="ACTION")]},
              "validation_agent": '```json\n{"candidates": []}\n```',
              "state_condition_agent": '{"error": "model_error", "detail": "RateLimitError"}',
              "business_rule_agent": "Sorry, I cannot help."}
    tags = {"data_source_agent": "DS", "interaction_agent": "IA", "validation_agent": "VL",
            "state_condition_agent": "ST", "business_rule_agent": "BR"}
    cands, usable, warnings = merge_candidates(joined, INV, tags)
    assert usable == 3 and [c.source_agents for c in cands] == [["DS"], ["IA"]]
    assert any(w.startswith("DS: dropped 1") for w in warnings)
    assert any(w.startswith("ST: unusable") for w in warnings) and any(w.startswith("BR:") for w in warnings)


def test_parse_curated():
    qs, _ = parse_curated({"questions": [raw(source_agents=["DS", "VL"]), raw(question="Who owns pincode rules?",
                                                                              source_agents=[])]}, INV, 5)
    assert [q.source_agents for q in qs] == [["DS", "VL"], ["CUR"]]
    with pytest.raises(InvalidOutput, match="never return"):
        parse_curated('{"questions": [], "dropped": [{"reason": "caps"}]}', INV, 38)
    assert parse_curated('{"questions": []}', INV, 0)[0] == []
    for bad in ('{"error": "budget_exhausted"}', '{"items": []}', "nope"):
        with pytest.raises(InvalidOutput):
            parse_curated(bad, INV, 3)


# ------------------------------------------------------------------ T3.2 dedupe

def test_same_address_question_on_three_screens_becomes_one_flow_question():
    qs = [cand("Where does the saved address come from?", screens=[s], refs=[f"{s}.E1"], priority=p, sources=[src],
               hint=h)
          for s, p, src, h in [("S2", "P2", "DS", "endpoint"), ("S3", "P1", "IA", "endpoint, method, fields"),
                               ("S4", "P2", "DS", "api")]]
    out, merged = dedupe_across_screens(qs, INV)
    assert merged == 2 and len(out) == 1
    q = out[0]
    assert (q.scope, q.screens, q.element_refs, q.priority) == ("FLOW", ["S2", "S3", "S4"], ["RC1"], "P1")
    assert q.answer_hint == "endpoint, method, fields" and q.source_agents == ["DS", "IA"]


def test_jaccard_merge_rules():
    a = cand("When the user taps Continue what API call is made and what happens next?", "ACTION", ["S2"], ["S2.E4"])
    b = cand("When the user taps Place order what API call is made and what happens next?", "ACTION", ["S3"],
             ["S3.E3"], priority="P2", sources=["IA"])
    other_cat = cand(b.question, "STATE", ["S3"], ["S3.E3"])
    same_screen = cand(a.question.replace("Continue", "the button"), "ACTION", ["S2"], ["S2.E4"])
    unrelated = cand("Which items are shown in the cart list and in what order?", "ACTION", ["S1"], ["S1.E1"])
    out, merged = dedupe_across_screens([a, b, other_cat, unrelated], INV)
    assert merged == 1 and len(out) == 3
    assert out[0].scope == "FLOW" and out[0].screens == ["S2", "S3"] and out[0].element_refs == ["S2.E4", "S3.E3"]
    assert out[0].priority == "P1" and out[0].source_agents == ["DS", "IA"]
    assert dedupe_across_screens([a, same_screen], INV)[1] == 0  # Jaccard merge is cross-screen only
    assert dedupe_across_screens([a, b], INV, threshold=0.95)[1] == 0


# ------------------------------------------------------------------ T3.3 coverage, fallback, patch

def test_coverage():
    assert coverage([], INV) == ["S1.E1", "S1.E2", "S1.E3", "S2.E1", "S2.E2", "S2.E3", "S2.E4", "S3.E3", "S4.E2",
                                 "RC1", "RC2"]
    qs = [cand(refs=["RC1"], screens=["S2", "S3", "S4"]),          # covers S2.E1 via RC1
          cand(refs=["S3.E2"], screens=["S3"]),                       # member of RC2 -> RC2 + S1.E2 covered
          cand(category="ACTION", refs=["S2"], screens=["S2"]),       # whole-screen ref covers S2.*
          cand(category="CONTENT", refs=["S1"], screens=["S1"])]      # CONTENT screen ref does not cover
    assert coverage(qs, INV) == ["S1.E1", "S1.E3", "S3.E3", "S4.E2"]


def test_fallback_curate():
    dup1 = cand("Which endpoint returns cart items?", refs=["S1.E1"], sources=["DS"], priority="P2")
    dup2 = cand("Which endpoint returns the cart items?", refs=["S1.E1"], sources=["IA"], priority="P1")
    addr = [cand("Where does the saved address come from?", screens=[s], refs=[f"{s}.E1"]) for s in ("S2", "S3")]
    p2 = cand("Is the name field trimmed?", "VALIDATION", ["S2"], ["S2.E2"], priority="P2", sources=["VL"])
    out, _ = fallback_curate([p2, dup1, dup2, *addr], INV, SETTINGS)
    assert len(out) == 3 and out[-1] is not None and out[-1].priority == "P2"
    cart = next(q for q in out if q.element_refs == ["S1.E1"])
    assert cart.source_agents == ["DS", "IA"] and cart.priority == "P1"
    assert any(q.scope == "FLOW" and q.element_refs == ["RC1"] for q in out)
    assert fallback_curate([], INV, SETTINGS) == ([], [])


def numbered(qs):
    return [Question(**q.model_dump(), n=k) for k, q in enumerate(qs, 1)]


def test_apply_patch_all_ops():
    qs = numbered([cand("Q one about the cart list?"), cand("Q two about the cart list?"),
                   cand("Q three about the address?", screens=["S2"], refs=["S2.E1"]),
                   cand("Q four about the pincode?", "VALIDATION", ["S2"], ["S2.E3"])])
    patch = {"remove": [{"n": 1, "reason": "duplicate"}, {"n": 2, "reason": "i dislike it"}, {"n": 99,
                                                                                          "reason": "duplicate"}],
             "reword": [{"n": 2, "question": "Which API returns the cart list items"}, {"n": 1, "question": "x?"}],
             "rescope": [{"n": 3, "screens": ["S2", "S3", "S9"]}, {"n": 4, "screens": ["S2"]}],
             "add": [{"category": "STATE", "screens": ["S4"], "element_refs": ["S4.E2"],
                      "question": "When is the order number shown?", "answer_hint": "rule", "owner": "product"},
                     {"category": "NOPE", "element_refs": ["S1.E1"], "question": "bad add?"},
                     {"category": "STATE", "element_refs": ["S1.E3"], "question": "Add three is valid?"},
                     {"category": "STATE", "element_refs": ["S1.E3"], "question": "Add four is ignored?"}],
             "verdict": "patched"}
    out, warnings = apply_patch(qs, json.dumps(patch), INV)
    assert [q.question for q in out] == ["Which API returns the cart list items?", "Q three about the address?",
                                         "Q four about the pincode?", "When is the order number shown?",
                                         "Add three is valid?"]
    assert (out[1].scope, out[1].screens) == ("FLOW", ["S2", "S3"]) and out[2].scope == "SCREEN"
    assert out[3].source_agents == ["AUD"] and out[3].priority == "P2"
    assert len(warnings) == 6  # bad reason, unknown n=99, reword on removed, rescope<2, add>3, invalid add


def test_apply_patch_invalid_is_skipped():
    qs = numbered([cand()])
    for bad in ('{"error": "model_error"}', "garbage", "[]"):
        out, warnings = apply_patch(qs, bad, INV)
        assert [q.question for q in out] == [qs[0].question] and warnings[0].startswith("auditor patch skipped")


# ------------------------------------------------------------------ T3.4 caps, ordering, size

def test_caps_drop_p2_then_auditor_then_single_source():
    s = load_settings(env_file=None, environ={"MAX_Q_PER_SCREEN": "3", "MAX_FLOW_Q": "1", "MAX_TOTAL_Q": "4"})
    screen = [cand(f"P1 question {i}?", priority="P1") for i in range(2)] + [
        cand("P2 multi-source?", priority="P2", sources=["DS", "IA"]),
        cand("P2 single-source?", priority="P2", sources=["DS"]),
        cand("P2 auditor added?", priority="P2", sources=["AUD"])]
    flows = [cand("Flow P2?", screens=["S2", "S3"], refs=["RC1"], priority="P2"),
             cand("Flow P1?", screens=["S2", "S3"], refs=["RC1"], priority="P1")]
    other = [cand("Shipping P2?", screens=["S2"], refs=["S2.E2"], priority="P2")]
    kept, warnings = apply_caps(screen + flows + other, INV, s)
    assert [q.question for q in kept] == ["P1 question 0?", "P1 question 1?", "P2 multi-source?", "Flow P1?"]
    assert len(warnings) == 3
    assert apply_caps([], INV, SETTINGS) == ([], [])


def test_order_and_number():
    qs = [cand("S3 product P2?", screens=["S3"], refs=["S3.E3"], priority="P2", owner="product"),
          cand("S1 design P1?", owner="design"), cand("S1 tech P1?"), cand("S1 tech P2?", priority="P2"),
          cand("Flow P2?", screens=["S2", "S3"], refs=["RC1"], priority="P2"),
          cand("Flow P1 product?", screens=["S1", "S3"], refs=["RC2"], owner="product")]
    out = order_and_number(qs, INV)
    assert [(q.n, q.question) for q in out] == [
        (1, "Flow P1 product?"), (2, "Flow P2?"), (3, "S1 tech P1?"), (4, "S1 design P1?"), (5, "S1 tech P2?"),
        (6, "S3 product P2?")]


def test_floor_warnings():
    qs = [cand(screens=["S1"]), cand(screens=["S2", "S3"], refs=["RC1"])]
    assert floor_warnings(qs, INV) == []
    assert floor_warnings(qs[:1], INV) == ["S2 (Shipping) has no questions", "S3 (Place order) has no questions"]


def test_fit_to_size_drops_and_renumbers():
    qs = order_and_number([cand("Keep me P1 please?"), cand("Drop me first P2?", priority="P2"),
                           cand("Second P1 kept?")], INV)

    def render(items):
        return "\n".join(f"Q{q.n}. {q.question}" for q in items)

    out, text, warnings = fit_to_size(qs, render, max_chars=len(render(qs)) - 5)
    assert [(q.n, q.question) for q in out] == [(1, "Keep me P1 please?"), (2, "Second P1 kept?")]
    assert text == render(out) and warnings == [f"comment over {len(render(qs)) - 5} chars: dropped 1 question(s)"]
    same, _, none = fit_to_size(qs, render, 10_000)
    assert same == qs and none == []


def test_flow_index():
    idx = FlowIndex(INV)
    assert idx.screens_for(["RC1", "S1.E1", "FLOW"]) == ["S1", "S2", "S3", "S4"]
    assert idx.single_rc(["S2.E1", "RC1"]) == "RC1" and idx.single_rc(["S2.E1", "S2.E2"]) is None


@pytest.mark.parametrize("raw, clean", [
    ("What API feeds the Stepper component (RC1) on Shipping?", "What API feeds the Stepper component on Shipping?"),
    ("Which rules apply to Full name (S2.E2, S2.E3) and Pincode", "Which rules apply to Full name and Pincode?"),
    ("Is the  cart (S1) shown.", "Is the cart shown?"),
    ("Keep (optional) words like RC car?", "Keep (optional) words like RC car?"),
])
def test_clean_question_drops_internal_ids(raw, clean):
    from src.postprocess import clean_question
    assert clean_question(raw) == clean
