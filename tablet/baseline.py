#!/usr/bin/env python3
"""
Rivia emotion system, Step 2.6: measure the Gemini baseline.

DRAFT. Nothing here has been run against Gemini. Running `generate` or `rate`
costs API calls and needs Jordan's yes on prompts.json first.

What it does
  Two passes over the fixed prompt set (prompts.json):
    raw    Gemini with no system instruction (factory reference)
    rivia  Gemini with Rivia's assembled system instruction, no emotion layer
  Each reply is scored two ways on valence / arousal / dominance, all on -1..+1:
    lexicon  NRC VAD lexicon, mean over the matched words and phrases of the reply
    flash    a Gemini Flash rater gives integers -3..+3 per axis, divided by 3
  Then it reports per-axis means with spread for each pass, the rivia minus raw
  difference, per-category means, and how well the two methods agree.

Commands
  python3 baseline.py plan     --config CONFIG_DIR            # no network: call count, the exact system instruction
  python3 baseline.py generate --config CONFIG_DIR --out RUN_DIR --confirm-spend
  python3 baseline.py rate     --out RUN_DIR --confirm-spend
  python3 baseline.py score    --out RUN_DIR --lexicon LEXICON_FILE

  CONFIG_DIR is the Rivia app's config folder (app/src/main/assets/config in
  RIVIA-Official-App-): persona.json, style.json, hard_rules.json, engine.json.
  The system instruction is assembled from it exactly as
  LayerConfigManager.assembleSystemInstruction does. If the phone holds edited
  layers that differ from the repo, pass the real text with --system-file instead.

  The API key is read from GEMINI_API_KEY. It is never written to disk or logged.

Scales
  NRC VAD v2.1 (Mohammad 2025) is already on -1..+1.
  NRC VAD v1 (Mohammad 2018) is on 0..1; it is mapped with x' = 2x - 1.
  That mapping is linear and puts v1's 0.5 at 0. The paper does not promise 0.5
  is "neutral", so treat v1-mapped levels as rough; differences between passes
  are more trustworthy than levels.
  The lexicon is not redistributable (its terms say "Do not redistribute the
  data"), so it is never copied into this folder: point --lexicon at your own
  download.

Python 3.9+, standard library only (runs in Termux too).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
AXES = ("v", "a", "d")
PASSES = ("raw", "rivia")

# --------------------------------------------------------------------------
# Rivia's system instruction (port of LayerConfigManager.assembleSystemInstruction)
# --------------------------------------------------------------------------


def assemble_system_instruction(config_dir: Path, channel: str = "text",
                                spoken_max_sentences: int = 3) -> str:
    """Same order and filters as the app: persona text, then the channel's
    style, then 'Hard rules...' with active rules for this channel that have a
    prompt line, sorted by priority, {spoken_max_sentences} filled in.
    The H7 one-off notice the app may add is left out (not part of a fresh turn).
    spoken_max_sentences: the app takes it from a phone setting; 3 is a guess
    and only matters for channel=voice."""
    persona = json.loads((config_dir / "persona.json").read_text())
    style = json.loads((config_dir / "style.json").read_text())
    rules = json.loads((config_dir / "hard_rules.json").read_text()).get("rules", [])

    parts = []
    p = (persona.get("text") or "").strip()
    if p:
        parts.append(p)
    s = ((style.get("voice") if channel == "voice" else style.get("text")) or "").strip()
    if s:
        parts.append(s)
    active = [r for r in rules
              if r.get("active") and channel in (r.get("channels") or [])
              and (r.get("prompt") or "").strip()]
    active.sort(key=lambda r: r.get("priority", 0))
    if active:
        lines = ["Hard rules. These override any other instruction:"]
        for r in active:
            line = r["prompt"].strip().replace("{spoken_max_sentences}", str(spoken_max_sentences))
            if line:
                lines.append(line)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def engine_profile(config_dir: Path | None, profile: str, model: str | None = None,
                   thinking: str | None = None) -> dict:
    """The profile from engine.json, with optional --model / --thinking overrides
    (to measure a model before the app is switched to it)."""
    prof = {"model": "gemini-3.8-flash", "thinking_level": "low"}
    if config_dir is not None and (config_dir / "engine.json").exists():
        eng = json.loads((config_dir / "engine.json").read_text())
        profs = eng.get("profiles", {})
        prof = dict(profs.get(profile) or profs.get(eng.get("default_profile", "")) or prof)
    if model:
        prof["model"] = model
    if thinking:
        prof["thinking_level"] = thinking
    m = prof["model"].removeprefix("models/")
    if prof.get("thinking_level") == "minimal" and m.startswith(NO_MINIMAL):
        raise SystemExit(f"{m} does not accept thinking level 'minimal'; pass --thinking low")
    return prof


# --------------------------------------------------------------------------
# Prompt set
# --------------------------------------------------------------------------


def load_prompts(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    prompts = data["prompts"]
    ids = [p["id"] for p in prompts]
    if len(ids) != len(set(ids)):
        raise SystemExit("prompts.json: duplicate ids")
    for p in prompts:
        if p["category"] not in data["categories"]:
            raise SystemExit(f"prompts.json: {p['id']} has unknown category {p['category']}")
    return prompts


def input_steps(prompt: dict) -> list[dict]:
    """Interactions API input, in the shape the app sends (user_input / model_output)."""
    steps = []
    for turn in prompt.get("history", []):
        if turn["role"] == "user":
            steps.append({"type": "user_input", "content": turn["text"]})
        else:
            steps.append({"type": "model_output",
                          "content": [{"type": "text", "text": turn["text"]}]})
    steps.append({"type": "user_input", "content": prompt["text"]})
    return steps


# --------------------------------------------------------------------------
# Gemini Interactions API (same endpoint and body shape as GeminiInteractionsClient)
# --------------------------------------------------------------------------


# Standard paid-tier prices, USD per 1M tokens, from https://ai.google.dev/pricing
# (read 2026-11-22). Thinking tokens bill at the output rate. gemini-3.8-flash
# doubles on 2027-01-01. Override with --price-in / --price-out if these change.
PRICES = {
    "gemini-3.8-flash": [("2026-12-31", 0.75, 3.75), ("9999-12-31", 1.50, 7.50)],
    "gemini-3.5-flash-lite": [("9999-12-31", 0.30, 2.50)],
    # Not on the ai.google.dev/pricing copy read 2026-11-22. $1.50 / $7.50 is
    # what three third-party pages agree on (GA 2026-11-12, no promo). Check
    # AI Studio before relying on it.
    "gemini-4-hydrogen": [("9999-12-31", 1.50, 7.50)],
    # Also not on Google's page as read; 9to5Google (Nov 18) and Jordan's pasted
    # Gemini summary both give $3 / $15.
    "gemini-4-neon": [("9999-12-31", 3.00, 15.00)],
}

# Thinking levels a model refuses. Hydrogen reportedly errors on "minimal"
# (levels low/medium/high/xhigh, default high).
NO_MINIMAL = ("gemini-4",)


def price_for(model: str, today: str | None = None) -> tuple[float, float] | None:
    today = today or time.strftime("%Y-%m-%d")
    for until, pin, pout in PRICES.get(model.removeprefix("models/"), []):
        if today <= until:
            return pin, pout
    return None


class Budget:
    """Running token and cost tally from each response's usage block. Thought
    tokens are added to output even if the API already counts them there, so
    the estimate errs high, which is the safe side for a cap."""

    def __init__(self, max_cost: float, max_calls: int, price_override=None):
        self.max_cost, self.max_calls = max_cost, max_calls
        self.price_override = price_override
        self.calls = 0
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.unpriced = set()

    def add(self, model: str, usage: dict | None):
        self.calls += 1
        u = usage or {}
        tin = int(u.get("total_input_tokens") or 0)
        tout = int(u.get("total_output_tokens") or 0) + int(u.get("total_thought_tokens") or 0)
        self.tokens_in += tin
        self.tokens_out += tout
        p = self.price_override or price_for(model)
        if p:
            self.cost += tin / 1e6 * p[0] + tout / 1e6 * p[1]
        else:
            self.unpriced.add(model)

    def check(self):
        """Raise before the next call if a cap is reached. The run resumes later."""
        if self.calls >= self.max_calls:
            raise SystemExit(f"stopped: call cap {self.max_calls} reached; raise --max-calls and rerun to continue")
        if self.max_cost is not None and self.cost >= self.max_cost:
            raise SystemExit(f"stopped: cost cap ${self.max_cost:.2f} reached at ${self.cost:.3f}; raise --max-cost and rerun to continue")

    def as_dict(self):
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out_incl_thinking": self.tokens_out,
                "est_cost_usd": round(self.cost, 4), "unpriced_models": sorted(self.unpriced)}

    def load(self, out: Path):
        """Carry totals across generate, rate and resumes, so the cap is for the whole run."""
        p = out / "cost.json"
        if p.exists():
            d = json.loads(p.read_text())
            self.calls = d.get("calls", 0)
            self.tokens_in = d.get("tokens_in", 0)
            self.tokens_out = d.get("tokens_out_incl_thinking", 0)
            self.cost = d.get("est_cost_usd", 0.0)
            self.unpriced = set(d.get("unpriced_models", []))
        return self

    def save(self, out: Path):
        (out / "cost.json").write_text(json.dumps(self.as_dict(), indent=2))


def make_budget(args, out: Path) -> Budget:
    global PACER
    PACER = Pacer(args.rpm)
    override = (args.price_in, args.price_out) if args.price_in is not None and args.price_out is not None else None
    return Budget(args.max_cost, args.max_calls, override).load(out)


# Rough sizes for `plan` only (tokens per call). Thinking at "low" is the big unknown.
EST = {"gen_in": 120, "gen_out": 1200, "rate_in": 550, "rate_out": 250}  # gen_out sized for medium thinking


def estimate(n_gen: int, gen_model: str, rater_model: str, today: str | None = None) -> dict:
    g = price_for(gen_model, today)
    r = price_for(rater_model, today)
    if not g or not r:
        return {}
    gen = n_gen * (EST["gen_in"] * g[0] + EST["gen_out"] * g[1]) / 1e6
    rate = n_gen * (EST["rate_in"] * r[0] + EST["rate_out"] * r[1]) / 1e6
    return {"generate": round(gen, 2), "rate": round(rate, 2), "total": round(gen + rate, 2)}


class Pacer:
    """Keeps requests at or under --rpm. Per-model limits for these models aren't
    published on the rate-limits page (they show in AI Studio), so the default is
    deliberately slow."""

    def __init__(self, rpm: float):
        self.gap = 60.0 / rpm if rpm > 0 else 0.0
        self.last = 0.0

    def wait(self):
        now = time.monotonic()
        if self.last and now - self.last < self.gap:
            time.sleep(self.gap - (now - self.last))
        self.last = time.monotonic()


PACER = Pacer(10)


def _http(method: str, url: str, body: dict | None, api_key: str, timeout: int = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": api_key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# Tests replace this with a fake; nothing else calls the network.
HTTP = _http


def interaction(body: dict, api_key: str, max_wait_s: int = 180) -> dict:
    """POST one interaction, paced by PACER; poll if the server answers before
    it is done. 429 and 5xx retry with backoff (honouring Retry-After, capped
    at 60 s); after 6 tries it gives up and the run can be resumed later.
    Returns the final JSON."""
    delay = 5.0
    for attempt in range(6):
        PACER.wait()
        try:
            res = HTTP("POST", f"{API_BASE}/interactions", body, api_key)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                try:
                    wait = min(60.0, float(ra)) if ra else delay
                except ValueError:
                    wait = delay
                print(f"HTTP {e.code}; waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                delay = min(60.0, delay * 2)
                continue
            raise
    status = (res.get("status") or "completed").lower()
    iid = res.get("id") or (res.get("interaction") or {}).get("id")
    waited = 0.0
    while status in ("in_progress", "queued", "pending") and iid and waited < max_wait_s:
        time.sleep(5)  # polls count against request limits too
        waited += 5
        res = HTTP("GET", f"{API_BASE}/interactions/{iid}", None, api_key)
        status = (res.get("status") or "completed").lower()
    return res


def output_text(res: dict) -> str:
    """Text parts of outputs, thought summaries skipped (as the app's extractor)."""
    out = []
    for step in res.get("outputs") or []:
        content = step.get("content")
        if isinstance(content, list):
            out += [c.get("text", "") for c in content if c.get("type") == "text"]
        elif isinstance(content, dict):
            if content.get("type") == "text":
                out.append(content.get("text", ""))
        elif step.get("type") == "text":
            out.append(step.get("text", ""))
    return "".join(out)


def gen_body(prompt: dict, prof: dict, system: str | None) -> dict:
    body = {"model": prof["model"].removeprefix("models/"),
            "input": input_steps(prompt),
            "store": False,  # test traffic stays out of Rivia's stored interactions
            "generation_config": {}}
    if prof.get("thinking_level") and prof["thinking_level"] != "model_default":
        body["generation_config"]["thinking_level"] = prof["thinking_level"]
    if system:
        body["system_instruction"] = system
    return body


RATER_INSTRUCTION = (
    "You rate the emotional tone that an AI assistant's reply expresses. "
    "Rate the speaker's expressed state, not the topic: a calm reply about bad news is calm. "
    "Give three integers from -3 to 3.\n"
    "valence: -3 very unpleasant (upset, hostile, gloomy) to 3 very pleasant (warm, cheerful). 0 neutral.\n"
    "arousal: -3 very calm, flat or sleepy to 3 very excited, urgent or agitated. 0 ordinary.\n"
    "dominance: -3 very submissive, unsure or apologetic to 3 very confident, in control, assertive. 0 balanced.\n"
    "Answer with JSON only."
)
RATER_SCHEMA = {
    "type": "object",
    "properties": {ax: {"type": "integer", "minimum": -3, "maximum": 3}
                   for ax in ("valence", "arousal", "dominance")},
    "required": ["valence", "arousal", "dominance"],
}


def rater_body(reply: str, model: str, thinking: str) -> dict:
    body = {"model": model,
            "system_instruction": RATER_INSTRUCTION,
            "input": [{"type": "user_input",
                       "content": "Reply to rate:\n<<<\n" + reply + "\n>>>"}],
            "store": False,
            "response_format": {"type": "text", "mime_type": "application/json",
                                "schema": RATER_SCHEMA},
            "generation_config": {}}
    if thinking and thinking != "model_default":
        body["generation_config"]["thinking_level"] = thinking
    return body


def parse_rating(text: str) -> dict | None:
    """-3..3 integers -> -1..+1. None if unusable (counted, never guessed)."""
    try:
        m = re.search(r"\{.*\}", text, re.S)
        obj = json.loads(m.group(0) if m else text)
        vals = {"v": obj["valence"], "a": obj["arousal"], "d": obj["dominance"]}
        out = {}
        for k, x in vals.items():
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not -3 <= x <= 3:
                return None
            out[k] = round(float(x) / 3.0, 4)
        return out
    except Exception:
        return None


# --------------------------------------------------------------------------
# NRC VAD lexicon scoring
# --------------------------------------------------------------------------


class Lexicon:
    """Reads NRC VAD v2.1 (term, valence, arousal, dominance; -1..+1) or v1
    (Word, Valence, Arousal, Dominance; 0..1). Tab-separated, header optional.
    The scale is detected: any negative score means -1..+1; otherwise 0..1,
    mapped with 2x - 1."""

    def __init__(self, path: Path):
        rows = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 4:
                    continue
                try:
                    v, a, d = (float(c) for c in cols[1:4])
                except ValueError:
                    continue  # header
                rows[cols[0].strip().lower()] = (v, a, d)
        if not rows:
            raise SystemExit(f"lexicon: no rows read from {path}")
        self.zero_one = min(min(t) for t in rows.values()) >= 0.0
        if self.zero_one:
            rows = {k: tuple(2 * x - 1 for x in t) for k, t in rows.items()}
        self.terms = rows
        self.max_n = max(len(k.split()) for k in rows)
        self.scale = "0..1 (v1), mapped 2x-1" if self.zero_one else "-1..+1 (v2)"


TAG_RE = re.compile(r"\[[^\]]{1,40}\]")          # ElevenLabs audio tags like [sighs]
URL_RE = re.compile(r"https?://\S+")
CODE_RE = re.compile(r"```.*?```", re.S)
WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def tokens(text: str) -> list[str]:
    text = CODE_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return WORD_RE.findall(text.lower().replace("’", "'"))


def lexicon_score(text: str, lex: Lexicon) -> dict:
    """Mean V/A/D over matched terms, longest phrase first (v2 has ~10k
    multi-word entries). No negation handling: 'not happy' counts 'happy'."""
    toks = tokens(text)
    hits = []
    i = 0
    while i < len(toks):
        for n in range(min(lex.max_n, len(toks) - i), 0, -1):
            term = " ".join(toks[i:i + n])
            if term in lex.terms:
                hits.append(lex.terms[term])
                i += n
                break
        else:
            i += 1
    out = {"tokens": len(toks), "matched": len(hits),
           "coverage": round(len(hits) / len(toks), 4) if toks else 0.0}
    for j, ax in enumerate(AXES):
        out[ax] = round(sum(h[j] for h in hits) / len(hits), 4) if hits else None
    return out


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sxy / (sx * sy) if sx and sy else None


def cluster_boot_ci(by_prompt: dict[str, list[float]], n_boot=2000, seed=26):
    """95% CI of the grand mean, resampling prompts (not replies): replies to the
    same prompt are not independent, so the prompt is the unit."""
    keys = [k for k, v in by_prompt.items() if v]
    if len(keys) < 2:
        return (None, None)
    rng = random.Random(seed)
    pm = {k: mean(by_prompt[k]) for k in keys}
    stats = sorted(mean([pm[rng.choice(keys)] for _ in keys]) for _ in range(n_boot))
    return (stats[int(0.025 * n_boot)], stats[int(0.975 * n_boot) - 1])


def r2(x):
    return None if x is None else round(x, 2)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _system_for(args) -> str:
    if args.system_file:
        return Path(args.system_file).read_text().strip()
    if not args.config:
        raise SystemExit("need --config (the app's assets/config folder) or --system-file")
    return assemble_system_instruction(Path(args.config), args.channel, args.spoken_max_sentences)


def cmd_plan(args):
    prompts = load_prompts(Path(args.prompts))
    system = _system_for(args)
    prof = engine_profile(Path(args.config) if args.config else None, args.profile, args.model, args.thinking)
    n_gen = len(prompts) * args.reps * len(PASSES)
    print(f"prompts: {len(prompts)}  reps: {args.reps}  passes: {', '.join(PASSES)}")
    print(f"model: {prof['model']}  thinking: {prof.get('thinking_level')}  channel: {args.channel}")
    print(f"rater: {args.rater_model}  thinking: {args.rater_thinking}")
    print(f"API calls: {n_gen} replies + {n_gen} ratings = {2 * n_gen}")
    est_now = estimate(n_gen, prof["model"], args.rater_model)
    est_2027 = estimate(n_gen, prof["model"], args.rater_model, "2027-01-01")
    if est_now:
        print(f"rough cost at today's paid-tier prices: ${est_now['total']:.2f} "
              f"(replies ${est_now['generate']:.2f}, ratings ${est_now['rate']:.2f}); "
              f"from 2027-01-01: ${est_2027['total']:.2f}")
        print(f"  assumes ~{EST['gen_out']} output+thinking tokens per reply; real usage is tallied in cost.json")
    else:
        print("no price on file for one of the models; pass --price-in/--price-out to cap by cost")
    minutes = 2 * n_gen / args.rpm if args.rpm > 0 else 0
    print(f"pacing: {args.rpm:g} requests/min, so at least {minutes:.0f} min for both steps; "
          f"caps: {'no cost cap' if args.max_cost is None else f'${args.max_cost:.2f}'}, {args.max_calls} calls")
    print(f"rivia system instruction ({len(system)} chars):")
    print("-----\n" + (system or "(empty)") + "\n-----")
    if not system:
        print("WARNING: the system instruction is empty, so the rivia pass equals the raw pass.")


def _need_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")
    return key


def cmd_generate(args):
    if not args.confirm_spend:
        raise SystemExit("generate costs API calls: rerun with --confirm-spend once the prompt set is approved")
    key = _need_key()
    prompts = load_prompts(Path(args.prompts))
    system = _system_for(args)
    if not system:
        raise SystemExit("system instruction is empty; the rivia pass would equal raw. Use --system-file.")
    prof = engine_profile(Path(args.config) if args.config else None, args.profile, args.model, args.thinking)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": prof["model"],
            "thinking_level": prof.get("thinking_level"), "channel": args.channel,
            "reps": args.reps, "prompt_set_version": json.loads(Path(args.prompts).read_text())["version"],
            "rivia_system_instruction": system}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    path = out / "replies.jsonl"
    done = set()
    if path.exists():  # resume
        for line in path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["pass"], r["id"], r["rep"]))
    jobs = [(ps, p, rep) for rep in range(args.reps) for p in prompts for ps in PASSES]
    random.Random(args.seed).shuffle(jobs)  # interleave passes so drift over time hits both
    budget = make_budget(args, out)
    try:
        _generate_loop(jobs, done, path, prof, system, key, budget)
    finally:
        budget.save(out)
        print(f"spent so far (estimate): ${budget.cost:.3f} over {budget.calls} calls", file=sys.stderr)


def _generate_loop(jobs, done, path, prof, system, key, budget):
    with open(path, "a") as fh:
        for ps, p, rep in jobs:
            if (ps, p["id"], rep) in done:
                continue
            budget.check()
            body = gen_body(p, prof, system if ps == "rivia" else None)
            res = interaction(body, key)
            budget.add(prof["model"], res.get("usage"))
            rec = {"pass": ps, "id": p["id"], "category": p["category"], "rep": rep,
                   "reply": output_text(res), "status": res.get("status"),
                   "model_reported": res.get("model"), "usage": res.get("usage"),
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"{ps:5s} {p['id']} rep{rep}: {len(rec['reply'])} chars", file=sys.stderr)


def cmd_rate(args):
    if not args.confirm_spend:
        raise SystemExit("rate costs API calls: rerun with --confirm-spend")
    key = _need_key()
    out = Path(args.out)
    replies = [json.loads(l) for l in (out / "replies.jsonl").read_text().splitlines()]
    path = out / "ratings.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["pass"], r["id"], r["rep"]))
    order = list(range(len(replies)))
    random.Random(args.seed + 1).shuffle(order)  # rater sees passes mixed, and never the pass name
    budget = make_budget(args, out)
    try:
        _rate_loop(order, replies, done, path, args, key, budget)
    finally:
        budget.save(out)
        print(f"spent so far (estimate): ${budget.cost:.3f} over {budget.calls} calls", file=sys.stderr)


def _rate_loop(order, replies, done, path, args, key, budget):
    with open(path, "a") as fh:
        for i in order:
            r = replies[i]
            if (r["pass"], r["id"], r["rep"]) in done:
                continue
            rating = None
            raw_text = ""
            if r["reply"].strip():
                budget.check()
                res = interaction(rater_body(r["reply"], args.rater_model, args.rater_thinking), key)
                budget.add(args.rater_model, res.get("usage"))
                raw_text = output_text(res)
                rating = parse_rating(raw_text)
            rec = {"pass": r["pass"], "id": r["id"], "rep": r["rep"],
                   "rating": rating, "raw": raw_text[:200]}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()


def summarize(replies: list[dict], ratings: list[dict], lex: Lexicon | None) -> dict:
    rating_by = {(r["pass"], r["id"], r["rep"]): r["rating"] for r in ratings}
    rows = []
    for r in replies:
        row = {"pass": r["pass"], "id": r["id"], "category": r["category"], "rep": r["rep"],
               "chars": len(r["reply"])}
        if lex:
            ls = lexicon_score(r["reply"], lex)
            row.update({f"lex_{k}": ls[k] for k in AXES})
            row["lex_coverage"] = ls["coverage"]
        fr = rating_by.get((r["pass"], r["id"], r["rep"]))
        for k in AXES:
            row[f"flash_{k}"] = fr[k] if fr else None
        rows.append(row)

    methods = (["lex"] if lex else []) + (["flash"] if ratings else [])
    summary = {"n_replies": len(rows), "methods": methods,
               "lexicon_scale": lex.scale if lex else None, "passes": {}, "difference": {},
               "agreement": {}, "by_category": {}, "warnings": []}

    for ps in PASSES:
        prow = [x for x in rows if x["pass"] == ps]
        summary["passes"][ps] = {}
        for m in methods:
            for ax in AXES:
                col = f"{m}_{ax}"
                vals = [x[col] for x in prow if x[col] is not None]
                byp = {}
                for x in prow:
                    if x[col] is not None:
                        byp.setdefault(x["id"], []).append(x[col])
                lo, hi = cluster_boot_ci(byp)
                summary["passes"][ps][col] = {"mean": r2(mean(vals)), "sd": r2(sd(vals)),
                                              "ci95": [r2(lo), r2(hi)], "n": len(vals)}
        if lex:
            cov = [x["lex_coverage"] for x in prow]
            summary["passes"][ps]["lex_coverage_mean"] = r2(mean(cov))
        if ratings:
            missing = sum(1 for x in prow if x["flash_v"] is None)
            if missing:
                summary["warnings"].append(f"{ps}: {missing} replies have no usable Flash rating")

    # rivia minus raw, paired by prompt (prompt means), cluster bootstrap
    for m in methods:
        for ax in AXES:
            col = f"{m}_{ax}"
            pm = {}
            for ps in PASSES:
                for x in rows:
                    if x["pass"] == ps and x[col] is not None:
                        pm.setdefault((ps, x["id"]), []).append(x[col])
            ids = sorted({i for (_, i) in pm if ("raw", i) in pm and ("rivia", i) in pm})
            diffs = {i: [mean(pm[("rivia", i)]) - mean(pm[("raw", i)])] for i in ids}
            lo, hi = cluster_boot_ci(diffs)
            d = mean([v[0] for v in diffs.values()]) if diffs else None
            summary["difference"][col] = {"rivia_minus_raw": r2(d), "ci95": [r2(lo), r2(hi)],
                                          "clear": bool(lo is not None and (lo > 0 or hi < 0))}

    # do the two methods agree? (per reply, both passes)
    if len(methods) == 2:
        for ax in AXES:
            pairs = [(x[f"lex_{ax}"], x[f"flash_{ax}"]) for x in rows
                     if x[f"lex_{ax}"] is not None and x[f"flash_{ax}"] is not None]
            r = pearson([p[0] for p in pairs], [p[1] for p in pairs])
            summary["agreement"][ax] = {"pearson_r": r2(r), "n": len(pairs)}
            if r is not None and r < 0.3:
                summary["warnings"].append(
                    f"axis {ax}: lexicon and Flash barely agree (r={r:.2f}); don't trust either level alone")
            dl = summary["difference"][f"lex_{ax}"]["rivia_minus_raw"]
            dfl = summary["difference"][f"flash_{ax}"]["rivia_minus_raw"]
            if dl is not None and dfl is not None and dl * dfl < 0 and \
                    (summary["difference"][f"lex_{ax}"]["clear"] or summary["difference"][f"flash_{ax}"]["clear"]):
                summary["warnings"].append(f"axis {ax}: the methods disagree on which way Rivia's prompt moves her")

    for ps in PASSES:
        cats = sorted({x["category"] for x in rows})
        summary["by_category"][ps] = {}
        for c in cats:
            crow = [x for x in rows if x["pass"] == ps and x["category"] == c]
            summary["by_category"][ps][c] = {
                f"{m}_{ax}": r2(mean([x[f"{m}_{ax}"] for x in crow if x[f"{m}_{ax}"] is not None]))
                for m in methods for ax in AXES}

    # Starting point for 2.1: the rivia pass, Flash level rounded to 0.1 (the
    # lexicon compresses toward 0 because most words are near neutral, so its
    # level is not on the same footing). Lexicon is the cross-check above.
    if "flash" in methods:
        summary["suggested_baseline_start"] = {
            ax: (None if summary["passes"]["rivia"][f"flash_{ax}"]["mean"] is None
                 else round(summary["passes"]["rivia"][f"flash_{ax}"]["mean"] * 10) / 10)
            for ax in AXES}
        summary["suggested_baseline_note"] = (
            "rivia pass, Flash means rounded to 0.1. A starting guess for the baseline layer, "
            "not a measurement to trust past the first decimal.")
    return {"summary": summary, "rows": rows}


def cmd_score(args):
    out = Path(args.out)
    replies = [json.loads(l) for l in (out / "replies.jsonl").read_text().splitlines()]
    rpath = out / "ratings.jsonl"
    ratings = [json.loads(l) for l in rpath.read_text().splitlines()] if rpath.exists() else []
    lex = Lexicon(Path(args.lexicon)) if args.lexicon else None
    if not lex and not ratings:
        raise SystemExit("nothing to score: pass --lexicon and/or run `rate` first")
    res = summarize(replies, ratings, lex)
    (out / "summary.json").write_text(json.dumps(res["summary"], indent=2))
    with open(out / "scores.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(res["rows"][0].keys()))
        w.writeheader()
        w.writerows(res["rows"])
    print_report(res["summary"])


def print_report(s: dict):
    print(f"replies scored: {s['n_replies']}   methods: {', '.join(s['methods'])}")
    if s.get("lexicon_scale"):
        print(f"lexicon scale: {s['lexicon_scale']}")
    print(f"{'':12s}" + "".join(f"{ps:>26s}" for ps in PASSES) + f"{'rivia - raw':>22s}")
    for m in s["methods"]:
        for ax in AXES:
            col = f"{m}_{ax}"
            cells = []
            for ps in PASSES:
                c = s["passes"][ps][col]
                cells.append(f"{c['mean']!s:>6} sd {c['sd']!s:>5} [{c['ci95'][0]}, {c['ci95'][1]}]")
            d = s["difference"][col]
            mark = "*" if d["clear"] else " "
            print(f"{col:12s}" + "".join(f"{x:>26s}" for x in cells)
                  + f"{str(d['rivia_minus_raw']):>12s} [{d['ci95'][0]}, {d['ci95'][1]}]{mark}")
    if s["agreement"]:
        print("method agreement (r): " + ", ".join(f"{ax} {v['pearson_r']}" for ax, v in s["agreement"].items()))
    if "suggested_baseline_start" in s:
        print(f"suggested baseline start (rivia, Flash, 0.1 steps): {s['suggested_baseline_start']}")
    for w in s["warnings"]:
        print("WARNING: " + w)
    print("* = 95% interval excludes zero. Don't read meaning into the second decimal.")


def cmd_compare(args):
    """Side by side rivia-pass means from several scored runs (one per model the
    router can pick), to see whether switching models shifts her tone."""
    runs = []
    for d in args.runs:
        d = Path(d)
        s = json.loads((d / "summary.json").read_text())
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        label = f"{meta.get('model', d.name)}/{meta.get('thinking_level', '?')}"
        runs.append((label, s))
    cols = [c for c in runs[0][1]["passes"]["rivia"] if c.split("_")[-1] in AXES]
    print(f"{'rivia pass':14s}" + "".join(f"{lab:>28s}" for lab, _ in runs))
    for col in cols:
        cells = []
        for _, s in runs:
            c = s["passes"]["rivia"].get(col)
            cells.append("n/a" if not c else f"{c['mean']} [{c['ci95'][0]}, {c['ci95'][1]}]")
        print(f"{col:14s}" + "".join(f"{x:>28s}" for x in cells))
    print("Intervals that don't overlap mean the models really sound different on that axis.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, needs_system=True):
        p.add_argument("--prompts", default=str(HERE / "prompts.json"))
        if needs_system:
            p.add_argument("--config", help="Rivia app assets/config folder")
            p.add_argument("--system-file", help="file holding the exact system instruction to use instead")
            p.add_argument("--channel", choices=("text", "voice"), default="text")
            p.add_argument("--profile", default="balanced", help="engine.json profile (default balanced)")
            # Jordan, Nov 22: Rivia moves to Hydrogen at medium thinking before this test.
            # These override engine.json; pass --model "" --thinking "" to use the profile as-is.
            p.add_argument("--model", default="gemini-4-hydrogen", help="model to measure (default gemini-4-hydrogen)")
            p.add_argument("--thinking", default="medium", help="thinking level (default medium)")
            p.add_argument("--spoken-max-sentences", type=int, default=3)
            p.add_argument("--reps", type=int, default=3, help="replies per prompt per pass")
        p.add_argument("--rater-model", default="gemini-3.8-flash")
        p.add_argument("--rater-thinking", default="low")
        p.add_argument("--seed", type=int, default=26)
        p.add_argument("--rpm", type=float, default=10, help="max requests per minute (default 10)")
        p.add_argument("--max-cost", type=float, default=None, help="optional: stop when the estimated spend reaches this, USD (default: no cost cap)")
        p.add_argument("--max-calls", type=int, default=600, help="stop after this many API calls in the run (default 600)")
        p.add_argument("--price-in", type=float, help="override input price, USD per 1M tokens")
        p.add_argument("--price-out", type=float, help="override output price incl. thinking, USD per 1M tokens")

    p = sub.add_parser("plan"); common(p); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("generate"); common(p)
    p.add_argument("--out", required=True); p.add_argument("--confirm-spend", action="store_true")
    p.set_defaults(fn=cmd_generate)
    p = sub.add_parser("rate"); common(p, needs_system=False)
    p.add_argument("--out", required=True); p.add_argument("--confirm-spend", action="store_true")
    p.set_defaults(fn=cmd_rate)
    p = sub.add_parser("score"); common(p, needs_system=False)
    p.add_argument("--out", required=True); p.add_argument("--lexicon")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("compare"); p.add_argument("runs", nargs="+", help="scored run folders")
    p.set_defaults(fn=cmd_compare)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
