"""Graph function nodes / routers (M4). This first part holds the pure input builders (T2.2) that the
`build_*_input` nodes call: they turn run data into the rendered user message of the next Agent node."""

from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .image_utils import Flow
from .prompts import render_input
from .schemas import FlowInventory

COMPLEXITY_TARGET = {"low": "2-3", "medium": "4-6", "high": "6-8"}


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
