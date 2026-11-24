"""Per-run, non-serializable context: settings, images, budget, call log. Never stored in ADK session state."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
    out_dir: Path | None = None  # outputs/<KEY>_<ts>/; created on first write
    jira: Any = None  # JiraClient (or a test double); None in dry-run without credentials
    budget: LLMBudget = field(init=False)
    image_parts: list[Any] = field(init=False)
    agent_models: dict[str, str] = field(default_factory=dict)  # agent name -> model name (filled in agents.py)
    calls: list[LlmCallLog] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _open_calls: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _t0: float = field(default_factory=time.monotonic, repr=False)

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

    def say(self, msg: str) -> None:
        """Live progress line on stdout, prefixed with seconds since the run started. Display only, not stored."""
        with self._lock:
            print(f"[{time.monotonic() - self._t0:5.1f}s] {msg}", flush=True)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    # --- convenience ---
    @property
    def n_screens(self) -> int:
        return len(self.flow.screens)

    @property
    def marker(self) -> str:
        return self.flow.marker

    def output_dir(self) -> Path:
        if self.out_dir is None:
            self.out_dir = Path("outputs") / f"{self.jira_key}_{time.strftime('%Y%m%d-%H%M%S')}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir
