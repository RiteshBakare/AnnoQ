"""Graph function nodes / routers (spec §9, A§5).

Part 1: pure input builders (T2.2) that turn run data into the next Agent node's user message.
Part 2: `build_nodes(rc)` -> the 13 function nodes, closed over the RunContext. Session state stays JSON-only;
images, budget and settings live in the RunContext. Routers return Event(route=OK|RETRY|FALLBACK|ABORT).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from google.adk import Event

from . import postprocess as pp
from .agents import SPECIALIST_TAGS
from .budget import PLANNED_CALLS
from .config import Settings
from .image_utils import Flow
from .jira_client import JiraError
from .prompts import render_input, render_template
from .schemas import Candidate, FlowInventory, Question, RunReport

if TYPE_CHECKING:
    from .run_context import RunContext

log = logging.getLogger(__name__)
COMPLEXITY_TARGET = {"low": "2-3", "medium": "4-6", "high": "6-8"}
MIN_USABLE_SPECIALISTS = 3


# =========================================================================== input builders (T2.2)

def max_candidates(n_screens: int) -> int:
    """Per-specialist candidate limit (spec §8.5)."""
    return min(12, 2 + 2 * n_screens)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def panel_inventory_json(inv: FlowInventory) -> str:
    """Full inventory without default-valued fields: same facts, far fewer tokens for 5 parallel Groq calls."""
    return compact_json(inv.model_dump(exclude_defaults=True))


def compact_inventory(inv: FlowInventory) -> dict:
    """What the curator/auditor need: screens, repeats, transitions, flagged elements (id + label)."""
    return {
        "flow_name": inv.flow_name,
        "screens": [{"id": s.id, "name": s.name, "complexity": s.complexity} for s in inv.screens],
        "recurring_components": [{"id": rc.id, "name": rc.name, "appears_on": rc.appears_on,
                                  "element_refs": rc.element_refs} for rc in inv.recurring_components],
        "transitions": [{"from": t.from_screen, "to": t.to_screen, "trigger": t.trigger} for t in inv.transitions],
        "flagged": [{"id": e.id, "label": e.label} for s in inv.screens for e in s.elements if e.needs_clarification],
    }


def curator_targets(inv: FlowInventory, settings: Settings) -> dict:
    """Per-screen ranges by complexity, flow-level range, and the expected total (sum of ranges, capped)."""
    multi = len(inv.screens) > 1
    per_screen = {s.id: COMPLEXITY_TARGET[s.complexity] for s in inv.screens}
    flow_lo, flow_hi = (2, settings.max_flow_q) if multi and settings.max_flow_q >= 2 else (0, 0)
    lo = flow_lo + sum(int(r.split("-")[0]) for r in per_screen.values())
    hi = flow_hi + sum(int(r.split("-")[1]) for r in per_screen.values())
    return {
        "per_screen": per_screen,
        "flow_level": f"{flow_lo}-{flow_hi}",
        "total": f"{min(lo, settings.max_total_q)}-{min(hi, settings.max_total_q)}",
        "hard_caps": {"per_screen": settings.max_q_per_screen, "flow": settings.max_flow_q if multi else 0,
                      "total": settings.max_total_q},
    }


def analyst_input(flow: Flow, ticket_context: str = "", repair: str = "") -> str:
    screens = [{"id": s.id, "file": s.file, "hint": s.hint} for s in flow.screens]
    return render_input("analyst_input", flow_hint=flow.hint, ticket_context=ticket_context,
                        screens=screens, repair=repair)


def panel_input(inv: FlowInventory, ticket_context: str = "") -> str:
    n = len(inv.screens)
    recurring = [{"id": rc.id, "name": rc.name, "appears_on": rc.appears_on} for rc in inv.recurring_components]
    return render_input("panel_input", inventory_json=panel_inventory_json(inv), max_candidates=max_candidates(n),
                        n_screens=n, recurring=recurring, ticket_context=ticket_context)


def curator_input(inv: FlowInventory, candidates: list[dict], settings: Settings, repair: str = "") -> str:
    return render_input("curator_input", inventory=compact_inventory(inv), candidates=candidates,
                        targets=curator_targets(inv, settings), n_screens=len(inv.screens), repair=repair)


def auditor_input(questions: list[dict], inv: FlowInventory, uncovered: list[str]) -> str:
    return render_input("auditor_input", questions=questions, inventory=compact_inventory(inv), uncovered=uncovered)


# =========================================================================== function nodes (T4.1)

def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")


def build_nodes(rc: RunContext) -> SimpleNamespace:
    """The graph's function nodes. Node name = function name; first parameter = previous node's output; the other
    parameters are bound from session state (all have defaults, so a missing key never breaks binding)."""
    s = rc.settings
    retry_pool = max(0, rc.budget.max - PLANNED_CALLS)  # spare calls above the 8 planned; analyst first, then curator

    def inv_of(data: Any) -> FlowInventory:
        return FlowInventory.model_validate(data)

    def dumps(items: list) -> list[dict]:
        return [x.model_dump() for x in items]

    def warn(messages: list[str]) -> None:
        for m in messages:
            rc.warn(m)

    def write_report(questions: list[dict], posted: bool, skipped: str | None) -> Path:
        report = RunReport(jira_key=rc.jira_key, flow_sha=rc.flow.sha, screens=rc.n_screens,
                           llm_calls=rc.budget.used, calls=rc.calls, question_count=len(questions),
                           common_count=sum(q["scope"] == "FLOW" for q in questions), posted=posted,
                           skipped_reason=skipped, warnings=rc.warnings)
        path = rc.output_dir() / "run_report.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return path

    # --- analyst ------------------------------------------------------------------------------
    def build_analyst_input(node_input, analyst_repair=""):
        return Event(output=analyst_input(rc.flow, rc.ticket_context, analyst_repair))

    def validate_inventory(node_input, retries_used=0):
        try:
            inv, warnings = pp.validate_inventory(node_input, rc.flow)
        except pp.InvalidOutput as e:
            if retries_used < retry_pool:
                rc.warn(f"analyst output invalid, retrying once: {e}")
                return Event(state={"retries_used": retries_used + 1, "analyst_repair": str(e)}, route="RETRY")
            return Event(state={"fail_reason": f"analyst output invalid: {e}"}, route="ABORT")
        warn(warnings)
        data = inv.model_dump()
        return Event(output=data, state={"inventory": data}, route="OK")

    # --- specialist panel ---------------------------------------------------------------------
    def build_panel_input(node_input):
        return Event(output=panel_input(inv_of(node_input), rc.ticket_context))

    def merge_candidates(node_input, inventory=None):
        cands, usable, warnings = pp.merge_candidates(node_input or {}, inv_of(inventory), SPECIALIST_TAGS)
        warn(warnings)
        if usable < MIN_USABLE_SPECIALISTS:
            return Event(state={"fail_reason": f"only {usable} of {len(SPECIALIST_TAGS)} specialists returned "
                                               f"usable output (need {MIN_USABLE_SPECIALISTS})"}, route="ABORT")
        data = dumps(cands)
        return Event(output=data, state={"candidates": data}, route="OK")

    # --- curator ------------------------------------------------------------------------------
    def build_curator_input(node_input, inventory=None, candidates=None, curator_repair=""):
        return Event(output=curator_input(inv_of(inventory), candidates or [], s, curator_repair))

    def validate_curated(node_input, inventory=None, candidates=None, retries_used=0):
        try:
            questions, warnings = pp.parse_curated(node_input, inv_of(inventory), len(candidates or []))
        except pp.InvalidOutput as e:
            if retries_used < retry_pool:
                rc.warn(f"curator output invalid, retrying once: {e}")
                return Event(state={"retries_used": retries_used + 1, "curator_repair": str(e)}, route="RETRY")
            rc.warn(f"curator output unusable ({e}); using the deterministic fallback")
            return Event(output=candidates or [], route="FALLBACK")
        warn(warnings)
        data = dumps(questions)
        return Event(output=data, state={"curated": data, "curated_by": "curator"}, route="OK")

    def fallback_curate(node_input, inventory=None, candidates=None):
        questions, warnings = pp.fallback_curate([Candidate(**c) for c in candidates or []], inv_of(inventory), s)
        warn(warnings)
        data = dumps(questions)
        return Event(output=data, state={"curated": data, "curated_by": "fallback"})

    # --- coverage + auditor -------------------------------------------------------------------
    def coverage_check(node_input, inventory=None):
        inv = inv_of(inventory)
        questions, merged = pp.dedupe_across_screens([Candidate(**q) for q in node_input or []], inv)
        if merged:
            log.info("code dedupe merged %d cross-screen duplicate(s)", merged)
        numbered = dumps(pp.order_and_number(questions, inv))
        return Event(output=numbered, state={"audit_questions": numbered, "coverage": pp.coverage(questions, inv)})

    def build_audit_input(node_input, inventory=None, coverage=None):
        return Event(output=auditor_input(node_input or [], inv_of(inventory), coverage or []))

    def apply_patch(node_input, inventory=None, audit_questions=None):
        questions = [Question(**q) for q in audit_questions or []]
        patched, warnings = pp.apply_patch(questions, node_input, inv_of(inventory))
        warn(warnings)
        raw = node_input if isinstance(node_input, str) else json.dumps(node_input)
        return Event(output=dumps(patched), state={"audit_raw": raw})

    # --- finalize + publish -------------------------------------------------------------------
    def finalize(node_input, inventory=None):
        inv = inv_of(inventory)
        capped, warnings = pp.apply_caps([Candidate(**q) for q in node_input or []], inv, s)
        numbered = pp.order_and_number(capped, inv)
        warn(warnings + pp.floor_warnings(numbered, inv))

        def render(qs: list[Question]) -> str:
            return render_template("jira_comment",
                                   **pp.comment_context(qs, inv, s.owner_labels, rc.run_id, rc.marker))

        final, comment, size_warnings = pp.fit_to_size(numbered, render, s.jira_comment_max_chars)
        warn(size_warnings)
        return Event(output=comment, state={"questions": dumps(final), "comment": comment})

    def publish(node_input, inventory=None, candidates=None, curated=None, audit_raw="", questions=None):
        out, comment, questions = rc.output_dir(), node_input or "", questions or []
        (out / "comment.txt").write_text(comment, encoding="utf-8")  # saved BEFORE any POST, so exit 3 keeps it
        _write_json(out / "flow_inventory.json", inventory)
        _write_json(out / "candidates.json", candidates or [])
        _write_json(out / "curated.json", curated or [])
        _write_json(out / "questions.json", questions)  # final numbered list, exactly as in the comment
        try:
            audit = json.loads(audit_raw) if audit_raw else {}
        except json.JSONDecodeError:
            audit = {"raw": audit_raw}
        _write_json(out / "audit.json", audit)

        posted, skipped, jira_error = False, None, None
        if s.dry_run:
            skipped = "dry-run"
        elif rc.jira is None:
            jira_error = "Jira is not configured (JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN)"
        else:
            try:
                if not s.force and rc.jira.has_marker(rc.jira_key, rc.marker):
                    skipped = "marker already on the ticket (use --force to post again)"
                else:
                    rc.jira.post_comment(rc.jira_key, comment)  # exactly one POST, never retried
                    posted = True
            except JiraError as e:
                jira_error = str(e)
        if jira_error:
            rc.warn(f"Jira: {jira_error}")
        report = write_report(questions, posted, skipped)
        return Event(state={"result": {"ok": True, "posted": posted, "skipped_reason": skipped,
                                       "jira_error": jira_error, "comment_path": str(out / "comment.txt"),
                                       "report_path": str(report)}})

    def fail(node_input, fail_reason="", inventory=None):
        if inventory:
            _write_json(rc.output_dir() / "flow_inventory.json", inventory)
        rc.warn(f"pipeline failed: {fail_reason}")
        report = write_report([], posted=False, skipped=f"pipeline failed: {fail_reason}")
        return Event(state={"result": {"ok": False, "posted": False, "error": fail_reason,
                                       "report_path": str(report)}})

    return SimpleNamespace(**{f.__name__: f for f in (
        build_analyst_input, validate_inventory, build_panel_input, merge_candidates, build_curator_input,
        validate_curated, fallback_curate, coverage_check, build_audit_input, apply_patch, finalize, publish, fail)})
