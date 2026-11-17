"""T0.5/T0.6 live spike — needs GOOGLE_API_KEY, GROQ_API_KEY, VISION_MODEL, TEXT_MODEL. Not collected by pytest.

Run:  python -m tests.spikes.live_models
Checks:
  A5  Agent(model=LiteLlm("groq/..."), mode="single_turn") as a workflow node, Jinja2-rendered user message,
      JSON output with and without response_format=json_object, callbacks fire, usage tokens reported.
  A6  a Groq branch with a broken model id -> on_model_error_callback stub -> JoinNode still completes.
  T0.6 Gemini Agent node receives 4 images attached in before_model_callback; Groq nodes receive none.
"""

from __future__ import annotations

import io
import json
import os
import time

from dotenv import load_dotenv
from google.adk import Agent, Event, Workflow
from google.adk.models.lite_llm import LiteLlm
from google.adk.workflow import JoinNode, START
from google.genai import types
from jinja2 import Environment, StrictUndefined
from PIL import Image, ImageDraw

from ._harness import run_workflow
from .test_t05_t06_callbacks import Budget, make_callbacks

load_dotenv(".env")
VISION, TEXT = os.environ.get("VISION_MODEL", ""), os.environ.get("TEXT_MODEL", "")
assert VISION.startswith("gemini") and TEXT.startswith("groq/"), "set VISION_MODEL=gemini-* and TEXT_MODEL=groq/*"

SCREENS = [("Cart", "red"), ("Shipping", "green"), ("Place order", "blue"), ("Success", "orange")]


def screen_png(title: str, color: str) -> bytes:
    img = Image.new("RGB", (360, 640), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 360, 90], fill=color)
    d.text((20, 35), title.upper(), fill="white")
    d.rectangle([20, 560, 340, 610], outline="black")
    d.text((140, 578), "Continue", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> None:
    budget = Budget(9)
    images = [types.Part.from_bytes(data=screen_png(t, c), mime_type="image/png") for t, c in SCREENS]
    seen_images: dict[str, int] = {}

    def counting(cbs, name):
        inner = cbs["before_model_callback"]

        def before(callback_context, llm_request):
            out = inner(callback_context, llm_request)
            seen_images[name] = sum(1 for c in llm_request.contents for p in (c.parts or []) if p.inline_data)
            return out

        return {**cbs, "before_model_callback": before}

    analyst = Agent(
        name="screen_analyst", model=VISION, mode="single_turn",
        instruction='Images of app screens are attached in order S1..Sn. Return ONLY JSON: '
                    '{"screens":[{"id":"S1","title":"<header text>","header_color":"<color>"}]}',
        **counting(make_callbacks(budget, images), "screen_analyst"),
    )

    jenv = Environment(undefined=StrictUndefined)
    panel_tpl = jenv.from_string("FLOW INVENTORY (JSON):\n{{ inventory_json }}\nlimits: {\"max_candidates\": {{ n }}}")

    def build_panel_input(node_input):
        return Event(output=panel_tpl.render(inventory_json=node_input, n=2))

    lens = ('You write clarification questions about this app flow. Return ONLY JSON: '
            '{"candidates":[{"question":"...?","screens":["S1"]}]} with at most limits.max_candidates items.')

    def groq_agent(name, model):
        return Agent(name=name, model=model, mode="single_turn", instruction=lens,
                     **counting(make_callbacks(budget, None), name))

    plain = groq_agent("ds_plain", LiteLlm(model=TEXT))
    json_mode = groq_agent("ia_json_mode", LiteLlm(model=TEXT, response_format={"type": "json_object"}))
    broken = groq_agent("vl_broken", LiteLlm(model="groq/does-not-exist-model"))
    panel = Workflow(name="specialist_panel", edges=[(START, (plain, json_mode, broken), JoinNode(name="join_panel"))])

    def merge(node_input):
        return Event(output=None, state={"joined": node_input})

    def capture(node_input):
        return Event(output=node_input, state={"analyst_out": node_input})

    wf = Workflow(name="live_spike", edges=[(START, analyst, capture, build_panel_input, panel, merge)])
    t0 = time.time()
    state, events = run_workflow(wf)
    print(f"elapsed {time.time() - t0:.1f}s, budget used {budget.used}")

    analyst_out = state.get("analyst_out")
    print("analyst output type:", type(analyst_out).__name__, "\n", str(analyst_out)[:600])
    print("images per request:", seen_images)
    for name, out in state["joined"].items():
        try:
            parsed = json.loads(out) if isinstance(out, str) else out
            print(f"{name}: {type(out).__name__} json-ok keys={list(parsed)}")
        except Exception as e:  # noqa: BLE001
            print(f"{name}: {type(out).__name__} NOT JSON ({type(e).__name__}): {str(out)[:200]}")
    for agent, usage in budget.log:
        tok = (usage.prompt_token_count, usage.candidates_token_count) if hasattr(usage, "prompt_token_count") else usage
        print("log:", agent, tok)


if __name__ == "__main__":
    main()
