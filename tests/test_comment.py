"""T5.1: jira_comment.j2 + postprocess.comment_context. Snapshots for a 4-screen and a 1-screen flow.

Update after an intentional template change:  UPDATE_SNAPSHOTS=1 pytest tests/test_comment.py
"""

from src.config import parse_owner_labels
from src.postprocess import comment_context, order_and_number
from src.prompts import render_template
from src.schemas import Candidate, FlowInventory
from tests.test_postprocess import INV, INV_DICT, cand
from tests.test_templates import check_snapshot

LABELS = parse_owner_labels(None)
MARKER = "ai-screen-questions:v2:abc123def456"


def render(questions, inv=INV):
    return render_template("jira_comment", **comment_context(questions, inv, LABELS, "run42", MARKER))


def four_screen_questions():
    return order_and_number([
        cand("Which API provides the saved address and is it reused on all three screens?", screens=["S2", "S3", "S4"],
             refs=["RC1"], hint="endpoint path, method, response fields"),
        cand("How are taxes, shipping fee and promo discounts calculated, and in what order?", "BUSINESS_RULE",
             screens=["S1", "S3"], refs=["RC2"], owner="product", priority="P2", hint="formula"),
        cand("What happens on tap of *Proceed to checkout* {when} the cart has [0] items?", "ACTION",
             refs=["S1.E3"], hint="navigation target, API call"),
        cand("Which rules apply to Full name and Pincode?", "VALIDATION", ["S2"], ["S2.E2", "S2.E3"],
             owner="product", hint=""),
        cand("What does Place order call, and what happens on payment failure?", "ACTION", ["S3"], ["S3.E3"],
             owner="tech"),
        cand("Who designs the empty-state illustration?", "CONTENT", ["S3"], ["S3"], owner="design", priority="P2"),
    ], INV)


def test_four_screen_snapshot():
    text = render(four_screen_questions())
    check_snapshot("jira_comment_4_screens", text)
    assert text.startswith("h2. Clarification questions — Shopping (4 screens)")
    assert "Hi @tech team, @product team, @design team" in text
    assert "*Who answers what:* @tech team → Q1, Q3, Q5 · @product team → Q2, Q4 · @design team → Q6" in text
    assert "h3. Common to multiple screens — answer once" in text
    assert "*Q1. \\[DATA_SOURCE\\] Address block* — applies to: Shipping, Place order, Success" in text
    assert "h3. S1 — Cart" in text and "h3. S4" not in text  # screens without questions are omitted
    assert "\\*Proceed to checkout\\* \\{when\\} the cart has \\[0\\] items?" in text  # LLM text escaped
    assert "Q4." in text and "_Please include: _" not in text  # empty hint line dropped
    assert text.rstrip().endswith(f"run run42 · {MARKER}{{color}}")
    assert "@" not in text.replace("@tech team", "").replace("@product team", "").replace("@design team", "")


def test_one_screen_snapshot():
    one = FlowInventory.model_validate({**INV_DICT, "screens": INV_DICT["screens"][:1], "recurring_components": [],
                                        "transitions": []})
    qs = order_and_number([cand("Which endpoint returns the cart items?"),
                           cand("What does Proceed to checkout do?", "ACTION", refs=["S1.E3"], priority="P2")], one)
    text = render(qs, one)
    check_snapshot("jira_comment_1_screen", text)
    assert "(1 screen)" in text and "Common to multiple screens" not in text and "h3. S1 — Cart" in text


def test_no_questions():
    text = render([])
    assert "_No open questions were found for this flow._" in text and "Who answers what" not in text
    assert "Hi @product team, @tech team, @design team" in text  # all owners when nobody is assigned
    assert MARKER in text


def test_numbering_contiguous_and_matches_owner_index():
    qs = four_screen_questions()
    assert [q.n for q in qs] == list(range(1, 7))
    text = render(qs)
    for q in qs:
        assert f"*Q{q.n}. " in text


def test_element_label_truncates_long_ref_lists():
    q = order_and_number([Candidate(**{**cand().model_dump(), "element_refs": ["S1.E1", "S1.E2", "S1.E3", "S1"]})],
                         INV)
    view = comment_context(q, INV, LABELS, "r", MARKER)["screen_sections"][0]["questions"][0]
    assert view["element_label"] == "Cart items, Order summary, Proceed to checkout +1 more"
