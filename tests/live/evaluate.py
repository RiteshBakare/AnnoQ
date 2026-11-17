"""T6.2 evaluation harness (no LLM calls): score the latest run of each golden flow and write annotator sheets.

  python -m tests.live.evaluate [flow ...]          # default: every flow in tests/golden/references.json

For each flow it finds the newest outputs/<KEY>_<ts>/ whose run_report.flow_sha matches flows/<flow> and writes
  outputs/eval/summary.md            automated metrics (proxy recall, ask-once, duplicates, caps, calls, time)
  outputs/eval/<flow>_rating.csv     one row per question for annotators: rating = useful | generic |
                                     answerable_from_image | duplicate | wrong_scope (+ note)
Proxy recall uses keyword topics, so it is a smoke signal; the real targets need the annotators' ratings.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.image_utils import load_flow
from src.postprocess import coverage, jaccard, tokens
from src.schemas import Candidate, FlowInventory

REFS = Path("tests/golden/references.json")
OUT = Path("outputs/eval")
ID_LEAK = re.compile(r"\b(RC\d+|S\d+\.E\d+)\b")


def latest_run(flow_dir: str) -> Path | None:
    sha = load_flow(flow_dir).sha
    runs = [p.parent for p in Path("outputs").glob("*/run_report.json")
            if json.loads(p.read_text(encoding="utf-8")).get("flow_sha") == sha]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def matches(question: str, groups: list[list[str]]) -> bool:
    q = question.lower()
    return all(any(w in q for w in group) for group in groups)


def evaluate(flow: str, topics: list[dict]) -> dict | None:
    run = latest_run(f"flows/{flow}")
    if run is None:
        return None
    report = json.loads((run / "run_report.json").read_text(encoding="utf-8"))
    inv = json.loads((run / "flow_inventory.json").read_text(encoding="utf-8"))
    comment = (run / "comment.txt").read_text(encoding="utf-8")
    questions = json.loads((run / "curated.json").read_text(encoding="utf-8"))
    final = re.findall(r"^\*Q(\d+)\. ", comment, flags=re.M)
    if (run / "questions.json").exists():  # written by publish since M6
        numbered = [{**q, "element": ", ".join(q["element_refs"]), "blocker": q["priority"] == "P1"}
                    for q in json.loads((run / "questions.json").read_text(encoding="utf-8"))]
    else:  # older runs: rebuild from the rendered comment
        numbered = _final_questions(run)
    texts = [q["question"] for q in numbered]

    covered, ask_once = [], []
    for t in topics:
        hits = [q for q in numbered if matches(q["question"] + " " + q.get("answer_hint", ""), t["match"])]
        covered.append(bool(hits))
        if t.get("ask_once"):
            ask_once.append((t["topic"], any(q["scope"] == "FLOW" for q in hits)))

    labels = {e["id"]: e["label"] for s in inv["screens"] for e in s["elements"]}
    labels |= {rc["id"]: rc["name"] for rc in inv.get("recurring_components", [])}

    def subject(q: dict) -> set[str]:
        return tokens(" ".join(labels.get(r, "") for r in q.get("element_refs", [])) or q.get("element", ""))

    dupes = [(a["n"], b["n"]) for i, a in enumerate(numbered) for b in numbered[i + 1:]
             if a["category"] == b["category"] and set(a["screens"]) != set(b["screens"])
             and jaccard(tokens(a["question"]), tokens(b["question"])) >= 0.5 and subject(a) & subject(b)]
    per_screen = Counter(q["screens"][0] for q in numbered if q["scope"] == "SCREEN")
    complexity = {s["id"]: s["complexity"] for s in inv["screens"]}
    calls = report["calls"]
    uncovered = None
    if (run / "questions.json").exists():
        final_qs = [Candidate(**{k: v for k, v in q.items() if k in Candidate.model_fields}) for q in
                    json.loads((run / "questions.json").read_text(encoding="utf-8"))]
        uncovered = coverage(final_qs, FlowInventory.model_validate(inv))
    _write_sheet(flow, numbered, topics)
    return {
        "flow": flow, "run": run.name, "screens": report["screens"], "llm_calls": report["llm_calls"],
        "gemini": sum(c["model"].startswith("gemini") for c in calls),
        "groq": sum(c["model"].startswith("groq/") for c in calls),
        "seconds": round(sum(c["seconds"] for c in calls), 1), "questions": len(numbered),
        "common": sum(q["scope"] == "FLOW" for q in numbered), "recall": sum(covered) / len(topics),
        "missed": [t["topic"] for t, c in zip(topics, covered) if not c], "ask_once": ask_once,
        "cross_screen_dupes": dupes, "id_leaks": [t for t in texts if ID_LEAK.search(t)],
        "caps_ok": max(per_screen.values(), default=0) <= 8 and len(numbered) <= 30
        and sum(q["scope"] == "FLOW" for q in numbered) <= 8,
        "per_screen": {sid: f"{per_screen.get(sid, 0)} ({cx})" for sid, cx in complexity.items()},
        "warnings": report["warnings"], "curated_before_audit": len(questions), "rendered": len(final),
        "uncovered": uncovered,
    }


def _final_questions(run: Path) -> list[dict]:
    """Final numbered questions = state["questions"]; publish stores curated + comment, so parse the comment."""
    comment = (run / "comment.txt").read_text(encoding="utf-8")
    inv = json.loads((run / "flow_inventory.json").read_text(encoding="utf-8"))
    names = {s["id"]: s["name"] for s in inv["screens"]}
    by_name = {v: k for k, v in names.items()}
    out, section = [], None
    blocks = re.split(r"\n(?=\*Q\d+\. |h3\. )", comment)
    for b in blocks:
        if b.startswith("h3. "):
            title = b.split("\n", 1)[0][4:]
            section = None if title.startswith("Common") else title.split(" — ", 1)[0]
            continue
        m = re.match(r"\*Q(\d+)\. \\\[(\w+)\\\] (.*?)\*(?: — applies to: ([^{\n]*))?", b)
        if not m:
            continue
        lines = b.split("\n")
        question = lines[1].replace("\\", "") if len(lines) > 1 else ""
        hint = next((ln[len("_Please include: "):-1] for ln in lines if ln.startswith("_Please include: ")), "")
        owner = next((ln[len("Owner: "):] for ln in lines if ln.startswith("Owner: ")), "")
        if m.group(4):
            screens = [by_name.get(x.strip(), x.strip()) for x in m.group(4).replace("\\", "").split(",")]
            scope = "FLOW"
        else:
            screens, scope = [section], "SCREEN"
        out.append({"n": int(m.group(1)), "category": m.group(2), "element": m.group(3).replace("\\", ""),
                    "scope": scope, "screens": screens, "question": question, "answer_hint": hint.replace("\\", ""),
                    "owner": owner, "blocker": "(blocker)" in lines[0]})
    return out


def _write_sheet(flow: str, questions: list[dict], topics: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{flow}_rating.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "scope", "screens", "category", "owner", "blocker", "element", "question", "answer_hint",
                    "matched_topics", "rating (useful|generic|answerable_from_image|duplicate|wrong_scope)", "note"])
        for q in questions:
            hit = [t["topic"] for t in topics if matches(q["question"] + " " + q["answer_hint"], t["match"])]
            w.writerow([q["n"], q["scope"], " ".join(q["screens"]), q["category"], q["owner"], q["blocker"],
                        q["element"], q["question"], q["answer_hint"], "; ".join(hit), "", ""])


def main(flows: list[str]) -> None:
    refs = json.loads(REFS.read_text(encoding="utf-8"))
    flows = flows or [k for k in refs if not k.startswith("_")]
    rows = [r for f in flows if (r := evaluate(f, refs[f]))]
    lines = [f"# Golden-set evaluation ({datetime.now():%Y-%m-%d %H:%M})", "",
             "| flow | screens | calls (G/Groq) | model time s | questions (common) | proxy recall | ask-once | "
             "x-screen dupes | id leaks | caps | uncovered after audit |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        once = "/".join("ok" if ok else "MISS" for _, ok in r["ask_once"]) or "–"
        lines.append(f"| {r['flow']} | {r['screens']} | {r['llm_calls']} ({r['gemini']}/{r['groq']}) | "
                     f"{r['seconds']} | {r['questions']} ({r['common']}) | {r['recall']:.0%} | {once} | "
                     f"{len(r['cross_screen_dupes'])} | {len(r['id_leaks'])} | {'ok' if r['caps_ok'] else 'FAIL'} | "
                     f"{'n/a' if r['uncovered'] is None else len(r['uncovered'])} |")
    for r in rows:
        lines += ["", f"## {r['flow']}  (`{r['run']}`)", f"- per screen: {r['per_screen']}",
                  f"- missed topics: {r['missed'] or 'none'}",
                  f"- ask-once: {r['ask_once'] or 'n/a'}", f"- cross-screen near-duplicates: {r['cross_screen_dupes']}",
                  f"- uncovered after audit: {r['uncovered']}", f"- id leaks: {r['id_leaks']}",
                  f"- warnings: {r['warnings'] or 'none'}"]
    missing = [f for f in flows if f not in {r['flow'] for r in rows}]
    if missing:
        lines += ["", f"No run found for: {missing} (run `python run.py --file-path flows/<flow> --dry-run`)."]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1:])
