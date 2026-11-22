#!/usr/bin/env python3
"""
Rivia emotion system, Step 2.8: rerun the 2.6 baseline set after Gemini model
updates and catch personality drift.

DRAFT. Nothing here has been run against Gemini. It reuses 2.6's baseline.py
and prompts.json (imported, not copied).

Commands
  watch        no Gemini calls. Has anything changed since the reference runs?
               One reference per profile (Jordan's Nov 22 plan: balanced =
               Gemini 4 Hydrogen at medium, deep = Gemini 4 Neon at high), each
               checked against engine.json, Rivia's system instruction, the
               prompt set and Google changelog lines naming its model. Prints a
               flag and the rerun list with its call count. Never runs it.
  targets      the same check, as one line per profile to rerun (for rerun.sh).
  register     file a finished, scored 2.6 run into history/ with its model,
               profile, date, prompt-set hash, system-instruction hash, rater
               and engine snapshot. Does not touch the reference.
  set-reference  by hand only: make a registered run the reference for its
               profile and channel.
  anchor       PAID (40 calls, needs --confirm-spend): today's rater re-rates
               one stored reference reply per prompt, so a change in the rater
               itself is not mistaken for a change in Rivia.
  compare      no Gemini calls. New run against the reference: per pass, method
               and axis, the paired difference with a 95% interval and a verdict.
  selfcheck    no Gemini calls. Compares a run against itself split by reps. Any
               flag here means the thresholds are tighter than the noise.

Never: runs generate/rate by itself, changes Rivia's baseline layer, or writes
outside the history folder. The baseline layer (2.1) is changed only by Jordan.

Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 2.6's folder in the drafts; at install baseline.py can sit beside this file instead.
BASELINE_DIR = Path(os.environ.get("RIVIA_BASELINE_DIR", HERE.parent / "2.6"))
if not (BASELINE_DIR / "baseline.py").exists() and (HERE / "baseline.py").exists():
    BASELINE_DIR = HERE
sys.path.insert(0, str(BASELINE_DIR))
import baseline as B  # noqa: E402  (2.6)

AXES = B.AXES
PASSES = B.PASSES
METHODS = ("flash", "lex")
CHANGELOG_URL = "https://ai.google.dev/gemini-api/docs/changelog"

# --------------------------------------------------------------------------
# Thresholds (on Rivia's -1..+1 scale). Reasons in README.md.
# --------------------------------------------------------------------------
WATCH = 0.10      # smallest shift that would change a baseline rounded to 0.1
DRIFT = 0.20      # two rounding steps; ~60% of replies one rater step off
CATEGORY = 0.33   # one whole Flash rater step, averaged over a category
RATER_MOVED = 0.10
# Every flag also needs the 95% interval (bootstrap over prompts) to exclude 0.
# The lexicon has no level threshold: it is only a direction check.


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


class History:
    """history/index.json: {"runs": [record...], "reference": {"balanced/text": id}}.
    Each run's files are copied to history/<id>/ so a record outlives its run dir."""

    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / "index.json"
        if self.index_path.exists():
            self.data = json.loads(self.index_path.read_text())
        else:
            self.data = {"runs": [], "reference": {}}

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.index_path)

    def get(self, run_id: str) -> dict:
        for r in self.data["runs"]:
            if r["id"] == run_id:
                return r
        raise SystemExit(f"no registered run {run_id}")

    def reference(self, key: str) -> dict | None:
        rid = self.data["reference"].get(key)
        return self.get(rid) if rid else None

    def dir(self, run_id: str) -> Path:
        return self.root / run_id


def key_of(rec: dict) -> str:
    return f"{rec['profile']}/{rec['channel']}"


def engine_snapshot(config_dir: Path | None) -> dict:
    if not config_dir or not (config_dir / "engine.json").exists():
        return {}
    eng = json.loads((config_dir / "engine.json").read_text())
    return {name: {"model": p.get("model"), "thinking_level": p.get("thinking_level")}
            for name, p in (eng.get("profiles") or {}).items()}


def infer_profile(engine: dict, model: str, thinking: str | None) -> str:
    """The engine.json profile that runs this model at this thinking level.
    Needs --profile when none or several match (e.g. the app switch to a new
    model isn't installed yet, so engine.json doesn't name it)."""
    hits = [name for name, p in engine.items()
            if (p.get("model") or "").removeprefix("models/") == model and p.get("thinking_level") == thinking]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"can't tell which profile {model} at {thinking} belongs to "
                     f"({'none' if not hits else ', '.join(hits)} in engine.json); pass --profile")


def cmd_register(args):
    run = Path(args.run)
    meta = json.loads((run / "meta.json").read_text())
    for f in ("replies.jsonl", "scores.csv", "summary.json"):
        if not (run / f).exists():
            raise SystemExit(f"{run} has no {f}: run 2.6 generate, rate and score first")
    prompts_path = Path(args.prompts)
    prompts_doc = json.loads(prompts_path.read_text())
    if prompts_doc["version"] != meta["prompt_set_version"]:
        raise SystemExit("prompts.json version differs from the run's; pass the prompts file the run used")
    started = meta.get("started", "")[:10] or date.today().isoformat()
    model = meta["model"].removeprefix("models/")
    engine = engine_snapshot(Path(args.config) if args.config else None)
    profile = args.profile or infer_profile(engine, model, meta.get("thinking_level"))
    args.profile = profile
    rid = f"{started}_{profile}_{meta['channel']}_{model}"
    h = History(Path(args.history))
    n = 2
    base = rid
    while any(r["id"] == rid for r in h.data["runs"]):
        rid = f"{base}_{n}"
        n += 1
    rec = {
        "id": rid, "date": started, "registered": datetime.now().isoformat(timespec="seconds"),
        "model": model, "profile": args.profile, "thinking_level": meta.get("thinking_level"),
        "channel": meta["channel"], "reps": meta["reps"],
        "prompt_set_version": meta["prompt_set_version"], "prompt_set_sha": file_sha(prompts_path),
        "system_sha": sha(meta.get("rivia_system_instruction", "")),
        "system_chars": len(meta.get("rivia_system_instruction", "")),
        "rater_model": args.rater_model, "rater_thinking": args.rater_thinking,
        "lexicon_sha": file_sha(Path(args.lexicon)) if args.lexicon else None,
        "engine": engine,
        "note": args.note or "",
    }
    dest = h.dir(rid)
    dest.mkdir(parents=True)
    for f in ("meta.json", "replies.jsonl", "ratings.jsonl", "scores.csv", "summary.json", "anchor.jsonl"):
        if (run / f).exists():
            shutil.copy2(run / f, dest / f)
    h.data["runs"].append(rec)
    h.save()
    print(f"registered {rid}")
    ref = h.reference(key_of(rec))
    if ref is None:
        print(f"no reference yet for {key_of(rec)}. To use this run as one (your call):")
        print(f"  python3 drift.py set-reference --id {rid}")


def cmd_set_reference(args):
    h = History(Path(args.history))
    rec = h.get(args.id)
    old = h.data["reference"].get(key_of(rec))
    h.data["reference"][key_of(rec)] = rec["id"]
    h.data.setdefault("reference_log", []).append(
        {"at": datetime.now().isoformat(timespec="seconds"), "key": key_of(rec),
         "from": old, "to": rec["id"], "why": args.why or ""})
    h.save()
    print(f"reference for {key_of(rec)}: {old} -> {rec['id']}")
    print("This changes only what future runs are compared against. Rivia's baseline layer is untouched.")


# --------------------------------------------------------------------------
# Loading scores
# --------------------------------------------------------------------------


def load_rows(run_dir: Path) -> list[dict]:
    rows = []
    with open(run_dir / "scores.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            row = {"pass": r["pass"], "id": r["id"], "category": r["category"], "rep": int(r["rep"])}
            for m in METHODS:
                for ax in AXES:
                    v = r.get(f"{m}_{ax}", "")
                    row[f"{m}_{ax}"] = float(v) if v not in ("", "None", None) else None
            rows.append(row)
    return rows


def load_anchor(run_dir: Path) -> dict:
    """anchor.jsonl -> {prompt_id: {ax: today's rating of the stored reference reply}}"""
    path = run_dir / "anchor.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r.get("rating"):
            out[r["id"]] = {"pass": r["pass"], "rep": r["rep"], **r["rating"]}
    return out


def prompt_means(rows, ps, col, category=None):
    pm = {}
    for x in rows:
        if x["pass"] == ps and x[col] is not None and (category is None or x["category"] == category):
            pm.setdefault(x["id"], []).append(x[col])
    return {k: B.mean(v) for k, v in pm.items()}


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def comparable(ref: dict, new: dict) -> tuple[list[str], list[str]]:
    """(blockers, rivia_only_blockers). A blocker stops the whole comparison;
    a rivia-only one means the rivia pass measured a different prompt."""
    block, rivia = [], []
    if ref["prompt_set_sha"] != new["prompt_set_sha"] or ref["prompt_set_version"] != new["prompt_set_version"]:
        block.append("prompt set differs (version or contents): runs aren't comparable")
    if ref["channel"] != new["channel"] or ref["profile"] != new["profile"]:
        block.append("different channel or profile")
    if ref.get("lexicon_sha") != new.get("lexicon_sha"):
        block.append("different lexicon file (lexicon columns not comparable)")
    if ref["system_sha"] != new["system_sha"]:
        rivia.append("Rivia's system instruction changed since the reference, so a rivia-pass "
                     "difference could be her prompt, not the model")
    return block, rivia


def verdict(d, lo, hi, threshold):
    if d is None or lo is None:
        return "no data"
    clear = lo > 0 or hi < 0
    if clear and abs(d) >= threshold:
        return "drift" if threshold == DRIFT else "flag"
    if clear and abs(d) >= WATCH and threshold == DRIFT:
        return "watch"
    return "steady"


def compare_rows(ref_rows, new_rows, anchor=None, ref_rows_for_anchor=None, seed=26) -> dict:
    """Paired by prompt: new prompt mean minus reference prompt mean, cluster
    bootstrap over prompts (2.6's cluster_boot_ci). With an anchor, Flash diffs
    are corrected per prompt by how much today's rater scores the identical
    reference reply differently."""
    out = {"cells": {}, "categories": {}, "rater_shift": {}}
    # rater shift: anchor rating minus stored reference rating, same reply
    shift_by_prompt = {ax: {} for ax in AXES}
    if anchor:
        ref_idx = {(x["pass"], x["id"], x["rep"]): x for x in ref_rows_for_anchor or ref_rows}
        for pid, a in anchor.items():
            r = ref_idx.get((a["pass"], pid, a["rep"]))
            if not r:
                continue
            for ax in AXES:
                if r[f"flash_{ax}"] is not None:
                    shift_by_prompt[ax][pid] = a[ax] - r[f"flash_{ax}"]
        for ax in AXES:
            s = shift_by_prompt[ax]
            lo, hi = B.cluster_boot_ci({k: [v] for k, v in s.items()}, seed=seed)
            m = B.mean(list(s.values())) if s else None
            moved = m is not None and lo is not None and (lo > 0 or hi < 0) and abs(m) >= RATER_MOVED
            out["rater_shift"][ax] = {"mean": B.r2(m), "ci95": [B.r2(lo), B.r2(hi)],
                                      "n": len(s), "moved": moved}

    for ps in PASSES:
        for m in METHODS:
            for ax in AXES:
                col = f"{m}_{ax}"
                rp, np_ = prompt_means(ref_rows, ps, col), prompt_means(new_rows, ps, col)
                ids = sorted(set(rp) & set(np_))
                if not ids:
                    continue
                diffs = {i: [np_[i] - rp[i]] for i in ids}
                cell = {"n_prompts": len(ids)}
                if m == "flash" and anchor and out["rater_shift"].get(ax, {}).get("moved"):
                    # only correct when the rater clearly moved; otherwise the
                    # correction adds noise for nothing
                    sp = shift_by_prompt[ax]
                    raw_d = B.mean([v[0] for v in diffs.values()])
                    diffs = {i: [v[0] - sp[i]] for i, v in diffs.items() if i in sp}
                    cell["uncorrected"] = B.r2(raw_d)
                    cell["corrected_for_rater"] = True
                lo, hi = B.cluster_boot_ci(diffs, seed=seed)
                d = B.mean([v[0] for v in diffs.values()])
                cell.update({"diff": B.r2(d), "ci95": [B.r2(lo), B.r2(hi)],
                             "clear": bool(lo is not None and (lo > 0 or hi < 0)),
                             "ref_mean": B.r2(B.mean([rp[i] for i in ids])),
                             "new_mean": B.r2(B.mean([np_[i] for i in ids]))})
                # lexicon: direction only (it compresses toward 0)
                cell["verdict"] = (verdict(d, lo, hi, DRIFT) if m == "flash"
                                   else ("moved" if cell["clear"] else "steady"))
                out["cells"][f"{ps}.{col}"] = cell
        # per category, Flash only, one-step threshold
        cats = sorted({x["category"] for x in ref_rows})
        for c in cats:
            for ax in AXES:
                col = f"flash_{ax}"
                rp = prompt_means(ref_rows, ps, col, c)
                np_ = prompt_means(new_rows, ps, col, c)
                ids = sorted(set(rp) & set(np_))
                if len(ids) < 3:
                    continue
                diffs = {i: [np_[i] - rp[i]] for i in ids}
                if anchor and out["rater_shift"].get(ax, {}).get("moved"):
                    diffs = {i: [v[0] - shift_by_prompt[ax][i]] for i, v in diffs.items()
                             if i in shift_by_prompt[ax]}
                lo, hi = B.cluster_boot_ci(diffs, seed=seed)
                d = B.mean([v[0] for v in diffs.values()])
                v = verdict(d, lo, hi, CATEGORY)
                if v == "flag":
                    out["categories"][f"{ps}.{c}.{ax}"] = {"diff": B.r2(d), "ci95": [B.r2(lo), B.r2(hi)],
                                                          "n_prompts": len(ids)}
    return out


def interpret(res: dict, rivia_ok: bool) -> dict:
    """Overall verdict from the Flash cells, with the lexicon as a direction
    check. Returns {"overall": steady|watch|drift, "lines": [...]}."""
    lines, level = [], "steady"
    rank = {"steady": 0, "watch": 1, "drift": 2}
    for ax, name in zip(AXES, ("valence", "arousal", "dominance")):
        for ps in PASSES:
            if ps == "rivia" and not rivia_ok:
                continue
            c = res["cells"].get(f"{ps}.flash_{ax}")
            if not c or c["verdict"] in ("steady", "no data"):
                continue
            lx = res["cells"].get(f"{ps}.lex_{ax}")
            agree = ""
            if lx and lx["clear"]:
                agree = (" The word lexicon moved the same way." if lx["diff"] * c["diff"] > 0
                         else " The word lexicon moved the OPPOSITE way: treat with suspicion.")
            elif lx:
                agree = " The word lexicon saw no clear change."
            lines.append(f"{ps} pass {name}: {c['ref_mean']} -> {c['new_mean']} "
                         f"(change {c['diff']:+}, 95% {c['ci95']}) = {c['verdict']}.{agree}")
            if rank[c["verdict"]] > rank[level]:
                level = c["verdict"]
    for k, c in res["categories"].items():
        lines.append(f"category {k}: change {c['diff']:+} over {c['n_prompts']} prompts (95% {c['ci95']})")
        level = max(level, "watch", key=rank.get)
    for ax, s in res["rater_shift"].items():
        if s["moved"]:
            lines.append(f"the rater itself scores identical replies {s['mean']:+} on {ax}; "
                         f"Flash changes above are corrected for that")
    # what the pattern means
    raw_hit = any(res["cells"].get(f"raw.flash_{ax}", {}).get("verdict") in ("watch", "drift") for ax in AXES)
    riv_hit = rivia_ok and any(res["cells"].get(f"rivia.flash_{ax}", {}).get("verdict") in ("watch", "drift")
                               for ax in AXES)
    if raw_hit and riv_hit:
        reading = "Factory Gemini and Rivia both moved: the model changed and her prompt didn't hold her."
    elif riv_hit:
        reading = "Only the rivia pass moved: the new model reads her prompt differently."
    elif raw_hit:
        reading = "Factory Gemini moved but the rivia pass held: her prompt is absorbing the change."
    else:
        reading = "No clear change on any axis."
    return {"overall": level, "reading": reading, "lines": lines}


def cmd_compare(args):
    h = History(Path(args.history))
    new = h.get(args.new)
    ref = h.get(args.ref) if args.ref else h.reference(key_of(new))
    if ref is None:
        raise SystemExit(f"no reference for {key_of(new)}; set one with set-reference")
    if ref["id"] == new["id"]:
        raise SystemExit("new run is the reference; use selfcheck")
    block, rivia = comparable(ref, new)
    if block:
        raise SystemExit("not comparable: " + "; ".join(block))
    ref_rows, new_rows = load_rows(h.dir(ref["id"])), load_rows(h.dir(new["id"]))
    anchor = load_anchor(h.dir(new["id"]))
    res = compare_rows(ref_rows, new_rows, anchor)
    rep = interpret(res, rivia_ok=not rivia)
    report = {"ref": ref["id"], "new": new["id"], "compared_at": datetime.now().isoformat(timespec="seconds"),
              "thresholds": {"watch": WATCH, "drift": DRIFT, "category": CATEGORY, "rater_moved": RATER_MOVED},
              "rivia_pass_comparable": not rivia, "warnings": rivia
              + ([] if anchor else ["no anchor: a change in the rater would look like a change in Rivia"]),
              **rep, "detail": res}
    (h.dir(new["id"]) / f"compare_vs_{ref['id']}.json").write_text(json.dumps(report, indent=2))
    print_compare(report, ref, new)
    return report


def print_compare(r, ref, new):
    print(f"{new['id']}  vs reference  {ref['id']}")
    print(f"models: {ref['model']} ({ref['date']}) -> {new['model']} ({new['date']})")
    print(f"VERDICT: {r['overall'].upper()}. {r['reading']}")
    for line in r["lines"]:
        print("  " + line)
    for w in r["warnings"]:
        print("WARNING: " + w)
    if r["overall"] != "steady":
        print("Nothing has been changed. Rivia's baseline stays as it is unless Jordan decides otherwise.")
    print(f"(watch >= {WATCH}, drift >= {DRIFT} on -1..+1, and the 95% interval must exclude 0. "
          "Second decimals mean nothing.)")


def cmd_selfcheck(args):
    h = History(Path(args.history))
    rec = h.get(args.id)
    rows = load_rows(h.dir(rec["id"]))
    reps = sorted({x["rep"] for x in rows})
    if len(reps) < 2:
        raise SystemExit("selfcheck needs a run with 2 or more reps")
    a = [x for x in rows if x["rep"] % 2 == 0]
    b = [x for x in rows if x["rep"] % 2 == 1]
    res = compare_rows(a, b)
    rep = interpret(res, rivia_ok=True)
    print(f"selfcheck {rec['id']}: even reps vs odd reps (same model, same day)")
    print(f"result: {rep['overall'].upper()}")
    for line in rep["lines"]:
        print("  " + line)
    if rep["overall"] != "steady":
        print("The thresholds flag noise in this run: raise them or run more reps before trusting compare.")
    return rep


# --------------------------------------------------------------------------
# Anchor (PAID): today's rater re-rates stored reference replies
# --------------------------------------------------------------------------


def anchor_jobs(ref_dir: Path) -> list[dict]:
    """One stored reply per prompt: rivia pass, lowest rep with text."""
    replies = [json.loads(l) for l in (ref_dir / "replies.jsonl").read_text().splitlines()]
    pick = {}
    for r in sorted(replies, key=lambda r: r["rep"]):
        if r["pass"] == "rivia" and r["reply"].strip() and r["id"] not in pick:
            pick[r["id"]] = r
    return [pick[k] for k in sorted(pick)]


def cmd_anchor(args):
    h = History(Path(args.history))
    ref = h.get(args.ref)
    jobs = anchor_jobs(h.dir(ref["id"]))
    if not args.confirm_spend:
        raise SystemExit(f"anchor costs {len(jobs)} Gemini calls: rerun with --confirm-spend once Jordan says yes")
    key = B._need_key()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "anchor.jsonl"
    done = set()
    if path.exists():
        done = {json.loads(l)["id"] for l in path.read_text().splitlines()}
    # 2.6's guards: --max-calls (default 600) and --rpm (default 10), tallied in
    # the run's cost.json so the anchor counts toward the same cap as generate and rate.
    budget = B.make_budget(args, out)
    try:
        with open(path, "a") as fh:
            for r in jobs:
                if r["id"] in done:
                    continue
                budget.check()
                res = B.interaction(B.rater_body(r["reply"], args.rater_model, args.rater_thinking), key)
                budget.add(args.rater_model, res.get("usage"))
                txt = B.output_text(res)
                fh.write(json.dumps({"pass": r["pass"], "id": r["id"], "rep": r["rep"], "ref": ref["id"],
                                     "rating": B.parse_rating(txt), "raw": txt[:200]}) + "\n")
                fh.flush()
    finally:
        budget.save(out)
    print(f"anchor written to {path}; register the run afterwards so it travels with it")


# --------------------------------------------------------------------------
# Watch: notice a model update. No Gemini calls.
# --------------------------------------------------------------------------

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})\b")
CHANGE_WORDS = re.compile(r"deprecat|routed|replaced|points to|now the model|shut ?down|retire|updated|switched",
                          re.I)


def changelog_text_from_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div|tr)>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def changelog_hits(text: str, models: set[str], since: str) -> list[dict]:
    """Lines under a date heading later than `since` that name a watched model
    and use a change word. The page is someone else's text: lines are only
    matched and quoted to Jordan, never acted on."""
    since_d = date.fromisoformat(since)
    current = None
    hits = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        m = DATE_RE.search(line)
        if m and len(line) < 40:
            current = datetime.strptime(" ".join(m.groups()), "%B %d %Y").date()
            continue
        if current is None or current <= since_d:
            continue
        named = sorted(mod for mod in models if re.search(rf"(?<![\w.-]){re.escape(mod)}(?![\w-]|\.\w)", line))
        if named and CHANGE_WORDS.search(line):
            hits.append({"date": current.isoformat(), "models": named, "line": line[:240]})
    return hits


def mt(p: dict | None) -> tuple:
    """(model, thinking) with any models/ prefix dropped."""
    if not p:
        return (None, None)
    return ((p.get("model") or "").removeprefix("models/") or None, p.get("thinking_level"))


def fmt(t: tuple) -> str:
    return f"{t[0]} at {t[1]}"


def references(h: History) -> list[dict]:
    return [h.get(rid) for _, rid in sorted(h.data["reference"].items())]


def watch(config_dir: Path, h: History, changelog: str | None, prompts_path: Path) -> list[dict]:
    """Every reference (one per profile: everyday Hydrogen and deep Neon under
    Jordan's Nov 22 plan) is checked on its own. Findings carry `rerun`, the
    (profile, model, thinking) a rerun should measure, or None when no rerun is
    suggested."""
    findings = []
    current = engine_snapshot(config_dir)
    refs = references(h)
    if not refs:
        return [{"kind": "no_reference", "rerun": None,
                 "text": "No reference run exists yet, so there is nothing to compare against."}]
    measured = {r["profile"] for r in refs}
    prompts_sha = file_sha(prompts_path)
    watched_models = {mt(p)[0] for p in current.values() if mt(p)[0]}
    latest_snap = max(refs, key=lambda r: r["registered"]).get("engine") or {}
    for ref in refs:
        prof = ref["profile"]
        now, was_measured, snap = mt(current.get(prof)), (ref["model"], ref.get("thinking_level")), \
            mt((ref.get("engine") or {}).get(prof))
        if now != was_measured:
            if now == snap:
                # engine.json hasn't moved since the reference was filed: the
                # reference measured a model the app isn't on yet (Nov 22: the
                # switch to Hydrogen / Neon was sent but not installed)
                findings.append({
                    "kind": "pending", "profile": prof, "ref": ref["id"], "rerun": None,
                    "text": f"{prof}: engine.json still says {fmt(now)}, but the reference measured "
                            f"{fmt(was_measured)}. Unchanged since the reference was filed, so the app "
                            "switch probably isn't installed yet. No rerun suggested."})
            else:
                findings.append({
                    "kind": "engine", "profile": prof, "ref": ref["id"], "rerun": (prof,) + now,
                    "text": f"{prof}: Rivia now uses {fmt(now)}; the reference measured {fmt(was_measured)}."})
        si = B.assemble_system_instruction(config_dir, ref["channel"])
        if sha(si) != ref["system_sha"]:
            findings.append({"kind": "system", "ref": ref["id"], "rerun": None,
                             "text": f"Rivia's {ref['channel']} system instruction changed "
                                     f"({ref['system_chars']} -> {len(si)} chars). That isn't model drift, but "
                                     "the rivia pass can't be compared with the old reference any more."})
        if prompts_sha != ref["prompt_set_sha"]:
            findings.append({"kind": "prompts", "ref": ref["id"], "rerun": None,
                             "text": "prompts.json differs from the one the reference used: runs won't compare."})
        if changelog:
            # her measured model, and whatever the profile runs now
            models = {ref["model"]} | ({now[0]} if now[0] else set())
            for hit in changelog_hits(changelog, models, ref["date"]):
                target = now if now[0] in hit["models"] and now != snap else was_measured
                findings.append({"kind": "changelog", "profile": prof, "ref": ref["id"], **hit,
                                 "rerun": (prof,) + target,
                                 "text": f"{prof}: Google changelog {hit['date']} names {', '.join(hit['models'])}: "
                                         f"\"{hit['line']}\""})
    # profiles nobody measured (fast, live, and later the router's classifier)
    for name in sorted(current):
        if name in measured or name not in latest_snap:
            continue
        if mt(current[name]) != mt(latest_snap[name]):
            findings.append({"kind": "unmeasured", "profile": name, "rerun": None,
                             "text": f"{name}: {fmt(mt(latest_snap[name]))} -> {fmt(mt(current[name]))}. "
                                     "This profile has no reference run, so drift there isn't checked."})
    seen, uniq = set(), []
    for f in findings:
        if f["text"] not in seen:
            seen.add(f["text"])
            uniq.append(f)
    return uniq


def rerun_targets(findings: list[dict], h: History) -> list[dict]:
    """One rerun per profile, deduplicated: {profile, model, thinking, ref}."""
    out = {}
    for f in findings:
        if f.get("rerun"):
            prof, model, thinking = f["rerun"]
            out.setdefault(prof, {"profile": prof, "model": model, "thinking": thinking,
                                  "ref": h.data["reference"][prof + "/text"]
                                  if prof + "/text" in h.data["reference"] else f["ref"]})
    return [out[k] for k in sorted(out)]


def rerun_plan(targets: list[dict], h: History, prompts_n: int, config_hint: str) -> list[str]:
    lines, total = [], 0
    for t in targets:
        ref = h.get(t["ref"])
        calls = prompts_n * ref["reps"] * len(PASSES) * 2 + prompts_n
        total += calls
        price = B.price_for(t["model"])
        est = B.estimate(prompts_n * ref["reps"] * len(PASSES), t["model"], ref["rater_model"])
        cost = f", about ${est['total']:.2f} at 2.6's token guess" if est else (
            "" if price else ", price unknown for this model")
        lines.append(f"- {t['profile']}: {t['model']} at {t['thinking']}, {calls} calls{cost}")
    lines.insert(0, f"Rerun ({total} Gemini calls in all; each run stops at 600 calls and paces at 10 a minute):")
    lines += ["Only on Jordan's yes:",
              f"  CONFIG={config_hint} LEXICON=<your NRC file> GEMINI_API_KEY=... sh rerun.sh",
              "rerun.sh measures exactly the list above (it reads it from `drift.py targets`)."]
    return lines


def fetch_changelog() -> str:
    """Public docs page, not the Gemini API: costs nothing. Opt-in."""
    req = urllib.request.Request(CHANGELOG_URL, headers={"User-Agent": "rivia-drift-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return changelog_text_from_html(resp.read().decode("utf-8", "replace"))


def _changelog_from_args(args) -> str | None:
    if args.changelog_file:
        raw = Path(args.changelog_file).read_text()
        return changelog_text_from_html(raw) if "<" in raw[:2000] else raw
    if args.fetch_changelog:
        return fetch_changelog()
    return None


def cmd_watch(args):
    h = History(Path(args.history))
    findings = watch(Path(args.config), h, _changelog_from_args(args), Path(args.prompts))
    state_path = Path(args.history) / "watch_state.json"
    sig = sha(json.dumps(sorted(f["text"] for f in findings)))
    if args.new_only and state_path.exists() and json.loads(state_path.read_text()).get("sig") == sig:
        return 0
    Path(args.history).mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"sig": sig, "at": datetime.now().isoformat(timespec="seconds"),
                                      "findings": findings}, indent=2))
    real = [f for f in findings if f["kind"] not in ("no_reference",)]
    if not real:
        print("\n".join(f["text"] for f in findings) or "No change since the reference runs.")
        return 0
    targets = rerun_targets(real, h)
    print("Rivia drift check: a model change may affect her. Nothing has been run or changed." if targets
          else "Rivia drift check: no rerun needed. For the record:")
    for f in real:
        print("- " + f["text"])
    if targets:
        for line in rerun_plan(targets, h, len(B.load_prompts(Path(args.prompts))), args.config):
            print(line)
        return 3
    return 0


def cmd_targets(args):
    """Machine-readable list for rerun.sh: profile model thinking reference_id."""
    h = History(Path(args.history))
    findings = watch(Path(args.config), h, _changelog_from_args(args), Path(args.prompts))
    for t in rerun_targets(findings, h):
        print(t["profile"], t["model"], t["thinking"], t["ref"])
    return 0



# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--history", default=str(HERE / "history"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register")
    p.add_argument("--run", required=True)
    p.add_argument("--profile", help="engine profile the run measured (balanced, deep...); inferred from engine.json if it matches")
    p.add_argument("--config", help="app assets/config folder, to snapshot engine.json")
    p.add_argument("--prompts", default=str(BASELINE_DIR / "prompts.json"))
    p.add_argument("--rater-model", default="gemini-3.8-flash")
    p.add_argument("--rater-thinking", default="low")
    p.add_argument("--lexicon", help="the NRC file used for scoring (hashed, never copied)")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("set-reference")
    p.add_argument("--id", required=True)
    p.add_argument("--why")
    p.set_defaults(fn=cmd_set_reference)

    p = sub.add_parser("anchor")
    p.add_argument("--ref", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--rater-model", default="gemini-3.8-flash")
    p.add_argument("--rater-thinking", default="low")
    p.add_argument("--confirm-spend", action="store_true")
    p.add_argument("--rpm", type=float, default=10, help="max requests per minute (default 10, as 2.6)")
    p.add_argument("--max-calls", type=int, default=600, help="call cap for the run folder (default 600, as 2.6)")
    p.add_argument("--max-cost", type=float, default=None, help="optional cost cap, USD (default none, as 2.6)")
    p.add_argument("--price-in", type=float)
    p.add_argument("--price-out", type=float)
    p.set_defaults(fn=cmd_anchor)

    p = sub.add_parser("compare")
    p.add_argument("--new", required=True)
    p.add_argument("--ref")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("selfcheck")
    p.add_argument("--id", required=True)
    p.set_defaults(fn=cmd_selfcheck)

    p = sub.add_parser("watch")
    p.add_argument("--config", required=True)
    p.add_argument("--prompts", default=str(BASELINE_DIR / "prompts.json"))
    p.add_argument("--changelog-file")
    p.add_argument("--fetch-changelog", action="store_true")
    p.add_argument("--new-only", action="store_true", help="print nothing if the findings were already flagged")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("targets", help="for rerun.sh: what a rerun should measure, one line per profile")
    p.add_argument("--config", required=True)
    p.add_argument("--prompts", default=str(BASELINE_DIR / "prompts.json"))
    p.add_argument("--changelog-file")
    p.add_argument("--fetch-changelog", action="store_true")
    p.set_defaults(fn=cmd_targets)

    args = ap.parse_args(argv)
    rc = args.fn(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
