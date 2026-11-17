"""Golden reference set (tests/golden/references.json) is well-formed and usable by tests/live/evaluate.py."""

import json
from pathlib import Path

import pytest

from tests.live.evaluate import matches

REFS = json.loads(Path("tests/golden/references.json").read_text(encoding="utf-8"))
FLOWS = [k for k in REFS if not k.startswith("_")]


@pytest.mark.parametrize("flow", FLOWS)
def test_topics_well_formed(flow):
    topics = REFS[flow]
    assert topics, flow
    names = [t["topic"] for t in topics]
    assert len(names) == len(set(names)), "duplicate topic names"
    for t in topics:
        assert set(t) <= {"topic", "question", "match", "ask_once"}, t["topic"]
        assert t["match"] and all(isinstance(g, list) and g and all(isinstance(w, str) and w for w in g)
                                  for g in t["match"]), t["topic"]
        assert all(w == w.lower() for g in t["match"] for w in g), "match words must be lower-case"


@pytest.mark.parametrize("flow", FLOWS)
def test_flow_folder_exists_when_present(flow):
    folder = Path("flows") / flow
    if not folder.exists():
        pytest.skip(f"{folder} not on disk")
    assert any(p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} for p in folder.iterdir())


def test_purchase_reference_questions_match_their_own_topic():
    """Each real-design topic carries the annotator's reference question; it must satisfy its own matcher."""
    for t in REFS["purchase"]:
        assert t.get("question"), t["topic"]
        assert matches(t["question"], t["match"]), t["topic"]


def test_purchase_ask_once_topics():
    once = {t["topic"] for t in REFS["purchase"] if t.get("ask_once")}
    assert once == {"Order totals calculation (asked once)", "Bottom tab bar (asked once)"}
