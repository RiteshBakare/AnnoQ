"""Live dev harness for prompt tuning (T2.4 / T2.5). Needs keys + models in .env. Not collected by pytest.

  python -m tests.live.dev_stages analyst flows/shopping      # 1 Gemini call  -> outputs/dev/shopping/flow_inventory.json
  python -m tests.live.dev_stages panel   flows/shopping      # 5+1 Groq calls -> candidates.json, curated.json

Runs the real Agent nodes inside small ADK Workflows, with the real budget/logging callbacks.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

from google.adk import Event, Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types
from pydantic import ValidationError

from src.agents import SPECIALIST_TAGS, build_agents, build_specialist_panel
from src.config import load_settings
from src.image_utils import load_flow
from src.nodes import analyst_input, curator_input, max_candidates, panel_input
from src.prompts import extract_json
from src.run_context import RunContext
from src.schemas import Candidate, FlowInventory

OUT = Path("outputs/dev")


async def _run(wf: Workflow) -> dict:
    svc = InMemorySessionService()
    runner = Runner(node=wf, app_name="dev", session_service=svc)
    session = await svc.create_session(app_name="dev", user_id="dev")
    async for _ in runner.run_async(user_id="dev", session_id=session.id,
                                    new_message=types.Content(role="user", parts=[types.Part(text="start")])):
        pass
    return dict((await svc.get_session(app_name="dev", user_id="dev", session_id=session.id)).state)


def make_rc(flow_dir: str) -> RunContext:
    s = load_settings({"file_path": flow_dir, "dry_run": True})
    flow = load_flow(flow_dir, s.max_screens, s.max_image_mb, s.max_image_edge)
    return RunContext(settings=s, flow=flow, jira_key="DEV-1")


def print_calls(rc: RunContext) -> None:
    for c in rc.calls:
        print(f"  call {c.agent:22s} {c.model:28s} {c.seconds:6.1f}s  in={c.prompt_tokens} out={c.output_tokens} ok={c.ok}")
    for w in rc.warnings:
        print("  WARN", w)


def run_analyst(flow_dir: str) -> None:
    rc = make_rc(flow_dir)
    a = build_agents(rc)

    def build(node_input):
        return Event(output=analyst_input(rc.flow))

    def capture(node_input):
        return Event(output=None, state={"raw": node_input})

    t0 = time.time()
    state = asyncio.run(_run(Workflow(name="dev_analyst", edges=[(START, build, a.screen_analyst, capture)])))
    out = OUT / rc.flow.hint
    out.mkdir(parents=True, exist_ok=True)
    raw = state["raw"]
    (out / "analyst_raw.txt").write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    print(f"[{rc.flow.hint}] analyst {time.time() - t0:.1f}s")
    print_calls(rc)
    try:
        inv = FlowInventory.model_validate(extract_json(raw))
    except (ValueError, ValidationError) as e:
        print("  INVALID inventory:", str(e)[:800])
        return
    (out / "flow_inventory.json").write_text(inv.model_dump_json(indent=1), encoding="utf-8")
    ids = [s.id for s in inv.screens]
    print(f"  screens {ids} (expected {[s.id for s in rc.flow.screens]})  name: {inv.flow_name!r}")
    for s in inv.screens:
        flagged = sum(e.needs_clarification for e in s.elements)
        print(f"  {s.id} {s.name!r:28s} {s.complexity:6s} elements={len(s.elements):2d} flagged={flagged}")
    print("  transitions:", [(t.from_screen, t.to_screen, t.trigger) for t in inv.transitions])
    for c in inv.recurring_components:
        print(f"  {c.id} {c.name!r} on {c.appears_on} refs={c.element_refs}")
    print("  ambiguities:", inv.ambiguities[:6])


def merge(joined: dict, inv: FlowInventory) -> tuple[list[dict], dict]:
    """Harness-only merge (the real one is T3.1): parse, schema-validate, tag source, check refs exist."""
    known = {"FLOW"} | {s.id for s in inv.screens} | {e.id for s in inv.screens for e in s.elements} | {
        rc.id for rc in inv.recurring_components}
    out, stats = [], {}
    for agent, raw in joined.items():
        tag = SPECIALIST_TAGS.get(agent, agent)
        ok = bad = 0
        try:
            items = extract_json(raw).get("candidates", [])
        except (ValueError, AttributeError) as e:
            stats[tag] = f"UNUSABLE ({e})"
            continue
        for item in items:
            try:
                c = Candidate.model_validate({**item, "source_agents": [tag]})
                missing = [r for r in c.element_refs + c.screens if r not in known]
                if missing:
                    raise ValueError(f"unknown refs {missing}")
                out.append(c.model_dump())
                ok += 1
            except (ValidationError, ValueError) as e:
                bad += 1
                detail = "; ".join(f"{'.'.join(map(str, x['loc']))}={x['msg']}" for x in e.errors())                     if isinstance(e, ValidationError) else str(e)
                print(f"    drop {tag}: {detail[:200]}")
        stats[tag] = f"{ok} ok, {bad} dropped"
    return out, stats


def run_panel(flow_dir: str) -> None:
    rc = make_rc(flow_dir)
    out = OUT / rc.flow.hint
    inv = FlowInventory.model_validate_json((out / "flow_inventory.json").read_text(encoding="utf-8"))
    a = build_agents(rc)
    panel = build_specialist_panel(a, rc)

    def build_panel(node_input):
        return Event(output=panel_input(inv))

    def merge_node(node_input):
        (out / "panel_raw.json").write_text(json.dumps(node_input, indent=1, ensure_ascii=False), encoding="utf-8")
        cands, stats = merge(node_input, inv)
        return Event(output=curator_input(inv, cands, rc.settings), state={"cands": cands, "stats": stats})

    def capture(node_input):
        return Event(output=None, state={"curated_raw": node_input})

    t0 = time.time()
    state = asyncio.run(_run(Workflow(name="dev_panel", edges=[
        (START, build_panel, panel, merge_node, a.flow_curator, capture)])))
    print(f"[{rc.flow.hint}] panel+curator {time.time() - t0:.1f}s  max_candidates={max_candidates(len(inv.screens))}")
    print_calls(rc)
    print("  per agent:", state["stats"])
    cands = state["cands"]
    (out / "candidates.json").write_text(json.dumps(cands, indent=1, ensure_ascii=False), encoding="utf-8")
    raw = state["curated_raw"]
    (out / "curator_raw.txt").write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    try:
        qs = [Candidate.model_validate(q) for q in extract_json(raw)["questions"]]
    except (ValueError, ValidationError, KeyError, TypeError) as e:
        print("  INVALID curated:", str(e)[:800])
        return
    (out / "curated.json").write_text(json.dumps([q.model_dump() for q in qs], indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    print(f"  candidates={len(cands)} scope={dict(Counter(c['scope'] for c in cands))}")
    print(f"  curated={len(qs)} scope={dict(Counter(q.scope for q in qs))} "
          f"per_screen={dict(Counter(q.screens[0] for q in qs if q.scope == 'SCREEN'))}")
    for q in qs:
        print(f"   [{q.scope:6s} {','.join(q.screens):11s} {q.category:13s} {q.owner:7s} {q.priority} "
              f"{'/'.join(q.source_agents)}] {q.question}")


if __name__ == "__main__":
    stage, flow_dir = sys.argv[1], sys.argv[2]
    {"analyst": run_analyst, "panel": run_panel}[stage](flow_dir)
