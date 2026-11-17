"""LLM call budget and the three model callbacks shared by every Agent node.

before_model_callback  -> budget guard (+ attach all screen images for Gemini agents)
after_model_callback   -> usage logging (seconds, tokens)
on_model_error_callback -> log + stub JSON, so a failed panel branch still reaches the JoinNode
Every attempt (including retries) passes through `before`, so the cap covers retries too.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .config import HARD_LLM_CAP
from .schemas import LlmCallLog

if TYPE_CHECKING:
    from .run_context import RunContext

log = logging.getLogger(__name__)

PLANNED_CALLS = 8  # 1 analyst + 5 specialists + 1 curator + 1 auditor; the rest of the cap is the retry pool


class LLMBudget:
    """Thread/async-safe counter. Hard ceiling HARD_LLM_CAP (9) whatever the configured value."""

    def __init__(self, max_calls: int = HARD_LLM_CAP):
        self.max = max(0, min(max_calls, HARD_LLM_CAP))
        self.used = 0
        self.refused: list[str] = []
        self._lock = threading.Lock()

    def consume(self, agent: str) -> bool:
        with self._lock:
            if self.used >= self.max:
                self.refused.append(agent)
                return False
            self.used += 1
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.max - self.used


def stub_response(payload: dict[str, Any]) -> LlmResponse:
    """A model-shaped reply that validators will reject (it carries "error"), routing to retry/fallback/skip."""
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]))


def friendly_error(error: Exception, model: str) -> str:
    """One readable line for the run report / console (T6.3); the raw text goes to the debug log only."""
    text, name = str(error).lower(), type(error).__name__
    provider = "Groq" if model.startswith("groq/") else "Gemini" if model.startswith("gemini") else model
    if "ratelimit" in name.lower() or any(k in text for k in ("rate limit", "429", "resource_exhausted", "quota")):
        return (f"{provider} rate limit or quota reached ({name}) for {model}. Wait a minute and re-run, or move "
                f"to a paid tier / another model in .env.")
    if "503" in text or "unavailable" in text or "overloaded" in text or "high demand" in text:
        return f"{provider} is overloaded (503) for {model}. Re-run later or pick another model in .env."
    if "timeout" in name.lower() or "timed out" in text or "deadline" in text:
        return f"{provider} timed out for {model}."
    if "401" in text or "403" in text or "api key" in text or "authentication" in text:
        return f"{provider} rejected the API key for {model} ({name}). Check the key in .env."
    return f"model error {name} from {model}"


def make_callbacks(rc: RunContext, attach_images: bool) -> dict[str, Any]:
    """Callbacks for Agent(**make_callbacks(rc, attach_images=...)). Only vision agents may attach images."""

    def before_model_callback(callback_context, llm_request):
        agent = callback_context.agent_name
        if not rc.budget.consume(agent):
            rc.warn(f"LLM budget exhausted ({rc.budget.max} calls): call by {agent} refused")
            return stub_response({"error": "budget_exhausted"})
        if attach_images:
            llm_request.contents.append(types.Content(role="user", parts=list(rc.image_parts)))
        model = rc.agent_models.get(agent) or llm_request.model or "?"
        rc.start_call(agent, model)
        log.info("LLM call #%d -> %s (%s)", rc.budget.used, agent, model)
        return None

    def after_model_callback(callback_context, llm_response):
        # Also runs on stub replies from before/on_error; those have no open call, so nothing is logged twice.
        started = rc.end_call(callback_context.agent_name)
        if started is None or llm_response.partial:
            return None
        model, t0 = started
        usage = llm_response.usage_metadata
        rc.record(LlmCallLog(
            agent=callback_context.agent_name, model=model, seconds=round(time.monotonic() - t0, 2),
            prompt_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None), ok=True,
        ))
        return None

    def on_model_error_callback(callback_context, llm_request, error):
        agent = callback_context.agent_name
        started = rc.end_call(agent)
        model, t0 = started if started else (rc.agent_models.get(agent) or llm_request.model or "?", time.monotonic())
        rc.record(LlmCallLog(agent=agent, model=model, seconds=round(time.monotonic() - t0, 2), ok=False))
        rc.warn(f"{agent}: {friendly_error(error, model)}")
        log.warning("%s model error: %s", agent, str(error)[:300])
        return stub_response({"error": "model_error", "detail": type(error).__name__})

    return {
        "before_model_callback": before_model_callback,
        "after_model_callback": after_model_callback,
        "on_model_error_callback": on_model_error_callback,
    }
