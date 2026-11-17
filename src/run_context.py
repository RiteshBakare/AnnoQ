"""Per-run, non-serializable context: settings, images, budget, call log. Never stored in ADK session state."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .budget import LLMBudget
from .config import Settings
from .image_utils import Flow, image_parts
from .schemas import LlmCallLog


@dataclass
class RunContext:
    settings: Settings
    flow: Flow
    jira_key: str
    ticket_context: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    budget: LLMBudget = field(init=False)
    image_parts: list[Any] = field(init=False)
    agent_models: dict[str, str] = field(default_factory=dict)  # agent name -> model name (filled in agents.py)
    calls: list[LlmCallLog] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _open_calls: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.budget = LLMBudget(self.settings.max_llm_calls)
        self.image_parts = image_parts(self.flow)

    # --- call log (used by budget callbacks) ---
    def start_call(self, agent: str, model: str) -> None:
        with self._lock:
            self._open_calls[agent] = (model, time.monotonic())

    def end_call(self, agent: str) -> tuple[str, float] | None:
        with self._lock:
            return self._open_calls.pop(agent, None)

    def record(self, entry: LlmCallLog) -> None:
        with self._lock:
            self.calls.append(entry)

    def warn(self, msg: str) -> None:
        with self._lock:
            if msg not in self.warnings:
                self.warnings.append(msg)

    # --- convenience ---
    @property
    def n_screens(self) -> int:
        return len(self.flow.screens)

    @property
    def marker(self) -> str:
        return self.flow.marker
