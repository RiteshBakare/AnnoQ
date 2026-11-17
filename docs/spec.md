# Specification — Flow-to-Questions Pipeline (Phase 1, v2)

Companion to `architecture.md`. This is the implementation contract.

---

## 1. Overview

**Input:** a **folder** containing the screen images of one flow (e.g. `flows/shopping/01_cart.png, 02_shipping.png, 03_place_order.png, 04_success.png`) + the existing Jira issue key for that flow.
**Output:** exactly one Jira comment containing deduplicated clarification questions: a *Common to multiple screens* section (asked once) and one section per screen.
**Constraints:** ≤ 9 LLM calls (8 planned) · Gemini for image work, Groq for text work · models from `.env` · Python + Google ADK 2.0 graph `Workflow` + Jinja2 · all code in `src/`.

---

## 2. Functional requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `python run.py --file-path <folder> --jira-id <KEY>`; both optional → fall back to `FILE_PATH` / `JIRA_ISSUE_KEY` in `.env`. CLI overrides env. |
| FR-2 | `--file-path` may be a folder (flow) or a single image (treated as a 1-screen flow). |
| FR-3 | Folder scan: PNG/JPG/JPEG/WEBP, non-recursive, hidden files ignored, **natural filename sort = flow order** (`01_`, `02_`, `10_` sorts after `2_`). Screen ids `S1…Sn` assigned in that order. `n ≤ MAX_SCREENS` (default 8) else exit 1 with a "split this flow" message. Empty folder ⇒ exit 1. |
| FR-4 | Each image validated (size ≤ `MAX_IMAGE_MB`) and downscaled so long edge ≤ `MAX_IMAGE_EDGE`. |
| FR-5 | Fetch Jira summary + description (no LLM) → `ticket_context` (≤ 1500 chars). Failure ⇒ warning only. |
| FR-6 | **One** Gemini call sees **all** images and returns a validated `FlowInventory` (screens, elements, transitions, recurring components). |
| FR-7 | `specialist_panel`: five Groq agents (DS, IA, VL, ST, BR) run in parallel on the inventory and return candidate questions. |
| FR-8 | **Common questions are asked once:** questions about recurring components/data shown on multiple screens are emitted as `scope=FLOW` with the list of screens they apply to; no duplicates across screens (curator + code dedupe + auditor). |
| FR-9 | `flow_curator` (Groq) merges, dedupes, prioritizes, assigns owner, and fits per-scope targets. |
| FR-10 | Code computes coverage gaps; `auditor` (Gemini, all images) returns a patch (add ≤ 3, remove, reword, rescope). |
| FR-11 | Code applies patch, enforces `MAX_Q_PER_SCREEN`, `MAX_FLOW_Q`, `MAX_TOTAL_Q`, numbers questions, groups them, renders the comment. |
| FR-12 | Post **one** comment (REST v2) on the existing issue; never create tickets. |
| FR-13 | Skip posting if the idempotency marker is already on the issue unless `--force`. |
| FR-14 | `--dry-run` runs everything except posting. |
| FR-15 | Never exceed `MAX_LLM_CALLS` (default 9, ceiling 9); planned calls = 8 regardless of screen count. |
| FR-16 | Orchestration is an ADK 2.0 graph `Workflow` (edges in `src/workflow.py`); specialists are a nested `Workflow` named `specialist_panel`. |
| FR-17 | Models per agent come from `.env`; startup fails if a vision slot isn't Gemini or a text slot isn't Groq. |
| FR-18 | Labels are plain text: `@product team`, `@tech team`, `@design team`. No real mentions. |
| FR-19 | Never invent endpoints/fields (no API catalog); data questions ask the PMO to supply them. |
| FR-20 | Write artifacts to `outputs/<KEY>_<timestamp>/`. |

## 3. Non-functional requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | ≤ 90 s end-to-end for ≤ 8 screens (typical 30–60 s for 4). |
| NFR-2 | Simple code: ≤ ~1000 LOC in `src/` (excluding templates/tests). |
| NFR-3 | Deterministic nodes unit-tested offline; graph tested with stub agents. |
| NFR-4 | No secrets in logs/outputs. |
| NFR-5 | Clear exit codes (§13) and readable errors. |

---

## 4. CLI

```
python run.py [--file-path FOLDER_OR_IMAGE] [--jira-id KEY] [--dry-run] [--force] [-v]
```

| Flag | Meaning | Env |
|------|---------|-----|
| `--file-path` | Folder of screens (one flow) or one image | `FILE_PATH` |
| `--jira-id` | Existing Jira issue key | `JIRA_ISSUE_KEY` |
| `--dry-run` | No posting; print comment, save artifacts | `DRY_RUN=true` |
| `--force` | Post even if marker exists | – |
| `-v` | Verbose logs | `LOG_LEVEL` |

`run.py` = `from src.cli import main; main()`.

---

## 5. Configuration (`.env.example`)

```ini
# ---- Models (change here only) ----
VISION_MODEL=gemini-3-flash-preview       # example (verified 2026-11-17); analyst + auditor (must be Gemini)
TEXT_MODEL=groq/openai/gpt-oss-120b       # example (verified 2026-11-17); 5 specialists + curator (must be groq/…)
MODEL_ANALYST=
MODEL_AUDITOR=
MODEL_SPECIALISTS=
MODEL_CURATOR=

# ---- API keys ----
GOOGLE_API_KEY=your-gemini-key
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GROQ_API_KEY=your-groq-key

# ---- Jira (free Jira Cloud) ----
JIRA_BASE_URL=https://yourorg.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your-atlassian-api-token
JIRA_ISSUE_KEY=PROJ-123

# ---- Input ----
FILE_PATH=./flows/shopping
MAX_SCREENS=8
MAX_IMAGE_EDGE=2000
MAX_IMAGE_MB=10

# ---- Budget & limits ----
MAX_LLM_CALLS=9              # ceiling 9 (8 planned + 1 retry)
PANEL_CONCURRENCY=5          # lower if Groq returns 429
MAX_Q_PER_SCREEN=8
MAX_FLOW_Q=8
MAX_TOTAL_Q=30
JIRA_COMMENT_MAX_CHARS=30000

# ---- Behaviour ----
TEMPERATURE_VISION=0.2
TEMPERATURE_TEXT=0.3
OWNER_LABELS=product:@product team,tech:@tech team,design:@design team
DRY_RUN=false
LOG_LEVEL=INFO
```

`requirements.txt` must pin `google-adk>=2.0` (graph `Workflow`), plus `litellm`, `jinja2`, `pydantic>=2`, `python-dotenv`, `requests`, `pillow`, `pytest`.

---

## 6. Data schemas (`src/schemas.py`)

```python
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Category = Literal["DATA_SOURCE","MEDIA","ACTION","NAVIGATION","VALIDATION",
                   "STATE","VISIBILITY","BUSINESS_RULE","PERMISSION","CONTENT"]
Owner    = Literal["product","tech","design"]
Priority = Literal["P1","P2"]                       # P1 = blocks writing the code
AgentTag = Literal["DS","IA","VL","ST","BR","CUR","AUD"]

class Element(BaseModel):
    id: str                       # globally unique: "S2.E5"
    type: Literal["text","heading","button","link","input","select","checkbox","radio","toggle",
                  "image","icon","list","card","table","tab","nav","modal","banner","stepper","chart","other"]
    label: str                    # visible text verbatim, or short descriptor
    section: str = ""
    data_bearing: bool = False
    interactive: bool = False
    states_seen: List[str] = []
    needs_clarification: bool = False
    notes: str = ""

class Screen(BaseModel):
    id: str                       # "S1".. echoes the id given in the input (file order)
    file: str
    name: str                     # visible title, else filename-derived
    purpose: str
    complexity: Literal["low","medium","high"]
    elements: List[Element]

class Transition(BaseModel):
    from_screen: str; to_screen: str
    trigger: str                  # element id or label, e.g. "S1.E9" / "Continue"
    note: str = ""

class RecurringComponent(BaseModel):
    id: str                       # "RC1"
    name: str                     # e.g. "Shipping address block"
    appears_on: List[str]         # ["S1","S2","S3"]  (≥ 2)
    element_refs: List[str]       # element ids across screens
    notes: str = ""

class FlowInventory(BaseModel):
    flow_name: str
    flow_summary: str
    screens: List[Screen]
    transitions: List[Transition] = []
    recurring_components: List[RecurringComponent] = []
    ambiguities: List[str] = []

class Candidate(BaseModel):
    category: Category
    scope: Literal["FLOW","SCREEN"]
    screens: List[str] = Field(min_length=1)        # SCREEN → exactly 1 id; FLOW → ≥ 2 ids it applies to
    element_refs: List[str] = Field(min_length=1)   # "S2.E5" | "RC1" | "S2" (whole screen) | "FLOW"
    question: str = Field(max_length=300)
    answer_hint: str = Field(max_length=200)
    owner: Owner
    priority: Priority
    source_agents: List[AgentTag] = []

class Question(Candidate):
    n: int                                          # display number, 1..N

class AuditPatch(BaseModel):
    add: List[Candidate] = Field(default_factory=list, max_length=3)
    remove: List[dict] = []    # {"n": 4, "reason": "answerable_from_image|not_on_screen|duplicate"}
    reword: List[dict] = []    # {"n": 2, "question": "…"}
    rescope: List[dict] = []   # {"n": 5, "screens": ["S1","S3"]}  → promote to FLOW
    verdict: Literal["ok","patched"] = "ok"

class LlmCallLog(BaseModel):
    agent: str; model: str; seconds: float
    prompt_tokens: Optional[int] = None; output_tokens: Optional[int] = None; ok: bool = True

class RunReport(BaseModel):
    jira_key: str; flow_sha: str; screens: int; llm_calls: int
    calls: List[LlmCallLog]; question_count: int; common_count: int
    posted: bool; skipped_reason: Optional[str] = None; warnings: List[str] = []
```

**Inventory validation (`validate_inventory`):** `len(screens) == n_images`; ids `S1…Sn` in order; element ids unique and prefixed with their screen id; `recurring_components[*].appears_on` ⊆ screens and ≥ 2; transitions reference existing screens. Missing screen names ⇒ filled from filename stem.

**Candidate validation (`postprocess.validate_candidate`):** refs exist (element id / RC id / screen id / `FLOW`); `scope` consistent with `screens` (SCREEN→1 id; FLOW→≥2 ids; if flow has 1 screen → force SCREEN); question 8–300 chars (auto-append `?`); enums valid. Invalid candidates are dropped with a warning, never crash.

---

## 7. Agents

Common: `Agent(mode="single_turn")` nodes; system instruction rendered **once** from `templates/agents/*.j2`; user message = previous node's output (rendered `templates/inputs/*.j2`); JSON-only output; `before_model_callback` = budget guard (+ image injection for Gemini agents); `after_model_callback` = usage logging; `on_model_error_callback` = stub JSON so joins complete.

| Node | Tag | Model slot | Input message | Output |
|------|-----|------------|---------------|--------|
| `screen_analyst` | – | VISION | analyst_input (+ **all images** via callback) | `FlowInventory` |
| `data_source_agent` | DS | TEXT | panel_input | `{"candidates":[…]}` |
| `interaction_agent` | IA | TEXT | panel_input | same |
| `validation_agent` | VL | TEXT | panel_input | same |
| `state_condition_agent` | ST | TEXT | panel_input | same |
| `business_rule_agent` | BR | TEXT | panel_input | same |
| `flow_curator` | CUR | TEXT | curator_input | `{"questions":[…],"dropped":[…]}` |
| `auditor` | AUD | VISION | audit_input (+ **all images**) | `AuditPatch` |

Validators accept either a parsed `dict` or a `str` (run through `extract_json`: strips code fences/prose, `json.loads`, pydantic).

---

## 8. Prompt drafts

> Drafts — tune in the golden-set task. Every agent ends with "Return ONLY valid JSON, no markdown."

### 8.1 `agents/analyst.j2`
```jinja
You are a senior UI analyst preparing a multi-screen FLOW for front-end implementation.
All screen images are attached in order S1, S2, …. Produce ONE factual inventory of the whole flow.
Do NOT ask questions or guess behaviour.

Rules
- Use the screen ids given in the user message (S1..Sn) exactly, in the order given. Return every screen.
- Elements: ids "S<k>.E<m>" in reading order per screen (tabs, badges, icons with meaning, images, helper/error text included).
- "label": copy visible text EXACTLY; never invent text. For icons/images use a short descriptor.
- data_bearing=true if the value likely comes from a server/DB. interactive=true if the user can click/type/select/toggle.
- needs_clarification=true only if data source or behaviour cannot be inferred from the design.
- states_seen: default/error/empty/disabled/loading/selected/expanded visible in the design.
- transitions: likely screen-to-screen navigation you can see (CTA labels, stepper, order of screens).
- recurring_components: UI blocks or data that appear on 2+ screens (address block, order summary, header/stepper,
  cart total, user info). Give id RC1.., name, appears_on, element_refs. Only list true repeats.
- screen.complexity: low (≤8 elements, no forms) | medium | high (forms, lists/tables, many actions).
- ambiguities: dynamic/behavioural things that are unclear.

Return ONLY valid JSON matching:
{"flow_name":"","flow_summary":"","screens":[{"id":"S1","file":"","name":"","purpose":"","complexity":"medium",
 "elements":[{"id":"S1.E1","type":"button","label":"","section":"","data_bearing":false,"interactive":true,
 "states_seen":["default"],"needs_clarification":true,"notes":""}]}],
 "transitions":[{"from_screen":"S1","to_screen":"S2","trigger":"S1.E9","note":""}],
 "recurring_components":[{"id":"RC1","name":"","appears_on":["S1","S2"],"element_refs":["S1.E4","S2.E3"],"notes":""}],
 "ambiguities":[""]}
```

### 8.2 `inputs/analyst_input.j2`
```jinja
Flow folder name: {{ flow_hint }}
{% if ticket_context %}Jira ticket context (may be incomplete): {{ ticket_context }}{% endif %}
Screens, in flow order:
{% for s in screens %}- {{ s.id }}: file "{{ s.file }}"{% if s.hint %} (name hint: {{ s.hint }}){% endif %}
{% endfor %}
{% if repair %}Your previous answer was invalid ({{ repair }}). Fix it and return valid JSON only.{% endif %}
```

### 8.3 `agents/specialist_base.j2`
```jinja
You are a senior business analyst on a team that turns Figma flows into React code.
Normally an annotator asks Product, Tech and UI/UX owners what each part of each screen does.
You write those questions so nobody needs a call. You are ONE specialist on a panel of five; stay in your lens.

PROJECT FACTS
- There is NO API catalog / OpenAPI / Swagger. Never assume endpoints or field names — ask for them.
- Questions are answered by a PMO member in a Jira comment.

YOUR LENS
{% block lens %}{% endblock %}

RULES
1. Anchor every question: element_refs = element ids ("S2.E5"), a recurring component ("RC1"), a screen id (whole screen) or "FLOW".
2. Never ask what the design already shows or what is answerable from the image.
3. One decision per question; answerable in 1–3 sentences; ≤ 40 words; use real on-screen labels.
4. Bundle siblings with the same behaviour into ONE question ("For the Name, Phone and Pincode fields …").
5. answer_hint = what a complete answer must contain (e.g. "endpoint path, HTTP method, response field").
6. No generic boilerplate ("Should it be accessible?"). Skip decorative/static elements.
7. ASK ONCE ACROSS SCREENS: if a question concerns a recurring component, or data/behaviour shown on several screens,
   emit ONE question with scope="FLOW", screens=[all screens it applies to], element_refs=["RC#"] (or the relevant ids).
   Never emit the same question separately per screen. Use scope="SCREEN" only for things unique to one screen.
8. Return at most "limits.max_candidates" candidates (given in the user message). Return fewer, or [], if nothing in your lens is unclear.
9. owner: tech | product | design. priority: P1 if code cannot be written without the answer, else P2.

Return ONLY valid JSON: {"candidates":[{"category":"…","scope":"SCREEN","screens":["S1"],"element_refs":["S1.E3"],
 "question":"…?","answer_hint":"…","owner":"tech","priority":"P1"}]}
```
*(Implementation note: `max_n` is per-run, so the limit travels in the user message `limits`; the instruction just says "obey `limits.max_candidates`".)*

### 8.4 Lenses
**`ds.j2`** — *DATA SOURCE (DS).*
```jinja
{% extends "specialist_base.j2" %}
{% block lens %}DATA SOURCE. For each data_bearing element/component needing clarification: source (endpoint path + HTTP method +
response field, or other service), media/image service and URL pattern, formats (date, currency, units), list behaviour
(pagination, sort, filter, search), data shared across screens (cart, user, address) and how it stays in sync, refresh/caching.
Categories: DATA_SOURCE, MEDIA, CONTENT. Owner usually tech.
Do NOT cover click outcomes (interaction), input validation, UI states, or business formulas — other specialists own those.{% endblock %}
```
**`ia.j2`** — *INTERACTION (IA).*
```jinja
{% extends "specialist_base.j2" %}
{% block lens %}INTERACTION. For each interactive element: what happens on click/submit (API call, navigation target, modal, toast,
redirect), screen-to-screen transitions (use "transitions"), back/cancel/edit behaviour, side effects (e.g. order created, cart cleared).
Categories: ACTION, NAVIGATION. Owner tech or product.
Do NOT cover field validation, enabled/visible conditions, data sources, or business formulas.{% endblock %}
```
**`vl.j2`** — *VALIDATION (VL).*
```jinja
{% extends "specialist_base.j2" %}
{% block lens %}VALIDATION. For input elements: required/optional, formats, min/max length, masks (card number, postcode, phone),
when validation triggers (blur/submit), exact error messages, server-side error mapping, cross-field rules.
Category: VALIDATION. Owner usually product (rules) or tech (server errors).
Do NOT cover screen/component states, business thresholds, or data sources.{% endblock %}
```
**`st.j2`** — *STATE & CONDITION (ST).*
```jinja
{% extends "specialist_base.j2" %}
{% block lens %}STATE & CONDITION. Loading, empty, error and success states of data components; enabled/disabled rules for buttons;
conditional visibility; selected/expanded states; stepper/progress behaviour; persistence of state across steps (cart, form data
when going back); timeouts/session expiry. Categories: STATE, VISIBILITY. Owner product or tech (or design for empty/error visuals).
Do NOT cover input-validation text, business formulas, or data sources.{% endblock %}
```
**`br.j2`** — *BUSINESS RULE (BR).*
```jinja
{% extends "specialist_base.j2" %}
{% block lens %}BUSINESS RULES. Calculations (tax, shipping, discounts, totals), promo/coupon rules, limits/thresholds, eligibility,
role/permission differences, region/locale rules, feature flags, analytics events, compliance (payments/PII), edge cases that change
what is shown. Categories: BUSINESS_RULE, PERMISSION, CONTENT. Owner usually product.
Do NOT cover API shapes, UI states, or field validation.{% endblock %}
```

### 8.5 `inputs/panel_input.j2`
```jinja
FLOW INVENTORY (JSON):
{{ inventory_json }}

limits: {"max_candidates": {{ max_candidates }}, "screens": {{ n_screens }}}
{% if ticket_context %}TICKET CONTEXT: {{ ticket_context }}{% endif %}
{% if n_screens == 1 %}Single-screen flow: use scope="SCREEN" only.{% endif %}
```
`max_candidates = min(12, 2 + 2 × n_screens)`.

### 8.6 `agents/curator.j2`
```jinja
You are the lead annotator. Five specialists (DS, IA, VL, ST, BR) wrote candidate questions for a multi-screen flow.
Produce the FINAL list. Every question is asked ONCE.

Do, in order:
1. Merge duplicates/overlaps (even across screens and specialists); keep the best wording; union source_agents.
2. ASK ONCE: if the same intent applies to several screens or a recurring component (RC#), output ONE question with
   scope="FLOW" and screens=[all screens it applies to]. If behaviour might differ per screen, ask once and add
   "…and does it differ on <screen name>?". Keep scope="SCREEN" only for things unique to one screen.
3. Remove anything answerable from the design, generic, or about static/decorative elements.
4. Ensure every element/component with needs_clarification=true is covered by some question (bundle siblings).
5. Priority: P1 = blocks coding (data source, action outcome, validation rules, state conditions); P2 = refinement.
6. Rewrite for clarity (≤ 40 words, real labels, single decision); keep a useful answer_hint.
7. Assign owner (tech | product | design). Do not invent new topics.
8. Respect targets in the user message: screen-specific per screen by complexity, flow-level total, and the hard total cap.

Return ONLY valid JSON: {"questions":[{category,scope,screens,element_refs,question,answer_hint,owner,priority,source_agents}],
 "dropped":[{"reason":"…"}]}
```
**`inputs/curator_input.j2`:** compact inventory (screens: id, name, complexity; recurring components; transitions; elements flagged `needs_clarification` with id/label), the merged candidate list (JSON, tagged by source), and
`targets: {per_screen: {S1:"4-6", S2:"2-3", …}, flow_level:"3-8", hard_caps:{per_screen:8, flow:8, total:30}}`.
Per-screen target by complexity: low 2–3, medium 4–6, high 6–8. Flow-level target = 2–8 (0 if one screen).

### 8.7 `agents/auditor.j2`
```jinja
You are a skeptical reviewer. All screen images are attached (S1..Sn in order). Below are the curated questions.

Check
(a) Visible elements/components with data or behaviour that NO question covers → add (max 3, same schema).
(b) Questions about something not visible, or answerable from the design → remove (reason: not_on_screen | answerable_from_image | duplicate).
(c) Questions that are really the same ask repeated for several screens → remove the extras (duplicate) and rescope one to the full screen list.
(d) Vague or leading wording → reword.
Be conservative: if the list is good, return verdict "ok" with empty arrays.

Return ONLY valid JSON: {"add":[{…Candidate}],"remove":[{"n":1,"reason":"…"}],"reword":[{"n":2,"question":"…"}],
 "rescope":[{"n":5,"screens":["S1","S3"]}],"verdict":"ok|patched"}
```
**`inputs/auditor_input.j2`:** curated questions with `n`, compact inventory, `uncovered` list.

---

## 9. Graph (`src/workflow.py`)

```python
from google.adk import Workflow
from google.adk.workflow import JoinNode, START

def build_workflow(rc: RunContext) -> Workflow:
    a = build_agents(rc)     # screen_analyst, ds, ia, vl, st, br, flow_curator, auditor (Agent nodes)
    n = build_nodes(rc)      # function nodes/routers closed over RunContext

    join_panel = JoinNode(name="join_panel")
    specialist_panel = Workflow(
        name="specialist_panel",
        edges=[(START, (a.ds, a.ia, a.vl, a.st, a.br), join_panel)],
        max_concurrency=rc.settings.panel_concurrency,
    )

    return Workflow(
        name="flow_questions",
        edges=[
            (START, n.build_analyst_input, a.screen_analyst, n.validate_inventory),
            (n.validate_inventory, {"OK": n.build_panel_input,
                                    "RETRY": n.build_analyst_input,
                                    "ABORT": n.fail}),

            (n.build_panel_input, specialist_panel, n.merge_candidates),
            (n.merge_candidates, {"OK": n.build_curator_input, "ABORT": n.fail}),

            (n.build_curator_input, a.flow_curator, n.validate_curated),
            (n.validate_curated, {"OK": n.coverage_check,
                                  "RETRY": n.build_curator_input,
                                  "FALLBACK": n.fallback_curate}),
            (n.fallback_curate, n.coverage_check),

            (n.coverage_check, n.build_audit_input, a.auditor, n.apply_patch,
             n.finalize, n.publish),
        ],
    )
```

**Function-node conventions (ADK 2.0):** first parameter `node_input` = previous node's output; other parameters named like state keys are bound from session state; nodes `return`/`yield` `Event(output=…, state={…}, route="OK")`. Routers return `Event(output=…, route=…)`.

**State keys (JSON-serializable only):** `inventory`, `candidates`, `curated`, `coverage`, `patch`, `questions`, `comment`, `warnings`, `retries_used`, `result`. Not in state: images, `LLMBudget`, `Settings` (held in `RunContext`).

**Run:**
```python
runner = Runner(agent=workflow, app_name="flow_questions", session_service=InMemorySessionService())
# create session → run_async(new_message=Content(parts=[Part(text="start")])) → read final state["result"]
```
`state["result"]` = `{ok, posted, skipped_reason, comment_path, report_path}`; `fail` sets `ok=False`.

### 9.1 ADK API assumptions to verify in the M0 spike

| # | Assumption | Fallback if false |
|---|------------|-------------------|
| A1 | `from google.adk import Agent, Event, Workflow`; `from google.adk.workflow import JoinNode, START` work with `google-adk>=2.0`. | Pin the exact version that does. |
| A2 | A nested `Workflow` can be used as a node and receives the parent node's output at its `START`. | Inline the 5 agents + join in the parent graph. |
| A3 | `JoinNode` outputs `{node_name: output}` for all inbound branches. | Read the dict keys defensively in `merge_candidates`. |
| A4 | A node with two inbound edges (initial + RETRY loop) re-runs when either fires; cycles are allowed. | Replace retries with one **dynamic node** (`@node` + `ctx.run_node`, Python `while`) wrapping analyst/curator + validation. |
| A5 | `Agent(model=LiteLlm("groq/…"), mode="single_turn")` works as a node, with `before_model_callback`. | Use `LlmAgent` nodes or a thin function node calling the agent via `ctx.run_node`. |
| A6 | `on_model_error_callback` (or equivalent) lets a failed branch still emit output so `JoinNode` completes. | Wrap each panel agent in a function node with try/except that emits a stub. |
| A7 | `Workflow(max_concurrency=…)` throttles parallel agents. | Use an `asyncio.Semaphore` in the budget callback. |
| A8 | Agent output arrives to the next node as `str` or parsed `dict`. | Validators already handle both. |

#### 9.2 M0 spike results (google-adk 2.14.1, Python 3.13, 2026-11-17)

All of A1–A8 hold; **no fallback needed**. Spikes live in `tests/spikes/` (offline, run by `pytest`) and
`tests/spikes/live_models.py` (live, `python -m tests.spikes.live_models`, needs keys).

| # | Result | Evidence / notes |
|---|--------|------------------|
| A1 | ✅ | Imports work. `Runner(node=workflow, app_name=…, session_service=…)` (use `node=`, not `agent=`). |
| A2 | ✅ | Nested `Workflow` as a node; every panel agent got the parent node's output as its user message. |
| A3 | ✅ | `JoinNode` output = `{agent_name: output}` for all 5 branches. |
| A4 | ✅ | Router `Event(route=…)` + dict edges + loop back to `build_analyst_input` (2 inbound edges) re-runs; counter in state works; RETRY→OK and RETRY→ABORT both pass. The retried agent call contains **only** the new message (failed reply is not in history). |
| A5 | ✅ | `Agent(model=LiteLlm("groq/…"), mode="single_turn")` works as a node; before/after/error callbacks fire; usage tokens available in `after_model_callback` (`llm_response.usage_metadata`). JSON output valid with and without `response_format={"type":"json_object"}`. |
| A6 | ✅ | `on_model_error_callback` returning a stub `LlmResponse` lets the branch emit output; `JoinNode` completes with 2 failed branches. Note: `after_model_callback` also runs on the stub (`usage_metadata=None`) — usage logging must tolerate `None`. |
| A7 | ✅ | `max_concurrency=2` → peak 2 concurrent model calls; `None` → all parallel. |
| A8 | ✅ | Agent output (no `output_schema`) arrives as `str`; function-node dict output arrives as `dict`. Workflow `START` input arrives as `types.Content`. |
| T0.6 | ✅ | Images appended in `before_model_callback` reach Gemini (4 screens identified in order); Groq requests carry 0 image parts. Lock-guarded budget refuses calls past the cap under parallel runs; refused calls never reach the model. |

Findings for later tasks:
- `gemini-2.5-flash` returns 404 for new API users; `gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-flash-latest` returned 503 (high demand) on the spike day. `gemini-3-flash-preview` worked. `groq/llama-3.3-70b-versatile` is no longer listed on Groq; `groq/openai/gpt-oss-120b` works.
- On 503 the google-genai client retried internally for ~180 s before failing → breaks NFR-1. T6.3 must set an explicit HTTP timeout/retry policy for Gemini.

#### 9.3 M1 implementation notes (2026-11-17)

- **Dev models** (cheap, in `.env`): `VISION_MODEL=gemini-3.1-flash-lite`, `TEXT_MODEL=groq/openai/gpt-oss-20b`. Use the larger models (`gemini-3-flash-preview`, `groq/openai/gpt-oss-120b`) for the final demo. Gemini often wraps JSON in code fences, so `extract_json` strips them.
- **Image parts:** `image_parts()` puts a short text label `Screen S<k> (file "<name>"):` before each image. It is still one Gemini call; the label maps ids to images without doubt.
- **Marker hash** uses the original file bytes, so changing `MAX_IMAGE_EDGE` does not change the marker.
- **Jira Cloud returns 404, not 401, for a wrong token.** The 404 message also tells the user to check credentials.
- **`after_model_callback` runs on stub replies** too. Calls are tracked per agent (`RunContext.start_call/end_call`), so a refused or failed call is logged once only (`ok=False` for model errors; refused calls are not logged as calls).
- **Config precedence:** CLI > process env > `.env`. `MAX_LLM_CALLS` is clamped to 1..9. `PANEL_CONCURRENCY` is limited to 1..5. Jira credentials are optional in `--dry-run`.

#### 9.4 M2 implementation notes (2026-11-17)

The prompt files in `src/templates/` are now the source of truth for prompts; §8 keeps the original drafts.

**Changes from the §8 drafts (all inside the 8-call budget):**
- **analyst.j2:**
  - Lists the allowed element `type` values, so schema validation passes.
  - Groups repeated list rows into one `list` element.
  - Asks for a complete inventory: no section may be dropped.
  - Sets `needs_clarification=true` for data/interactive elements and CTAs. Flash-lite flagged nothing before this rule.
  - Asks for a pairwise screen comparison to find recurring components.
- **panel_input.j2:** lists the recurring components as explicit "ASK ONCE" targets. FLOW questions went from 3 to 6 on the shopping flow.
- **specialist_base.j2:** all 8 keys are required. BR left out `owner`/`priority` before this.
- **br.j2:** must not cover button enabled/disabled conditions (that is ST's lens).
- **curator.j2:**
  - Merge only questions that ask the same decision.
  - Bundle sibling fields into one question.
  - Keep the best questions up to the target; the list is never empty when candidates exist.
  - `targets.total` is the expected total, computed in code.
- **Instructions are passed as a callable** (instruction provider), so ADK does not try to inject `{state}` into the JSON braces in the prompts.

**Model plumbing (`agents.py`, `models.py`):**
- **Gemini:** `response_mime_type=application/json`.
- **Groq:** `json_object` mode is **off**. With gpt-oss it failed with `json_validate_failed` (empty generation), so JSON is enforced by the prompt and parsed by `extract_json`.
- **Groq text agents:** `thinking_level=LOW` (maps to `reasoning_effort=low`) and `max_output_tokens=4096`. Without these, gpt-oss used its whole 2048-token output on reasoning and returned no JSON.
- **Groq free tier is 8,000 tokens/min for every model on this account** (20b, 120b, qwen). One run's 6 Groq calls need about 14–18k tokens. `RateLimitedLiteLlm` waits as long as Groq's `try again in Xs` says (at most 90 s per call) and retries inside the same logical call, so the budget still counts it once. This part of T6.3 was pulled forward. Measured panel+curator time on the free tier: **63–88 s**, so a full run is ~2 min. **This breaks NFR-1 (≤ 90 s) unless the Groq account moves to a paid/dev tier.**

**Findings for M3/M4:**
- Specialists sometimes emit `scope=SCREEN` with several screens, and FLOW questions whose screens go beyond the RC's `appears_on` (e.g. address asked "on S1"). T3.1 must coerce scope from the screen count and clamp RC questions to `appears_on`.
- The curator once returned `{"questions":[]}` for 38 candidates. `validate_curated` must treat an empty list as invalid when candidates exist (route RETRY/FALLBACK).
- gpt-oss-20b curation is uneven: it sometimes keeps cross-lens duplicates (ACTION vs DATA_SOURCE on the same button) and sometimes over-merges. The code dedupe (T3.2) and auditor are the safety net; compare with the demo model in T6.2.
- Gemini 3.1 flash-lite returned 503 once in 6 runs (high demand). The T6.3 Gemini retry/timeout policy is still open.

#### 9.5 M3 implementation notes (2026-11-17)

`src/postprocess.py` is pure: no I/O and no LLM. `FlowIndex` gives fast lookups: refs → screens, RC membership, flagged elements.

**Inventory (`validate_inventory`).**
- *Fatal (→ RETRY with a short repair hint):* not JSON or an error stub; schema failure; screen ids not exactly `S1..Sn` in order; element id not `S<k>.E<m>` on its own screen; duplicate element id.
- *Repaired with a warning:*
  - an unknown element `type` becomes `other`; an unknown `complexity` becomes `medium`. This saves a retry call.
  - `file` is set from the real file name, and an empty name from the filename hint.
  - an RC with fewer than 2 known screens or a bad id is dropped; unknown RC element refs are removed.
  - transitions to unknown screens are dropped.

**Candidates (`validate_candidate`).**
- *Rejected (dropped with a warning):* unknown category; question shorter than 8 or longer than 300 characters; no known `element_refs`; owner not in the enum.
- *Coerced:*
  - **`screens` are derived from the refs**: an element counts as its screen, an RC as its `appears_on`. This clamps RC questions to where the RC really appears (M2 finding).
  - `scope` follows the screen count (1 → SCREEN, 2 or more → FLOW). A single-screen flow is always SCREEN.
  - A missing owner gets the category default. A missing or unexpected priority becomes `P2`.
  - `?` is appended; `answer_hint` is trimmed to 200 characters; comma-separated ref strings are split.
- `merge_candidates`: an agent is *usable* if it returned a parseable candidates list. An empty list is a valid answer: VL on a flow without inputs.
- `parse_curated`: an empty `questions` list is **invalid** when there were candidates (→ RETRY/FALLBACK; M2 finding). Questions with no sources get `["CUR"]`.

**Dedupe (`dedupe_across_screens`).**
- Union-find over pairs, so merges are transitive.
- Merge rules, as in §11.2: same category with the same single RC, or same category on different screens with Jaccard ≥ 0.6 (stopwords removed).
- The question text is kept from the first P1 (else the first) question in the group.

**Coverage.**
- An RC is covered if its id **or any of its member elements** is referenced.
- A flagged element is covered if it is referenced directly, its RC is covered, or its screen id is referenced by a non-CONTENT question.

**`fallback_curate`.** Merge exact duplicates (same category, same screens, same token set; union sources, higher priority) → cross-screen dedupe → P1 first → caps.

**`apply_patch`.**
- Each op is validated on its own: one bad `add` does not discard the whole patch.
- An op on an unknown or already removed `n` is ignored with a warning.
- Only the first 3 `add` items are used.
- A patch that is not JSON or is an error stub is skipped, and the curated list is kept.

**Caps, order and size.**
- Caps run in this order: `MAX_FLOW_Q` → `MAX_Q_PER_SCREEN` (per screen) → `MAX_TOTAL_Q`.
- Drop order (`drop_rank`): P2 before P1 → auditor-only before others → single-source before multi-source → later before earlier.
- `fit_to_size` takes a `render` callable; the comment template comes in T5.1.

**Replay of the saved M2 live outputs** (offline, no model calls; curator path): 27 / 17 / 9 final questions for shopping / login_otp / onboarding. Shopping has 6 FLOW questions, with the address clamped to S2–S4. On onboarding, the multi-screen SCREEN question was coerced to FLOW. Uncovered for the auditor: login `S1.E6, S2.E4`; onboarding's three "Next" buttons. The fallback alone would give 30 / 25 / 18.

#### 9.6 M4 implementation notes (2026-11-17)

**Graph.**
- `src/workflow.py` holds the edge list. It is identical to §9 / A§5; `test_graph_matches_architecture` checks this.
- `build_workflow(rc, agents=None)` takes the agents as an argument, so graph tests can pass in stub-model agents.
- `run_workflow(rc)` runs the graph through `Runner(node=…)` and returns the final state.
- `python -m src.workflow flows/<name>` prints the real graph as Mermaid. No LLM calls.

**Nodes (`build_nodes(rc)`, 13 closures).**
- Every state-bound parameter has a default.
- **State keys:** `inventory`, `candidates`, `curated`, `curated_by` (`curator` | `fallback`), `audit_questions` (numbered, for the auditor's `n`), `coverage`, `audit_raw`, `questions`, `comment`, `retries_used`, `analyst_repair`, `curator_repair`, `fail_reason`, `result`.
- Warnings go to `RunContext.warnings`, not to state.

**Retry pool.**
- Size = `budget.max − 8`: 1 with the default cap of 9, and 0 with `MAX_LLM_CALLS=8`.
- The analyst and curator share it. The analyst uses it first simply because it runs first.
- If the curator is still invalid when the pool is empty, the route is FALLBACK (no extra call).

**Ordering.**
- `coverage_check` runs the dedupe, numbers the questions for the auditor, and computes `uncovered`.
- `finalize` runs caps → final numbering → floor warnings → `jira_comment.j2` render → size guard.
- The template and `postprocess.comment_context` were pulled forward from T5.1, because `finalize` needs them.

**`publish`.**
- It writes `comment.txt` and the other artifacts **before** any Jira call.
- In `--dry-run` it makes no Jira call at all.
- Otherwise it checks the marker (unless `--force`) and makes exactly one POST.
- A `JiraError` is returned as `result.jira_error`; the CLI maps it to exit 3 (T5.2).
- `fail` writes `run_report.json` and sets `result.ok=False` (exit 2).

**Call log.** Uses the configured model name for each agent (`rc.agent_models`), not the model name in the request.

**Live check (shopping, cheap dev models, dry-run, no ticket context).**
- 8 calls: 2 Gemini + 6 Groq, all ok, no retries.
- 24 questions, 5 of them common: the address and the order summary are each asked once. Nothing left uncovered.
- The auditor removed 2 duplicates and reworded 1 question.
- **153 s in total.** Time per call: specialists 2–49 s (waiting on the Groq TPM limit), curator 44 s, auditor 49 s (flash-lite with 4 images). NFR-1 still depends on the Groq tier (§9.4).

#### 9.7 M5 implementation notes (2026-11-17)

**CLI (`src/cli.py`), in run order.**
1. Load settings (CLI > process env > `.env`).
2. Check the model slots and required inputs.
3. Check the issue key format (`PROJ-123`, upper-cased).
4. Load the flow images.
5. Fetch the ticket context with a read-only GET whenever Jira credentials are set, **also in `--dry-run`** (FR-5). A failure is only a warning.
6. Run the graph.
7. Print a summary: calls (Gemini/Groq), questions, artifacts path, warnings.
8. Print the comment in `--dry-run`.

**Exit codes.**
- 0: dry-run, posted, or skipped because the marker is already there.
- 1: `ConfigError` / `InputError`, including a bad path, bad key, bad slot or missing secret.
- 2: `result.ok=False`, or any unexpected exception (last-resort guard).
- 3: `result.jira_error` (marker check or POST failed). `comment.txt` is already saved.

**Console and logging.**
- stdout/stderr are switched to UTF-8; the Windows cp1252 console crashed on `→ · —`.
- Logging: third-party libraries at WARNING; `src.*` at `LOG_LEVEL` (`-v` = DEBUG). Each LLM call and each Groq 429 wait is logged at INFO.
- LiteLLM debug banners are turned off.

**Comment template (T5.1).**
- `\[CATEGORY\]` is escaped: `[...]` is link syntax in Jira wiki markup. The §12.1 draft used it raw.
- The `_Please include: …_` line is left out when `answer_hint` is empty.
- When there are no questions, the comment says "No open questions were found for this flow."
- `Hi …` lists only the owners that have questions; all owners when there are none.
- Snapshots: `tests/snapshots/jira_comment_4_screens.txt` and `jira_comment_1_screen.txt`.

**Question text clean-up.**
- `clean_question()` removes internal ids such as `(RC1)` or `(S2.E2, S2.E3)` from question text; the live run showed the curator writing them.
- It runs in `validate_candidate` and in the auditor's reword.
- No prompt was changed.

**Live end-to-end (T5.4).**
- Command: `python run.py --file-path flows/shopping --dry-run`, cheap dev models, ticket context from the sandbox ticket.
- Result: exit 0; 8 calls (2 Gemini + 6 Groq); 26 questions, 8 common (stepper, address and order summary each asked once).
- **The auditor got a Gemini 503.** The error stub → "patch skipped" warning → the curated list was posted as designed.
- **160 s in total**: analyst 12 s; specialists 2–59 s and curator 48 s (Groq TPM waits); auditor 34 s until it failed.
- Still open for T6.3: Gemini 503 retry/timeout policy, and the Groq tier vs NFR-1.

#### 9.8 M6 results (2026-11-17)

**Hardening (T6.3).**
- **Gemini:** `http_options` on both vision agents: timeout 75 s, 2 attempts, retry on 429/5xx. A final failure → error stub → the existing graph routes handle it.
- **Groq:** 429 waits stay inside the logical call (§9.4). `PANEL_CONCURRENCY` stays 5: the free tier is limited by tokens per minute, so lower concurrency does not save tokens.
- **Friendly errors:** `friendly_error()` turns provider errors into one readable line in the report (rate limit or daily quota, 503 overload, timeout, rejected key).
- **Daily quota:** the Groq daily quota (TPD 200k on `gpt-oss-20b`) was used up by a day of tuning. The run ended cleanly with exit 2 and a quota message. `qwen/qwen3.8-27b` cannot serve as a backup: its free tier caps output at 1,000 tokens/min.

**CLI idempotency pre-check.**
- Without `--dry-run` / `--force`, the CLI checks the marker **before** running the graph, so a repeat run spends 0 LLM calls.
- `publish` still checks again, for race safety.
- Jira semantics are unchanged: skip when the marker is there; `--force` posts again.

**Question text.** `clean_question(text, labels)` drops ids in brackets and replaces bare ids (`S1.E3`, `RC1`, `S2`) with the on-screen label or screen name.

**Dedupe fix (from the eval).**
- A Jaccard merge now also needs the questions' element **labels** to overlap.
- Reason: templated questions ("When is <X> enabled or disabled?") about different elements scored high on Jaccard and could be merged wrongly.
- Same-RC merges are unchanged.

**New artifact.** `questions.json` is the final numbered list, exactly as rendered.

**Golden set (synthetic) and automated evaluation** (`python -m tests.live.evaluate`; cheap models; dry-run):

| flow | screens | calls | questions (common) | proxy recall | ask-once | x-screen dupes | uncovered after audit |
|---|---|---|---|---|---|---|---|
| shopping | 4 | 8 | 30 (6) | 91 % | ok | 0 | n/a* |
| login_otp | 3 | 8 | 14 (3) | 90 % | – | 0 | 0 |
| onboarding | 4 | 8 | 13 (5) | 100 % | ok | 0 | 0 |
| single_screen | 1 | 8 | 8 (0) | 100 % | – | 0 | 0 |
| repeated_component | 3 | 8 | 16 (7) | 90 % | ok | 0 | 3 |

\* That run was made before `questions.json` existed.
- Model time per run: 58–132 s on the Groq free tier.
- Proxy recall uses keyword topics written by the developer; it is a smoke signal, not the annotator target.

**Sandbox post (T6.4).** Results are in tasks.md T6.4.

**Acceptance (§14).**
- *Met (5 of 9):* the call budget, 2 Gemini + 6 Groq calls, models from `.env`, the caps, one comment with plain labels and contiguous numbers.
- *Open:*
  - annotator review: no duplicates and ≥ 80 % useful / ≥ 90 % recall;
  - full coverage on repeated_component (3 flagged elements left after the audit);
  - ≤ 90 s: about 1–2.5 min per run on the Groq free tier; needs a paid tier.

---

## 10. Budget & model plumbing

```python
class LLMBudget:                       # in RunContext; thread/async-safe (lock)
    def __init__(self, max_calls=9): self.max = min(max_calls, 9); self.used = 0; self.log = []
    def consume(self, agent) -> bool:
        with self.lock:
            if self.used >= self.max: return False
            self.used += 1; return True

def make_before_cb(rc, attach_images: bool):
    def cb(callback_context, llm_request):
        if not rc.budget.consume(callback_context.agent_name):
            return LlmResponse(content=types.Content(role="model",
                   parts=[types.Part(text='{"error":"budget_exhausted"}')]))
        if attach_images:
            llm_request.contents.append(types.Content(role="user", parts=rc.image_parts))  # all screens, in order
        return None
    return cb
```
Retry loops pass through the same callback, so retries are counted. Planned = 8; `retries_used ≤ max − 8`.

```python
def build_model(name):
    if name.startswith("gemini"): return name
    if name.startswith("groq/"):  return LiteLlm(model=name)   # optionally response_format={"type": "json_object"}
    raise ConfigError(...)
def validate_slots(s): ...   # vision → gemini*, text → groq/*
```

---

## 11. Post-processing (`src/postprocess.py`, no LLM)

1. **Merge candidates:** union across agents; validate; tag `source_agents`; drop invalid.
2. **Cross-screen dedupe** (`dedupe_across_screens`), applied after the curator *and* in `fallback_curate`:
   - Same `category` + same recurring component (via `element_refs` → RC membership) ⇒ merge.
   - Same `category`, different screens, token-Jaccard (lowercased, stopwords removed) ≥ 0.6 ⇒ merge.
   - Merged result: `scope=FLOW`, `screens` = union, `element_refs` = RC id if all refs are in one RC else union, `priority` = higher, `answer_hint` = longer, `source_agents` = union.
3. **Coverage:** `uncovered` = elements with `needs_clarification` and recurring components with no question; an element is covered if directly referenced, referenced via its RC, or its whole screen id is referenced with category ≠ `CONTENT`.
4. **Fallback curate:** dedupe (steps 1–2) → sort P1 first → apply caps.
5. **Apply patch:** `remove` (only allowed reasons, known `n`) → `reword` (≤ 300 chars) → `rescope` (screens ⊆ flow, ≥ 2 ⇒ `scope=FLOW`) → `add` (validated; `priority` default P2; `source_agents=["AUD"]`). Unknown `n` ⇒ ignored with warning.
6. **Caps:** per-screen (`MAX_Q_PER_SCREEN`), flow-level (`MAX_FLOW_Q`), total (`MAX_TOTAL_Q`). Drop order: P2 before P1; auditor-added P2 first; then single-source questions before multi-source.
7. **Order & number:** FLOW group first, then screens in flow order; inside each: P1 → P2, then owner order tech → product → design. `n` assigned sequentially in render order.
8. **Floor:** if a non-low screen ends with 0 questions and no FLOW question covers it ⇒ warning.
9. **Size guard:** after rendering, while `len(comment) > JIRA_COMMENT_MAX_CHARS`: drop lowest-priority question, re-number, re-render (warning).

---

## 12. Jira

### 12.1 Comment template `jira_comment.j2` (wiki markup)
```jinja
h2. Clarification questions — {{ flow_name | jira }} ({{ n_screens }} screen{{ 's' if n_screens != 1 }})
Hi {{ labels | join(', ') }} — to build this flow correctly we need the answers below. Please reply under this comment using the question numbers.

*Flow summary:* {{ flow_summary | jira }}
*Screens:* {% for s in screens %}{{ s.id }} {{ s.name | jira }}{{ ' · ' if not loop.last }}{% endfor %}

*Who answers what:* {% for o in owner_index %}{{ o.label }} → {{ o.numbers | join(', ') }}{{ ' · ' if not loop.last }}{% endfor %}

{% if common %}
h3. Common to multiple screens — answer once
{% for q in common %}
*Q{{ q.n }}. [{{ q.category_label }}] {{ q.element_label | jira }}* — applies to: {{ q.screen_names | join(', ') | jira }}{% if q.priority == "P1" %} {color:#de350b}(blocker){color}{% endif %}

{{ q.question | jira }}
_Please include: {{ q.answer_hint | jira }}_
Owner: {{ q.owner_label }}

{% endfor %}
{% endif %}
{% for s in screen_sections %}
h3. {{ s.id }} — {{ s.name | jira }}
{% for q in s.questions %}
*Q{{ q.n }}. [{{ q.category_label }}] {{ q.element_label | jira }}*{% if q.priority == "P1" %} {color:#de350b}(blocker){color}{% endif %}

{{ q.question | jira }}
_Please include: {{ q.answer_hint | jira }}_
Owner: {{ q.owner_label }}

{% endfor %}
{% endfor %}
----
{color:#6b778c}Reply as "Q1: …". Generated by the AI Flow-Question pipeline · run {{ run_id }} · {{ marker }}{color}
```
- `jira` filter escapes `\ { } [ ] * _ | ~ ^` in LLM text.
- `marker` = `ai-screen-questions:v2:<sha256(ordered image hashes)[:12]>`.
- Screens with no screen-specific questions are omitted (or show "No screen-specific questions").

### 12.2 Example (Shopping flow, 4 screens, abridged)
```
h2. Clarification questions — Shopping checkout flow (4 screens)
Hi @tech team, @product team — …
Screens: S1 Cart · S2 Shipping · S3 Place order · S4 Order success
Who answers what: @tech team → Q1, Q2, Q5, Q8 · @product team → Q3, Q4, Q6, Q7, Q9

h3. Common to multiple screens — answer once
Q1. [DATA_SOURCE] Shipping address block — applies to: Shipping, Place order, Order success (blocker)
Which API provides the saved address (path, method, fields), and is the same data reused on all three screens?
Please include: endpoint, method, response fields · Owner: @tech team

Q2. [DATA_SOURCE] Order summary (items, subtotal, total) — applies to: Cart, Place order, Order success
Is the order summary returned by one cart/order endpoint for all screens? Which fields hold item price, discount, shipping fee and total?
…
Q3. [BUSINESS_RULE] Price/total — applies to: Cart, Place order
How are taxes, shipping fee and promo discounts calculated and in what order? …

h3. S1 — Cart
Q4. [ACTION] Update quantity (+/−) and Remove  …
h3. S2 — Shipping
Q5. [VALIDATION] Name, Phone, Pincode, Address line  …
h3. S3 — Place order
Q7. [ACTION] Place order (blocker) What API does it call, what happens on payment failure …
```

### 12.3 Client (`src/jira_client.py`)
```python
class JiraClient:
    def __init__(self, base_url, email, token): ...    # requests.Session, auth=(email, token), timeout=20
    def get_issue_context(self, key) -> str            # GET /rest/api/2/issue/{key}?fields=summary,description
    def has_marker(self, key, marker) -> bool          # GET /rest/api/2/issue/{key}/comment (paginate)
    def post_comment(self, key, body) -> dict          # POST /rest/api/2/issue/{key}/comment {"body": body}
```
404 → "Issue not found / no permission"; 401/403 → auth message (token never printed); GET retried once; **POST never retried**.

---

## 13. Errors and exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (posted, dry-run done, or skipped because marker exists) |
| 1 | Config/input error (missing env, empty/oversized folder, bad image, slot mismatch) |
| 2 | Pipeline failure (`fail` node: analyst invalid after retry, <3 specialists usable, budget exhausted before a usable list) |
| 3 | Jira failure (comment still saved in `outputs/`) |

---

## 14. Testing & acceptance

### Unit tests (offline)
- Folder scan: natural sort, ignored files, >MAX_SCREENS, empty folder, single file.
- `extract_json`, inventory/candidate validators (scope/screens consistency, bad refs).
- `dedupe_across_screens`: same RC ⇒ one FLOW question; Jaccard merge; priority/union rules.
- `coverage`, `fallback_curate`, `apply_patch` (unknown `n`, rescope, invalid add), caps, numbering order, size guard.
- `jira` filter; `jira_comment.j2` snapshot for 1-screen and 4-screen flows.
- `LLMBudget`: 10th call refused; retry accounting; concurrent consumption.
- Graph test with stub agents: analyst invalid → RETRY → OK; analyst invalid twice → `fail`; 2 specialists fail → proceeds; 3 fail → `fail`; curator invalid → RETRY → FALLBACK; auditor invalid → skipped.

### Manual integration (keys required)
- Golden flows (`--dry-run`): shopping (4 screens), login/OTP (3), onboarding (4–5), single-screen, flow with a deliberately repeated component.
- One real post to a sandbox ticket; re-run (marker skip); `--force`.

### Acceptance criteria
- [x] `run_report.json.llm_calls ≤ 9` always; **8** on a normal run, independent of screen count.
- [x] Exactly 2 Gemini calls (both with all images) and 6 Groq calls on a normal run; verified from the report.
- [x] Models change via `.env` only.
- [ ] A component repeated on 3 screens (e.g. address) yields **one** FLOW question listing those screens; **0 duplicate** questions across screens on golden flows (annotator review).
- [x] Per-screen ≤ 8, flow-level ≤ 8, total ≤ 30; simple screens get fewer questions.
- [ ] Every flagged element/component is covered or justified by the auditor.
- [ ] 0 questions answerable from the image; ≥ 80 % rated useful, ≥ 90 % recall vs annotators' usual questions.
- [x] Exactly one comment posted; labels are plain `@product team` / `@tech team`; numbering contiguous.
- [ ] ≤ 90 s for 8 screens.
