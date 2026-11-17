"""Model factory: Gemini for vision slots, Groq (via LiteLLM) for text slots. Names come only from Settings/.env."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Any, Literal

from .config import ConfigError, Settings

log = logging.getLogger(__name__)

Slot = Literal["analyst", "auditor", "specialists", "curator"]
SLOT_KIND: dict[str, Literal["vision", "text"]] = {
    "analyst": "vision", "auditor": "vision", "specialists": "text", "curator": "text",
}


def is_gemini(name: str) -> bool:
    return name.strip().lower().startswith("gemini")


def is_groq(name: str) -> bool:
    return name.strip().lower().startswith("groq/")


def model_name(settings: Settings, slot: Slot) -> str:
    """Per-agent override (MODEL_<SLOT>) wins over the slot default (VISION_MODEL / TEXT_MODEL)."""
    override = getattr(settings, f"model_{slot}").strip()
    default = settings.vision_model if SLOT_KIND[slot] == "vision" else settings.text_model
    return override or default.strip()


def validate_slots(settings: Settings) -> dict[str, str]:
    """Return {slot: model}. Raise ConfigError if a vision slot isn't Gemini or a text slot isn't groq/…."""
    resolved, errors = {}, []
    for slot, kind in SLOT_KIND.items():
        name = model_name(settings, slot)
        env_key = f"MODEL_{slot.upper()}" if getattr(settings, f"model_{slot}").strip() else (
            "VISION_MODEL" if kind == "vision" else "TEXT_MODEL")
        if not name:
            errors.append(f"{slot}: no model set ({env_key} is empty)")
        elif kind == "vision" and not is_gemini(name):
            errors.append(f"{slot}: {env_key}={name!r} must be a Gemini model (gemini-...); image calls go to Gemini only")
        elif kind == "text" and not is_groq(name):
            errors.append(f"{slot}: {env_key}={name!r} must be a Groq model (groq/...); text calls go to Groq only")
        resolved[slot] = name
    if errors:
        raise ConfigError("Model slot misconfiguration:\n  - " + "\n  - ".join(errors))
    return resolved


GROQ_TIMEOUT_S = 60
MAX_RATE_LIMIT_WAIT_S = 90  # total time one logical call may wait on 429s before failing
MAX_TRANSIENT_RETRIES = 2  # 5xx / connection errors
_RETRY_IN = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)(ms|s)", re.IGNORECASE)


def retry_after_seconds(error: Exception, default: float = 5.0) -> float:
    """Wait time from Groq's 'Please try again in 19.73s' / '735ms' (or a Retry-After header)."""
    m = _RETRY_IN.search(str(error))
    if m:
        minutes, value, unit = int(m.group(1) or 0), float(m.group(2)), m.group(3).lower()
        return minutes * 60 + (value / 1000 if unit == "ms" else value) + 0.5
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after")) + 0.5
    except (TypeError, ValueError):
        return default


@functools.cache
def _rate_limited_litellm_class() -> type:
    """LiteLlm subclass that waits out 429s (Groq free tier: 8k tokens/min) inside ONE logical call.

    Retries happen below the model callbacks, so the LLM budget counts the call once (spec T6.3 intent).
    """
    import litellm
    from google.adk.models.lite_llm import LiteLlm  # heavy import (litellm); only when a Groq model is built

    litellm.suppress_debug_info = True  # no "Give Feedback / Get Help" banners on handled 429s
    transient = (litellm.InternalServerError, litellm.ServiceUnavailableError, litellm.APIConnectionError,
                 litellm.Timeout)

    class RateLimitedLiteLlm(LiteLlm):
        async def generate_content_async(self, llm_request, stream: bool = False):
            waited, transient_left = 0.0, MAX_TRANSIENT_RETRIES
            while True:
                try:
                    async for response in super().generate_content_async(llm_request, stream):
                        yield response
                    return
                except litellm.RateLimitError as e:
                    delay = retry_after_seconds(e)
                    if waited + delay > MAX_RATE_LIMIT_WAIT_S:
                        raise
                    waited += delay
                except transient:
                    if transient_left == 0:
                        raise
                    transient_left -= 1
                    delay = 2.0
                log.info("%s: provider busy, retrying in %.1fs", self.model, delay)
                await asyncio.sleep(delay)

    return RateLimitedLiteLlm


def build_model(name: str, json_mode: bool = False) -> Any:
    """Gemini -> plain model string for Agent(model=…); groq/… -> rate-limit-aware LiteLlm instance.

    json_mode is off by default for Groq: with gpt-oss models Groq's json_object mode failed with
    `json_validate_failed` (empty generation). Prompts demand JSON and extract_json() parses it.
    """
    if is_gemini(name):
        return name.strip()
    if is_groq(name):
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        return _rate_limited_litellm_class()(model=name.strip(), num_retries=0, timeout=GROQ_TIMEOUT_S, **extra)
    raise ConfigError(f"Unsupported model {name!r}: expected gemini-... or groq/...")
