"""Deterministic post-processing (spec §11): validation, cross-screen dedupe, coverage, fallback curation, audit
patch, caps, ordering/numbering and the comment size guard. Pure functions: no I/O, no LLM."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, get_args

from pydantic import ValidationError

from .config import Settings
from .image_utils import Flow
from .prompts import extract_json
from .schemas import AgentTag, Candidate, Category, ElementType, FlowInventory, Question

FLOW = "FLOW"
JACCARD_MERGE = 0.6
REMOVE_REASONS = frozenset({"answerable_from_image", "not_on_screen", "duplicate"})
OWNER_ORDER = {"tech": 0, "product": 1, "design": 2}
DEFAULT_OWNER = {"DATA_SOURCE": "tech", "MEDIA": "tech", "ACTION": "tech", "NAVIGATION": "product",
                 "VALIDATION": "product", "STATE": "product", "VISIBILITY": "product",
                 "BUSINESS_RULE": "product", "PERMISSION": "product", "CONTENT": "product"}
CATEGORIES, ELEMENT_TYPES, AGENT_TAGS = set(get_args(Category)), set(get_args(ElementType)), set(get_args(AgentTag))
STOPWORDS = frozenset(
    "a an the is are be been to of for and or in on at by with what which when how does do did it its this that "
    "these those from as if any there should will would can could their they them we our you your user users".split())


class InvalidOutput(ValueError):
    """Agent output is unusable. The message is short enough to be used as the retry repair hint."""


class FlowIndex:
    """Lookups over a validated inventory: which refs exist, which screens they live on, RC membership."""

    def __init__(self, inv: FlowInventory):
        self.order = [s.id for s in inv.screens]
        self.pos = {sid: i for i, sid in enumerate(self.order)}
        self.element_screen = {e.id: s.id for s in inv.screens for e in s.elements}
        self.rc_screens = {rc.id: list(rc.appears_on) for rc in inv.recurring_components}
        self.element_rc = {ref: rc.id for rc in inv.recurring_components for ref in rc.element_refs}
        self.flagged = [e.id for s in inv.screens for e in s.elements if e.needs_clarification]
        self.complexity = {s.id: s.complexity for s in inv.screens}
        self.names = {s.id: s.name for s in inv.screens}
        self.labels = {FLOW: "Whole flow", **self.names, **{rc.id: rc.name for rc in inv.recurring_components},
                       **{e.id: e.label for s in inv.screens for e in s.elements}}

    def known(self, ref: str) -> bool:
        return ref == FLOW or ref in self.pos or ref in self.element_screen or ref in self.rc_screens

    def sort(self, screens: Iterable[str]) -> list[str]:
        return sorted(set(screens), key=self.pos.__getitem__)

    def screens_for(self, refs: Iterable[str]) -> list[str]:
        out: set[str] = set()
        for r in refs:
            out.update(self.rc_screens.get(r) or [self.element_screen.get(r) or (r if r in self.pos else None)])
        out.discard(None)
        return self.sort(out)

    def single_rc(self, refs: Iterable[str]) -> str | None:
        """The one recurring component all refs belong to (RC id or member element), else None."""
        rcs = {r if r in self.rc_screens else self.element_rc.get(r) for r in refs}
        return rcs.pop() if len(rcs) == 1 and None not in rcs else None


# --------------------------------------------------------------------------- helpers

def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _short(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])


def _load(raw: Any) -> Any:
    try:
        data = extract_json(raw)
    except ValueError as e:
        raise InvalidOutput(f"not valid JSON ({e})") from None
    if isinstance(data, dict) and "error" in data:
        raise InvalidOutput(f"agent error: {data['error']}")
    return data


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def as_candidate(q: Candidate) -> Candidate:
    return Candidate(**q.model_dump(exclude={"n"}))


def drop_rank(i: int, q: Candidate) -> tuple:
    """Sort key, lowest = dropped first (spec §11.6): P2 before P1; auditor-only P2 first; single-source before
    multi-source; later questions before earlier ones."""
    return q.priority == "P1", q.source_agents != ["AUD"], len(set(q.source_agents)) > 1, -i


# --------------------------------------------------------------------------- T3.1 validators

def validate_inventory(raw: Any, flow: Flow) -> tuple[FlowInventory, list[str]]:
    """Parse + validate the analyst output against the flow's images. Fatal problems raise InvalidOutput (-> RETRY);
    fixable ones are repaired with a warning."""
    data = _load(raw)
    if not isinstance(data, dict):
        raise InvalidOutput("expected a JSON object with flow_name, flow_summary, screens")
    for s in data.get("screens") or []:  # soft-coerce enums so a stray value doesn't cost a retry call
        if isinstance(s, dict):
            if s.get("complexity") not in ("low", "medium", "high"):
                s["complexity"] = "medium"
            for e in s.get("elements") or []:
                if isinstance(e, dict) and e.get("type") not in ELEMENT_TYPES:
                    e["type"] = "other"
    try:
        inv = FlowInventory.model_validate(data)
    except ValidationError as e:
        raise InvalidOutput(f"schema: {_short(e)}") from None

    expected, got = [s.id for s in flow.screens], [s.id for s in inv.screens]
    if got != expected:
        raise InvalidOutput(f"screens must be exactly {expected} in this order (got {got})")
    seen: set[str] = set()
    for s in inv.screens:
        for e in s.elements:
            if not re.fullmatch(rf"{s.id}\.E\d+", e.id):
                raise InvalidOutput(f"element id {e.id!r} on {s.id} must look like {s.id}.E<number>")
            if e.id in seen:
                raise InvalidOutput(f"duplicate element id {e.id}")
            seen.add(e.id)

    warnings = []
    for s, img in zip(inv.screens, flow.screens):
        s.file = img.file
        if not s.name.strip():
            s.name = img.hint or img.file
            warnings.append(f"{s.id}: no screen name from analyst; using {s.name!r}")
    kept, rc_ids = [], set()
    for rc in inv.recurring_components:
        rc.appears_on = [sid for sid in expected if sid in rc.appears_on]
        rc.element_refs = [r for r in dict.fromkeys(rc.element_refs) if r in seen]
        if len(rc.appears_on) < 2 or rc.id in rc_ids or not re.fullmatch(r"RC\d+", rc.id):
            warnings.append(f"recurring component {rc.id} ({rc.name!r}) dropped: invalid id or < 2 screens")
            continue
        rc_ids.add(rc.id)
        kept.append(rc)
    inv.recurring_components = kept
    before = len(inv.transitions)
    inv.transitions = [t for t in inv.transitions if t.from_screen in expected and t.to_screen in expected]
    if len(inv.transitions) < before:
        warnings.append(f"{before - len(inv.transitions)} transition(s) with unknown screens dropped")
    return inv, warnings


def validate_candidate(item: Any, inv: FlowInventory | FlowIndex, source: str | None = None) -> Candidate:
    """Validate and normalise one candidate (spec §6). Raises InvalidOutput with the reason.

    Coercions: unknown refs/screens removed; screens derived from the refs (an RC question is clamped to the RC's
    appears_on); scope follows the screen count (1 -> SCREEN, >=2 -> FLOW); missing owner -> category default;
    missing/odd priority -> P2; '?' appended; answer_hint trimmed to 200 chars.
    """
    idx = inv if isinstance(inv, FlowIndex) else FlowIndex(inv)
    if not isinstance(item, dict):
        raise InvalidOutput("candidate is not a JSON object")
    category = str(item.get("category") or "").strip().upper()
    if category not in CATEGORIES:
        raise InvalidOutput(f"unknown category {category!r}")
    question = " ".join(str(item.get("question") or "").split())
    if question and not question.endswith("?"):
        question = question.rstrip(".") + "?"
    if not 8 <= len(question) <= 300:
        raise InvalidOutput(f"question length {len(question)} outside 8-300")
    hint = " ".join(str(item.get("answer_hint") or "").split())
    if len(hint) > 200:
        hint = hint[:197].rsplit(" ", 1)[0] + "..."
    refs = [r for r in dict.fromkeys(_as_list(item.get("element_refs"))) if idx.known(r)]
    if not refs:
        raise InvalidOutput(f"no known element_refs in {item.get('element_refs')!r}")
    screens = idx.screens_for(refs) or idx.sort(s for s in _as_list(item.get("screens")) if s in idx.pos) \
        or list(idx.order)  # anchored only to "FLOW" with no screens -> whole flow
    owner = str(item.get("owner") or DEFAULT_OWNER[category]).strip().lower()
    priority = str(item.get("priority") or "").strip().upper()
    sources = [t for t in _as_list(item.get("source_agents")) if t in AGENT_TAGS]
    if source and source not in sources:
        sources.append(source)
    try:
        return Candidate(category=category, scope=FLOW if len(screens) >= 2 else "SCREEN", screens=screens,
                         element_refs=refs, question=question, answer_hint=hint, owner=owner,
                         priority=priority if priority in ("P1", "P2") else "P2", source_agents=sources)
    except ValidationError as e:
        raise InvalidOutput(_short(e)) from None


def _validate_all(items: list, idx: FlowIndex, source: str | None, label: str) -> tuple[list[Candidate], list[str]]:
    out, reasons = [], []
    for item in items:
        try:
            out.append(validate_candidate(item, idx, source))
        except InvalidOutput as e:
            reasons.append(str(e))
    warnings = [f"{label}: dropped {len(reasons)} invalid item(s), e.g. {reasons[0]}"] if reasons else []
    return out, warnings


def merge_candidates(joined: dict[str, Any], inv: FlowInventory,
                     tags: dict[str, str]) -> tuple[list[Candidate], int, list[str]]:
    """Panel join output {agent_name: raw} -> (valid candidates tagged by source, usable agent count, warnings).
    An agent is usable if it returned a parseable candidates list (an empty list is a valid answer)."""
    idx, out, usable, warnings = FlowIndex(inv), [], 0, []
    for name, raw in joined.items():
        tag = tags.get(name, name)
        try:
            data = _load(raw)
        except InvalidOutput as e:
            warnings.append(f"{tag}: unusable output ({e})")
            continue
        items = data.get("candidates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            warnings.append(f"{tag}: unusable output (no candidates list)")
            continue
        usable += 1
        valid, w = _validate_all(items, idx, tag, tag)
        out += valid
        warnings += w
    return out, usable, warnings


def parse_curated(raw: Any, inv: FlowInventory, n_candidates: int) -> tuple[list[Candidate], list[str]]:
    """Curator output -> valid questions. Raises InvalidOutput (-> RETRY/FALLBACK) when unusable, including an
    empty list when there were candidates to curate."""
    data = _load(raw)
    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InvalidOutput('missing "questions" list')
    valid, warnings = _validate_all(items, FlowIndex(inv), None, "curator")
    if n_candidates and not valid:
        raise InvalidOutput("no valid questions; keep the best candidates up to the targets, never return []")
    for q in valid:
        q.source_agents = q.source_agents or ["CUR"]
    return valid, warnings


# --------------------------------------------------------------------------- T3.2 cross-screen dedupe

def dedupe_across_screens(questions: list[Candidate], inv: FlowInventory,
                          threshold: float = JACCARD_MERGE) -> tuple[list[Candidate], int]:
    """Merge (a) same category + same recurring component, or (b) same category on different screens with token
    Jaccard >= threshold. Merged: FLOW (if >= 2 screens), union screens/sources, higher priority, longer hint,
    refs = the RC if all refs sit in one RC. Returns (questions, number of questions merged away)."""
    idx = FlowIndex(inv)
    n = len(questions)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rcs = [idx.single_rc(q.element_refs) for q in questions]
    toks = [tokens(q.question) for q in questions]
    for i in range(n):
        for j in range(i + 1, n):
            a, b = questions[i], questions[j]
            if a.category != b.category:
                continue
            same_rc = rcs[i] is not None and rcs[i] == rcs[j]
            if same_rc or (set(a.screens) != set(b.screens) and jaccard(toks[i], toks[j]) >= threshold):
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in sorted(groups.values()):
        if len(members) == 1:
            out.append(questions[members[0]])
            continue
        qs = [questions[i] for i in members]
        primary = min(qs, key=lambda q: q.priority != "P1")  # first P1, else first
        screens = idx.sort(s for q in qs for s in q.screens)
        refs = list(dict.fromkeys(r for q in qs for r in q.element_refs))
        rc = idx.single_rc(refs)
        out.append(primary.model_copy(update={
            "scope": FLOW if len(screens) >= 2 else "SCREEN", "screens": screens, "element_refs": [rc] if rc else refs,
            "priority": "P1" if any(q.priority == "P1" for q in qs) else "P2",
            "answer_hint": max((q.answer_hint for q in qs), key=len),
            "source_agents": list(dict.fromkeys(t for q in qs for t in q.source_agents)),
        }))
    return out, n - len(out)


# --------------------------------------------------------------------------- T3.3 coverage, fallback, patch

def coverage(questions: list[Candidate], inv: FlowInventory) -> list[str]:
    """Flagged elements and recurring components that no question covers (elements first, flow order)."""
    idx = FlowIndex(inv)
    refs = {r for q in questions for r in q.element_refs}
    screen_refs = {r for q in questions if q.category != "CONTENT" for r in q.element_refs if r in idx.pos}
    covered_rcs = {r for r in refs if r in idx.rc_screens} | {idx.element_rc[r] for r in refs if r in idx.element_rc}
    uncovered = [e for e in idx.flagged
                 if e not in refs and idx.element_rc.get(e) not in covered_rcs
                 and idx.element_screen[e] not in screen_refs]
    return uncovered + [rc for rc in idx.rc_screens if rc not in covered_rcs]


def fallback_curate(candidates: list[Candidate], inv: FlowInventory,
                    settings: Settings) -> tuple[list[Candidate], list[str]]:
    """Deterministic curation when the curator is unusable: exact + cross-screen dedupe, P1 first, caps."""
    unique: dict[tuple, Candidate] = {}
    for c in candidates:
        key = (c.category, tuple(c.screens), " ".join(sorted(tokens(c.question))))
        if key in unique:
            kept = unique[key]
            kept.source_agents = list(dict.fromkeys(kept.source_agents + c.source_agents))
            kept.priority = "P1" if "P1" in (kept.priority, c.priority) else "P2"
        else:
            unique[key] = c.model_copy()
    deduped, _ = dedupe_across_screens(list(unique.values()), inv)
    ranked = sorted(deduped, key=lambda q: q.priority != "P1")
    return apply_caps(ranked, inv, settings)


def apply_patch(questions: list[Question], raw: Any, inv: FlowInventory) -> tuple[list[Candidate], list[str]]:
    """Apply the auditor patch: remove (allowed reasons) -> reword -> rescope (>= 2 screens -> FLOW) -> add (max 3,
    validated, P2 default, source AUD). An unusable patch is skipped; unknown n are ignored with a warning."""
    base = [as_candidate(q) for q in questions]
    try:
        data = _load(raw)
        if not isinstance(data, dict):
            raise InvalidOutput("expected a JSON object")
    except InvalidOutput as e:
        return base, [f"auditor patch skipped: {e}"]
    idx, by_n, warnings, removed = FlowIndex(inv), {q.n: c for q, c in zip(questions, base)}, [], set()

    def target(op: Any, kind: str) -> int | None:
        n = op.get("n") if isinstance(op, dict) else None
        n = int(n) if isinstance(n, (int, float)) or (isinstance(n, str) and n.isdigit()) else None
        if n not in by_n or n in removed:
            warnings.append(f"auditor {kind}: unknown question n={op.get('n') if isinstance(op, dict) else op!r}")
            return None
        return n

    for op in data.get("remove") or []:
        if (n := target(op, "remove")) is not None:
            if op.get("reason") in REMOVE_REASONS:
                removed.add(n)
            else:
                warnings.append(f"auditor remove Q{n}: reason {op.get('reason')!r} not allowed")
    for op in data.get("reword") or []:
        if (n := target(op, "reword")) is not None:
            text = " ".join(str(op.get("question") or "").split())
            text = text if not text or text.endswith("?") else text.rstrip(".") + "?"
            if 8 <= len(text) <= 300:
                by_n[n].question = text
            else:
                warnings.append(f"auditor reword Q{n}: length {len(text)} outside 8-300")
    for op in data.get("rescope") or []:
        if (n := target(op, "rescope")) is not None:
            screens = idx.sort(s for s in _as_list(op.get("screens")) if s in idx.pos)
            if len(screens) >= 2:
                by_n[n].scope, by_n[n].screens = FLOW, screens
            else:
                warnings.append(f"auditor rescope Q{n}: needs >= 2 known screens")
    adds = data.get("add") or []
    if len(adds) > 3:
        warnings.append(f"auditor add: {len(adds)} items, only the first 3 used")
    added, w = _validate_all([{**a, "priority": a.get("priority") or "P2", "source_agents": []}
                              for a in adds[:3] if isinstance(a, dict)], idx, "AUD", "auditor add")
    return [by_n[q.n] for q in questions if q.n not in removed] + added, warnings + w


# --------------------------------------------------------------------------- T3.4 caps, order, number, size

def apply_caps(questions: list[Candidate], inv: FlowInventory,
               settings: Settings) -> tuple[list[Candidate], list[str]]:
    """Enforce MAX_FLOW_Q, MAX_Q_PER_SCREEN (screen-specific) and MAX_TOTAL_Q, dropping by drop_rank."""
    order = [s.id for s in inv.screens]
    kept = dict(enumerate(questions))
    warnings: list[str] = []

    def trim(members: list[int], cap: int, label: str) -> None:
        excess = len(members) - cap
        if excess > 0:
            for i in sorted(members, key=lambda i: drop_rank(i, kept[i]))[:excess]:
                del kept[i]
            warnings.append(f"cap {label}={cap}: dropped {excess} lower-priority question(s)")

    trim([i for i, q in kept.items() if q.scope == FLOW], settings.max_flow_q, "MAX_FLOW_Q")
    for sid in order:
        trim([i for i, q in kept.items() if q.scope == "SCREEN" and q.screens == [sid]],
             settings.max_q_per_screen, f"MAX_Q_PER_SCREEN ({sid})")
    trim(list(kept), settings.max_total_q, "MAX_TOTAL_Q")
    return [kept[i] for i in sorted(kept)], warnings


def order_and_number(questions: list[Candidate], inv: FlowInventory) -> list[Question]:
    """FLOW group first, then screens in flow order; within a group P1 -> P2, then tech -> product -> design.
    n = 1..N in that render order."""
    pos = {s.id: i for i, s in enumerate(inv.screens)}

    def key(item: tuple[int, Candidate]) -> tuple:
        i, q = item
        group = -1 if q.scope == FLOW else pos[q.screens[0]]
        return group, q.priority != "P1", OWNER_ORDER[q.owner], i

    ranked = sorted(enumerate(questions), key=key)
    return [Question(**q.model_dump(exclude={"n"}), n=k) for k, (_, q) in enumerate(ranked, start=1)]


def floor_warnings(questions: list[Candidate], inv: FlowInventory) -> list[str]:
    """Spec §11.8: a non-low screen with no screen question and no FLOW question covering it."""
    return [f"{s.id} ({s.name}) has no questions" for s in inv.screens
            if s.complexity != "low" and not any(s.id in q.screens for q in questions)]


def fit_to_size(questions: list[Question], render: Callable[[list[Question]], str],
                max_chars: int) -> tuple[list[Question], str, list[str]]:
    """Render; while too long, drop the most droppable question, renumber, re-render (spec §11.9)."""
    qs, text, dropped = list(questions), render(questions), 0
    while len(text) > max_chars and qs:
        worst = min(range(len(qs)), key=lambda i: drop_rank(i, qs[i]))
        del qs[worst]
        qs = [q.model_copy(update={"n": k}) for k, q in enumerate(qs, start=1)]
        text, dropped = render(qs), dropped + 1
    warnings = [f"comment over {max_chars} chars: dropped {dropped} question(s)"] if dropped else []
    return qs, text, warnings


# --------------------------------------------------------------------------- comment context (T5.1 template input)

def comment_context(questions: list[Question], inv: FlowInventory, owner_labels: dict[str, str], run_id: str,
                    marker: str) -> dict:
    """Template variables for jira_comment.j2: common (FLOW) section, per-screen sections, owner index."""
    idx = FlowIndex(inv)

    def view(q: Question) -> dict:
        labels = list(dict.fromkeys(idx.labels.get(r, r) for r in q.element_refs))
        label = ", ".join(labels[:3]) + (f" +{len(labels) - 3} more" if len(labels) > 3 else "")
        return {**q.model_dump(), "element_label": label, "owner_label": owner_labels[q.owner],
                "screen_names": [idx.names[s] for s in q.screens]}

    owners = [o for o in OWNER_ORDER if any(q.owner == o for q in questions)]
    sections = [{"id": s.id, "name": s.name,
                 "questions": [view(q) for q in questions if q.scope == "SCREEN" and q.screens == [s.id]]}
                for s in inv.screens]
    return {
        "flow_name": inv.flow_name, "flow_summary": inv.flow_summary, "n_screens": len(inv.screens),
        "screens": [{"id": s.id, "name": s.name} for s in inv.screens],
        "labels": [owner_labels[o] for o in owners] or list(owner_labels.values()),
        "owner_index": [{"label": owner_labels[o], "numbers": [f"Q{q.n}" for q in questions if q.owner == o]}
                        for o in owners],
        "common": [view(q) for q in questions if q.scope == FLOW],
        "screen_sections": [s for s in sections if s["questions"]],
        "run_id": run_id, "marker": marker,
    }
