"""T4.3 graph tests: the real workflow, real nodes and real budget callbacks; only the LLMs are stubs (offline).

Scenarios (spec §14): normal run = 8 calls; analyst RETRY -> OK; analyst invalid twice -> fail; 2 specialists fail
-> proceed; 3 fail -> fail; curator RETRY -> OK; curator invalid twice -> FALLBACK; retry pool shared; auditor invalid
-> skipped; MAX_LLM_CALLS=8 -> no retry; publish paths (dry-run, marker, --force, Jira error). Never more than 9.
"""

import asyncio
import json

import pytest
from google.adk import Agent

from src.agents import SPECIALISTS, Agents
from src.budget import make_callbacks
from src.config import load_settings
from src.image_utils import load_flow
from src.jira_client import JiraError
from src.run_context import RunContext
from src.workflow import build_workflow, edges, run_workflow
from tests.spikes._harness import StubLlm, request_text
from tests.test_postprocess import INV_DICT

FILES = ["01_cart.png", "02_ship.png", "03_place.png", "04_done.png"]


def cand(category, refs, question, owner="tech", priority="P1"):
    return {"category": category, "scope": "SCREEN", "screens": [], "element_refs": refs, "question": question,
            "answer_hint": "the rule", "owner": owner, "priority": priority}


PANEL = {
    "data_source_agent": {"candidates": [cand("DATA_SOURCE", ["RC1"], "Where does the saved address come from?"),
                                         cand("DATA_SOURCE", ["S1.E1"], "Which endpoint returns the cart items?")]},
    "interaction_agent": {"candidates": [cand("ACTION", ["S3.E3"], "What happens when Place order is tapped?")]},
    "validation_agent": {"candidates": [cand("VALIDATION", ["S2.E2", "S2.E3"], "Which rules apply to Full name "
                                                                                 "and Pincode?", "product")]},
    "state_condition_agent": {"candidates": [cand("STATE", ["S2.E4"], "When is Continue enabled?", "product")]},
    "business_rule_agent": {"candidates": [cand("BUSINESS_RULE", ["RC2"], "How is the order total calculated?",
                                                "product")]},
}
CURATED = {"questions": [c for v in PANEL.values() for c in v["candidates"]], "dropped": []}
PATCH_OK = {"add": [], "remove": [], "reword": [], "rescope": [], "verdict": "ok"}


class Script:
    """Replies for one stub agent, in order (the last one repeats). Exceptions are raised (-> on_model_error)."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, req):
        self.requests.append(req)
        r = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def stub_agents(rc, overrides=None):
    scripts = {"screen_analyst": Script(INV_DICT), "flow_curator": Script(CURATED), "auditor": Script(PATCH_OK),
               **{n: Script(PANEL[n]) for n, _, _ in SPECIALISTS}, **(overrides or {})}

    def agent(name, vision):
        rc.agent_models[name] = "gemini-stub" if vision else "groq/stub"
        return Agent(name=name, model=StubLlm(responder=scripts[name], requests=[]), mode="single_turn",
                     instruction=name, **make_callbacks(rc, attach_images=vision))

    agents = Agents(agent("screen_analyst", True), tuple(agent(n, False) for n, _, _ in SPECIALISTS),
                    agent("flow_curator", False), agent("auditor", True))
    return agents, scripts


@pytest.fixture
def make_rc(make_flow_dir, tmp_path):
    def _make(jira=None, **env):
        settings = load_settings({"dry_run": jira is None}, env_file=None, environ={k.upper(): v for k, v in env.items()})
        rc = RunContext(settings=settings, flow=load_flow(make_flow_dir(FILES)), jira_key="SCRUM-1",
                        out_dir=tmp_path / "out", jira=jira)
        return rc
    return _make


def run(rc, overrides=None):
    agents, scripts = stub_agents(rc, overrides)
    state = asyncio.run(run_workflow(rc, agents))
    assert rc.budget.used <= 9
    return state, scripts


def images(req):
    return sum(1 for c in req.contents for p in (c.parts or []) if p.inline_data)


def test_normal_run_is_8_calls_2_gemini_6_groq(make_rc):
    rc = make_rc()
    state, scripts = run(rc)
    assert state["result"]["ok"] and state["result"]["skipped_reason"] == "dry-run"
    assert rc.budget.used == 8 and len(rc.calls) == 8 and all(c.ok for c in rc.calls)
    assert sorted(c.model for c in rc.calls) == ["gemini-stub"] * 2 + ["groq/stub"] * 6
    assert images(scripts["screen_analyst"].requests[0]) == 4 and images(scripts["auditor"].requests[0]) == 4
    assert all(images(r) == 0 for n, sc in scripts.items() if n not in ("screen_analyst", "auditor")
               for r in sc.requests)
    out = rc.out_dir
    for f in ("comment.txt", "flow_inventory.json", "candidates.json", "curated.json", "audit.json",
              "run_report.json"):
        assert (out / f).exists(), f
    report = json.loads((out / "run_report.json").read_text())
    assert report["llm_calls"] == 8 and report["question_count"] == 6 and report["common_count"] == 2
    comment = (out / "comment.txt").read_text(encoding="utf-8")
    assert "h3. Common to multiple screens" in comment and rc.marker in comment and "*Q6." in comment and "*Q7." not in comment
    assert state["curated_by"] == "curator"


def test_analyst_retry_then_ok(make_rc):
    rc = make_rc()
    state, scripts = run(rc, {"screen_analyst": Script("not json", INV_DICT)})
    assert state["result"]["ok"] and rc.budget.used == 9 and state["retries_used"] == 1
    assert "Your previous answer was invalid (not valid JSON" in request_text(scripts["screen_analyst"].requests[1])


def test_analyst_invalid_twice_fails(make_rc):
    rc = make_rc()
    bad = {**INV_DICT, "screens": INV_DICT["screens"][:3]}
    state, scripts = run(rc, {"screen_analyst": Script(bad)})
    assert state["result"]["ok"] is False and "analyst output invalid" in state["result"]["error"]
    assert rc.budget.used == 2 and not scripts["flow_curator"].requests
    assert json.loads((rc.out_dir / "run_report.json").read_text())["skipped_reason"].startswith("pipeline failed")


def test_two_specialists_fail_still_proceeds(make_rc):
    rc = make_rc()
    state, _ = run(rc, {"validation_agent": Script(RuntimeError("down")), "business_rule_agent": Script("garbage")})
    assert state["result"]["ok"] and rc.budget.used == 8
    assert any(w.startswith("VL: unusable") for w in rc.warnings) and any(w.startswith("BR:") for w in rc.warnings)


def test_three_specialists_fail_aborts(make_rc):
    rc = make_rc()
    boom = Script(RuntimeError("provider down"))
    state, scripts = run(rc, {"validation_agent": boom, "business_rule_agent": Script("garbage"),
                              "state_condition_agent": Script({"error": "x"})})
    assert state["result"]["ok"] is False and "only 2 of 5 specialists" in state["result"]["error"]
    assert rc.budget.used == 6 and not scripts["flow_curator"].requests


def test_curator_retry_then_ok(make_rc):
    rc = make_rc()
    state, scripts = run(rc, {"flow_curator": Script({"questions": []}, CURATED)})
    assert state["curated_by"] == "curator" and rc.budget.used == 9
    assert "never return []" in request_text(scripts["flow_curator"].requests[1])


def test_curator_invalid_twice_falls_back(make_rc):
    rc = make_rc()
    state, _ = run(rc, {"flow_curator": Script("nope")})
    assert state["result"]["ok"] and state["curated_by"] == "fallback" and rc.budget.used == 9
    assert any("deterministic fallback" in w for w in rc.warnings)


def test_retry_pool_is_shared_analyst_first(make_rc):
    rc = make_rc()
    state, scripts = run(rc, {"screen_analyst": Script("bad", INV_DICT), "flow_curator": Script("nope")})
    assert state["curated_by"] == "fallback" and len(scripts["flow_curator"].requests) == 1
    assert rc.budget.used == 9


def test_auditor_invalid_patch_is_skipped(make_rc):
    rc = make_rc()
    state, _ = run(rc, {"auditor": Script(RuntimeError("503"))})
    assert state["result"]["ok"] and len(state["questions"]) == 6
    assert any(w.startswith("auditor patch skipped") for w in rc.warnings)


def test_auditor_patch_applied(make_rc):
    rc = make_rc()
    patch = {"remove": [{"n": 1, "reason": "duplicate"}],
             "add": [{"category": "STATE", "element_refs": ["S4.E2"], "question": "When is the order number shown?",
                      "answer_hint": "rule", "owner": "product"}], "verdict": "patched"}
    state, _ = run(rc, {"auditor": Script(patch)})
    qs = state["questions"]
    assert len(qs) == 6 and any(q["source_agents"] == ["AUD"] for q in qs)
    assert [q["n"] for q in qs] == list(range(1, 7))


def test_cap_of_8_means_no_retry(make_rc):
    rc = make_rc(max_llm_calls="8")
    state, _ = run(rc, {"screen_analyst": Script("bad", INV_DICT)})
    assert state["result"]["ok"] is False and rc.budget.used == 1


class FakeJira:
    def __init__(self, marker=False, error=None):
        self.marker, self.error, self.posts = marker, error, []

    def has_marker(self, key, marker):
        if self.error:
            raise self.error
        return self.marker

    def post_comment(self, key, body):
        self.posts.append((key, body))
        return {"id": "1"}


@pytest.mark.parametrize("jira, force, posted, skipped, error", [
    (FakeJira(), False, True, None, None),
    (FakeJira(marker=True), False, False, "marker already on the ticket (use --force to post again)", None),
    (FakeJira(marker=True), True, True, None, None),
    (FakeJira(error=JiraError("Issue SCRUM-1 not found", 404)), False, False, None, "Issue SCRUM-1 not found"),
])
def test_publish_paths(make_rc, jira, force, posted, skipped, error):
    rc = make_rc(jira=jira, force="true" if force else "")
    rc.settings.force = force
    state, _ = run(rc)
    result = state["result"]
    assert (result["posted"], result["skipped_reason"], result["jira_error"]) == (posted, skipped, error)
    assert len(jira.posts) == (1 if posted else 0)
    assert (rc.out_dir / "comment.txt").exists()  # saved even when Jira fails (exit 3)
    if posted:
        assert rc.marker in jira.posts[0][1]


def test_graph_matches_architecture(make_rc):
    """T4.4: the edge list equals the A§5 diagram (drift guard)."""
    rc = make_rc()
    wf = build_workflow(rc, stub_agents(rc)[0])
    got = set(edges(wf))
    expected = {
        ("__START__", "build_analyst_input", None), ("build_analyst_input", "screen_analyst", None),
        ("screen_analyst", "validate_inventory", None), ("validate_inventory", "build_panel_input", "OK"),
        ("validate_inventory", "build_analyst_input", "RETRY"), ("validate_inventory", "fail", "ABORT"),
        ("build_panel_input", "specialist_panel", None), ("specialist_panel", "merge_candidates", None),
        ("merge_candidates", "build_curator_input", "OK"), ("merge_candidates", "fail", "ABORT"),
        ("build_curator_input", "flow_curator", None), ("flow_curator", "validate_curated", None),
        ("validate_curated", "coverage_check", "OK"), ("validate_curated", "build_curator_input", "RETRY"),
        ("validate_curated", "fallback_curate", "FALLBACK"), ("fallback_curate", "coverage_check", None),
        ("coverage_check", "build_audit_input", None), ("build_audit_input", "auditor", None),
        ("auditor", "apply_patch", None), ("apply_patch", "finalize", None), ("finalize", "publish", None),
    } | {(f"specialist_panel.{a}", f"specialist_panel.{b}", None)
         for n, _, _ in SPECIALISTS for a, b in (("__START__", n), (n, "join_panel"))}
    assert got == expected
    assert wf.graph.nodes[[n.name for n in wf.graph.nodes].index("specialist_panel")].max_concurrency == 5
