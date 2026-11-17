"""Pydantic v2 data contracts (spec §6). Semantic checks (refs exist, scope/screens rules) live in postprocess."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["DATA_SOURCE", "MEDIA", "ACTION", "NAVIGATION", "VALIDATION",
                   "STATE", "VISIBILITY", "BUSINESS_RULE", "PERMISSION", "CONTENT"]
Owner = Literal["product", "tech", "design"]
Priority = Literal["P1", "P2"]  # P1 = blocks writing the code
AgentTag = Literal["DS", "IA", "VL", "ST", "BR", "CUR", "AUD"]
ElementType = Literal["text", "heading", "button", "link", "input", "select", "checkbox", "radio", "toggle",
                      "image", "icon", "list", "card", "table", "tab", "nav", "modal", "banner", "stepper",
                      "chart", "other"]


class Element(BaseModel):
    id: str  # globally unique: "S2.E5"
    type: ElementType
    label: str  # visible text verbatim, or short descriptor
    section: str = ""
    data_bearing: bool = False
    interactive: bool = False
    states_seen: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    notes: str = ""


class Screen(BaseModel):
    id: str  # "S1".. echoes the id given in the input (file order)
    file: str
    name: str  # visible title, else filename-derived
    purpose: str
    complexity: Literal["low", "medium", "high"]
    elements: list[Element]


class Transition(BaseModel):
    from_screen: str
    to_screen: str
    trigger: str  # element id or label, e.g. "S1.E9" / "Continue"
    note: str = ""


class RecurringComponent(BaseModel):
    id: str  # "RC1"
    name: str  # e.g. "Shipping address block"
    appears_on: list[str]  # ["S1","S2","S3"] (>= 2, checked by validate_inventory)
    element_refs: list[str]  # element ids across screens
    notes: str = ""


class FlowInventory(BaseModel):
    flow_name: str
    flow_summary: str
    screens: list[Screen]
    transitions: list[Transition] = Field(default_factory=list)
    recurring_components: list[RecurringComponent] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    category: Category
    scope: Literal["FLOW", "SCREEN"]
    screens: list[str] = Field(min_length=1)  # SCREEN -> exactly 1 id; FLOW -> >= 2 ids it applies to
    element_refs: list[str] = Field(min_length=1)  # "S2.E5" | "RC1" | "S2" (whole screen) | "FLOW"
    question: str = Field(max_length=300)
    answer_hint: str = Field(max_length=200)
    owner: Owner
    priority: Priority
    source_agents: list[AgentTag] = Field(default_factory=list)


class Question(Candidate):
    n: int  # display number, 1..N


class AuditPatch(BaseModel):
    add: list[Candidate] = Field(default_factory=list, max_length=3)
    remove: list[dict] = Field(default_factory=list)  # {"n": 4, "reason": "answerable_from_image|not_on_screen|duplicate"}
    reword: list[dict] = Field(default_factory=list)  # {"n": 2, "question": "…"}
    rescope: list[dict] = Field(default_factory=list)  # {"n": 5, "screens": ["S1","S3"]} -> promote to FLOW
    verdict: Literal["ok", "patched"] = "ok"


class LlmCallLog(BaseModel):
    agent: str
    model: str
    seconds: float
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    ok: bool = True


class RunReport(BaseModel):
    jira_key: str
    flow_sha: str
    screens: int
    llm_calls: int
    calls: list[LlmCallLog]
    question_count: int
    common_count: int
    posted: bool
    skipped_reason: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
