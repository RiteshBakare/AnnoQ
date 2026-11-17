"""T2.3: agent construction (offline, no model calls) and the Groq rate-limit wrapper."""

import asyncio

import litellm
import pytest
from google.adk import Workflow
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from src import models
from src.agents import SPECIALISTS, build_agents, build_specialist_panel
from src.config import load_settings
from src.image_utils import load_flow
from src.models import build_model, retry_after_seconds
from src.run_context import RunContext

ENV = {"VISION_MODEL": "gemini-v", "TEXT_MODEL": "groq/t", "MODEL_CURATOR": "groq/big",
       "GOOGLE_API_KEY": "x", "GROQ_API_KEY": "y", "TEMPERATURE_TEXT": "0.4", "PANEL_CONCURRENCY": "3"}


@pytest.fixture
def rc(make_flow_dir):
    s = load_settings(env_file=None, environ=ENV)
    return RunContext(settings=s, flow=load_flow(make_flow_dir(["1.png", "2.png"])), jira_key="P-1")


def test_build_agents_names_models_and_configs(rc):
    a = build_agents(rc)
    assert a.screen_analyst.name == "screen_analyst" and a.auditor.name == "auditor"
    assert [x.name for x in a.specialists] == [n for n, _, _ in SPECIALISTS]
    assert a.flow_curator.name == "flow_curator"
    for vision in (a.screen_analyst, a.auditor):
        assert vision.model == "gemini-v" and vision.mode == "single_turn"
        assert vision.generate_content_config.response_mime_type == "application/json"
        assert vision.generate_content_config.temperature == 0.2
    for text in (*a.specialists, a.flow_curator):
        assert isinstance(text.model, LiteLlm) and text.mode == "single_turn"
        assert text.generate_content_config.temperature == 0.4
        assert text.generate_content_config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert a.flow_curator.model.model == "groq/big"
    assert rc.agent_models["data_source_agent"] == "groq/t" and rc.agent_models["auditor"] == "gemini-v"


def test_instructions_rendered_once_and_bypass_state_injection(rc):
    a = build_agents(rc)
    text = a.specialists[0].instruction(None)  # callable -> ADK skips {state} injection on the JSON braces
    assert "DATA SOURCE (DS)" in text and '{"candidates"' in text
    assert "screen_analyst" not in text


def test_only_vision_agents_attach_images(rc):
    a = build_agents(rc)

    def contents_after_before_cb(agent):
        req = type("Req", (), {"contents": [], "model": "m"})()
        agent.before_model_callback(type("Ctx", (), {"agent_name": agent.name})(), req)
        return sum(1 for c in req.contents for p in c.parts if p.inline_data)

    assert contents_after_before_cb(a.screen_analyst) == 2
    assert contents_after_before_cb(a.auditor) == 2
    assert all(contents_after_before_cb(x) == 0 for x in (*a.specialists, a.flow_curator))


def test_specialist_panel_workflow(rc):
    panel = build_specialist_panel(build_agents(rc), rc)
    assert isinstance(panel, Workflow) and panel.name == "specialist_panel" and panel.max_concurrency == 3
    names = {n.name for n in panel.graph.nodes}
    assert {"join_panel", "data_source_agent", "business_rule_agent"} <= names


@pytest.mark.parametrize("msg, expected", [
    ("Please try again in 19.7325s. Need more tokens", 20.2325),
    ("Please try again in 735ms.", 1.235),
    ("Please try again in 1m2.5s.", 63.0),
    ("no hint", 5.0),
])
def test_retry_after_seconds(msg, expected):
    assert retry_after_seconds(Exception(msg)) == pytest.approx(expected)


def rate_limit_error(msg="Rate limit reached. Please try again in 2s."):
    return litellm.RateLimitError(message=msg, llm_provider="groq", model="groq/t")


def test_rate_limited_model_waits_and_retries(monkeypatch):
    attempts, sleeps = [], []

    async def fake_generate(self, llm_request, stream=False):
        attempts.append(1)
        if len(attempts) < 3:
            raise rate_limit_error()
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="{}")]))

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_generate)
    monkeypatch.setattr(models.asyncio, "sleep", fake_sleep)

    async def run():
        return [r async for r in build_model("groq/t").generate_content_async(None)]

    out = asyncio.run(run())
    assert len(out) == 1 and len(attempts) == 3 and sleeps == [2.5, 2.5]


def test_rate_limited_model_gives_up_after_max_wait(monkeypatch):
    async def always_429(self, llm_request, stream=False):
        raise rate_limit_error("Please try again in 50s.")
        yield  # pragma: no cover

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(LiteLlm, "generate_content_async", always_429)
    monkeypatch.setattr(models.asyncio, "sleep", fake_sleep)

    async def run():
        return [r async for r in build_model("groq/t").generate_content_async(None)]

    with pytest.raises(litellm.RateLimitError):
        asyncio.run(run())  # 50.5s waited once, a second 50.5s would exceed the 90s cap
