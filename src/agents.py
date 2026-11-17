"""Builds the 8 Agent nodes (spec §7) and the nested `specialist_panel` Workflow.

System instructions are rendered ONCE here from templates/agents/*.j2. They are passed as an instruction provider
(a callable) so ADK does not try to inject session state into the JSON braces inside the prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from google.adk import Agent, Workflow
from google.adk.workflow import JoinNode, START
from google.genai import types

from .budget import make_callbacks
from .config import export_provider_env
from .models import build_model, is_gemini, validate_slots
from .prompts import render_agent
from .schemas import ElementType

if TYPE_CHECKING:
    from .run_context import RunContext

# (agent name, template, tag) — names are the JoinNode output keys, tags go into source_agents
SPECIALISTS: tuple[tuple[str, str, str], ...] = (
    ("data_source_agent", "ds", "DS"),
    ("interaction_agent", "ia", "IA"),
    ("validation_agent", "vl", "VL"),
    ("state_condition_agent", "st", "ST"),
    ("business_rule_agent", "br", "BR"),
)
SPECIALIST_TAGS = {name: tag for name, _, tag in SPECIALISTS}
TEXT_MAX_OUTPUT_TOKENS = 4096
# Bounded Gemini transport policy (T6.3): without it a 503 "high demand" made the SDK retry for ~180 s. One retry after
# 3 s on 429/5xx, 75 s timeout per attempt; a final failure becomes the error stub (analyst -> RETRY/ABORT route,
# auditor -> patch skipped). Transport retries are below the model callbacks, so the LLM budget counts one call.
GEMINI_HTTP = types.HttpOptions(
    timeout=75_000,
    retry_options=types.HttpRetryOptions(attempts=2, initial_delay=3, max_delay=10,
                                         http_status_codes=[429, 500, 502, 503, 504]))


@dataclass(frozen=True)
class Agents:
    screen_analyst: Agent
    specialists: tuple[Agent, ...]
    flow_curator: Agent
    auditor: Agent


def _make_agent(rc: RunContext, name: str, model_name: str, instruction: str, vision: bool) -> Agent:
    gemini = is_gemini(model_name)
    if vision != gemini:  # defence in depth; validate_slots already guarantees this
        raise ValueError(f"{name}: vision agents must use Gemini and text agents Groq (got {model_name!r})")
    s = rc.settings
    if gemini:
        config = types.GenerateContentConfig(temperature=s.temperature_vision,
                                             response_mime_type="application/json",  # Gemini JSON mode
                                             http_options=GEMINI_HTTP)
    else:
        # Low reasoning effort (LiteLLM maps it to reasoning_effort where supported) keeps output tokens small:
        # without it gpt-oss spent the whole output budget on reasoning and returned no JSON, and every token
        # counts against Groq's tokens-per-minute limit.
        config = types.GenerateContentConfig(
            temperature=s.temperature_text, max_output_tokens=TEXT_MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))
    rc.agent_models[name] = model_name
    return Agent(
        name=name,
        model=build_model(model_name),  # Groq: prompt-enforced JSON (json_object mode fails on gpt-oss)
        mode="single_turn",
        instruction=lambda _ctx, text=instruction: text,
        generate_content_config=config,
        **make_callbacks(rc, attach_images=vision),  # only vision agents ever get image parts
    )


def build_agents(rc: RunContext) -> Agents:
    slots = validate_slots(rc.settings)
    export_provider_env(rc.settings)
    analyst = _make_agent(rc, "screen_analyst", slots["analyst"],
                          render_agent("analyst", element_types=list(get_args(ElementType))), vision=True)
    specialists = tuple(_make_agent(rc, name, slots["specialists"], render_agent(tpl), vision=False)
                        for name, tpl, _ in SPECIALISTS)
    curator = _make_agent(rc, "flow_curator", slots["curator"], render_agent("curator"), vision=False)
    auditor = _make_agent(rc, "auditor", slots["auditor"], render_agent("auditor"), vision=True)
    return Agents(analyst, specialists, curator, auditor)


def build_specialist_panel(agents: Agents, rc: RunContext) -> Workflow:
    """Nested Workflow: the 5 specialists in parallel -> JoinNode ({agent_name: output})."""
    return Workflow(
        name="specialist_panel",
        edges=[(START, agents.specialists, JoinNode(name="join_panel"))],
        max_concurrency=rc.settings.panel_concurrency,
    )
