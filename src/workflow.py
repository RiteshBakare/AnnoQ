"""The whole topology in one edge list (spec §9, A§5), plus the runner helper and a Mermaid dump (T4.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk import Runner, Workflow
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START
from google.genai import types

from .agents import Agents, build_agents, build_specialist_panel
from .nodes import build_nodes

if TYPE_CHECKING:
    from .run_context import RunContext


def build_workflow(rc: RunContext, agents: Agents | None = None) -> Workflow:
    """`agents` lets tests inject stub-model agents; production builds the real ones from .env."""
    a = agents or build_agents(rc)
    n = build_nodes(rc)
    specialist_panel = build_specialist_panel(a, rc)
    return Workflow(
        name="flow_questions",
        edges=[
            (START, n.build_analyst_input, a.screen_analyst, n.validate_inventory),
            (n.validate_inventory, {"OK": n.build_panel_input, "RETRY": n.build_analyst_input, "ABORT": n.fail}),

            (n.build_panel_input, specialist_panel, n.merge_candidates),
            (n.merge_candidates, {"OK": n.build_curator_input, "ABORT": n.fail}),

            (n.build_curator_input, a.flow_curator, n.validate_curated),
            (n.validate_curated, {"OK": n.coverage_check, "RETRY": n.build_curator_input,
                                  "FALLBACK": n.fallback_curate}),
            (n.fallback_curate, n.coverage_check),

            (n.coverage_check, n.build_audit_input, a.auditor, n.apply_patch, n.finalize, n.publish),
        ],
    )


async def run_workflow(rc: RunContext, agents: Agents | None = None) -> dict:
    """Run the graph once; return the final session state (`state["result"]` holds the outcome)."""
    svc = InMemorySessionService()
    runner = Runner(node=build_workflow(rc, agents), app_name="flow_questions", session_service=svc)
    session = await svc.create_session(app_name="flow_questions", user_id="cli")
    async for _ in runner.run_async(user_id="cli", session_id=session.id,
                                    new_message=types.Content(role="user", parts=[types.Part(text="start")])):
        pass
    final = await svc.get_session(app_name="flow_questions", user_id="cli", session_id=session.id)
    return dict(final.state)


def edges(wf: Workflow) -> list[tuple[str, str, str | None]]:
    """(from, to, route) for the workflow and any nested Workflow nodes (prefixed with the nested name)."""
    out = []
    for e in wf.graph.edges:
        route = "|".join(map(str, e.route)) if isinstance(e.route, list) else e.route
        out.append((e.from_node.name, e.to_node.name, route))
        if isinstance(e.to_node, Workflow) and e.to_node is not wf:
            out += [(f"{e.to_node.name}.{a}", f"{e.to_node.name}.{b}", r) for a, b, r in edges(e.to_node)]
    return list(dict.fromkeys(out))


def to_mermaid(wf: Workflow) -> str:
    def node_id(name: str) -> str:
        return name.replace(".", "__").replace("__START__", "START")

    lines = ["flowchart TD"]
    for a, b, route in edges(wf):
        arrow = f" -- {route} --> " if route else " --> "
        lines.append(f"    {node_id(a)}{arrow}{node_id(b)}")
    return "\n".join(lines)


if __name__ == "__main__":  # T4.4: python -m src.workflow [flow_dir]  -> Mermaid of the real graph (no LLM calls)
    import sys

    from .config import load_settings
    from .image_utils import load_flow
    from .run_context import RunContext

    settings = load_settings({"file_path": sys.argv[1] if len(sys.argv) > 1 else None, "dry_run": True})
    flow = load_flow(settings.file_path, settings.max_screens, settings.max_image_mb, settings.max_image_edge)
    print(to_mermaid(build_workflow(RunContext(settings=settings, flow=flow, jira_key="PRINT-1"))))
