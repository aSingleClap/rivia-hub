# rivia_inject.py: the injection line for Rivia's emotion state (emotion spec, Step 1.6).
#
# Turns the current V, A, D point into words, never raw numbers:
#   "mood: annoyance, intensity 2 of 4"
# The label and the intensity come from rivia_emotion.label() (1.4); this file only phrases them. The word is
# label()'s name, the same one GET /app/emotion shows on the dial. How she should sound (style) is Step 8's
# job, so the line carries no style.
#
# LIVE (Jordan, Nov 26: "Send #4 to rivia-server", Layer 1 live). SHADOW is False, so for_gemini() returns the
# line, and the app fetches it from GET /app/emotion/line (rivia_emotion_app) before each Gemini turn. Setting
# SHADOW back to True is the one-line way back to shadow: the line is then still built and logged, but "" goes
# out. No tool or door can change SHADOW at runtime. Layer 1's switch (l1 in rivia_toggles) also silences it:
# with l1 off, for_gemini() returns "".
#
# Rules:
#   - The only function a Gemini-feeding caller may use is for_gemini(). build_line() is for logs,
#     the admin view and tests; never pass its result into a prompt.
#   - for_gemini() never raises: any failure (no DB, bad row, log write fails) returns "", because
#     emotion colors how she speaks, never how she works.
#   - Reads only. Never changes the state; the one writer stays rivia_emotion._apply().
#   - The log (inject_log in emotion.db) is separate from the change log (1.7). Each for_gemini() call writes
#     one row: when, her numbers and label, the line that went out, and the shadow flag. Never any of Jordan's
#     words: nothing here ever sees them.
import rivia_emotion as emo
import rivia_store as store
import rivia_time as rt

SHADOW = False  # False: for_gemini() returns the line (live). True: build and log only. Edit here only.

# Step 2.3: which layer the label and this line read. Step 2 has three layers (emotion, mood, baseline);
# the line reads the EMOTION layer with decay applied, i.e. whatever rivia_emotion.read() returns. Emotion
# decays toward mood and mood toward baseline (2.2), so a fresh spike shows first, the lingering mood shows
# once the spike fades, and the baseline shows once the mood fades too. Mood and baseline are never read
# here directly: reading mood would hide spikes, reading baseline would hide both. GET /app/emotion
# (rivia_emotion_app._view) reads the same thing, so the app's dial and this line always agree.
# Jordan (Nov 21): her normal counts as neutral. The baseline is passed to label() as rest, so within
# NEUTRAL_BELOW of her baseline the line is empty even when the baseline isn't 0, 0, 0.
LABEL_LAYER = "emotion"
REST_LAYER = "baseline"

LOG_SCHEMA = """
create table if not exists inject_log(
    id integer primary key, at text not null, v real not null, a real not null, d real not null,
    name text not null, intensity text not null, line text not null, shadow integer not null);
"""

# Neutral injects nothing: an empty line means "no emotional colouring", so Gemini gets no hint at all.
NEUTRAL_LINE = ""
LINE_FORMAT = "mood: %s, intensity %d of 4"
LEVEL_MAX = 4

# label() intensity word -> the line's "intensity N of 4".
INTENSITY = {
    "faint": 1,
    "mild": 2,
    "moderate": 3,
    "strong": 4,
}


def build_line(v, a, d, *, rest=None):
    """The words for a V, A, D point: {"line", "name", "intensity", "level"}.
    Near 0, or near rest (her baseline, 2.3), the line is NEUTRAL_LINE and level is 0. Pure; reads nothing,
    writes nothing. Never feed the result to Gemini; for_gemini() is the only door for that."""
    lab = emo.label(v, a, d, rest=rest)
    name = lab["name"]
    if name == "neutral":
        return {"line": NEUTRAL_LINE, "name": "neutral", "intensity": "neutral", "level": 0}
    level = INTENSITY[lab["intensity"]]
    return {"line": LINE_FORMAT % (name, level), "name": name, "intensity": lab["intensity"], "level": level}


def _log(built, state, shadow, line=None):
    """One inject_log row. `line` is what went out (for_gemini); None logs the built line (current())."""
    con = store.connect("emotion", LOG_SCHEMA)
    try:
        con.execute("insert into inject_log(at, v, a, d, name, intensity, line, shadow) values(?,?,?,?,?,?,?,?)",
                    (rt.iso(rt.now_s()), state["v"], state["a"], state["d"], built["name"], built["intensity"],
                     built["line"] if line is None else line, 1 if shadow else 0))
    finally:
        con.close()


def _build(con=None):
    layers = emo.read_all(con)  # decayed (2.2); the label names LABEL_LAYER, measured against REST_LAYER (2.3)
    state, base = layers[LABEL_LAYER], layers[REST_LAYER]
    return state, build_line(state["v"], state["a"], state["d"], rest=(base["v"], base["a"], base["d"]))


def layer_on():
    """Layer 1's switch (l1). Unreadable switches fall back to the code's defaults (on), as rivia_toggles does."""
    import rivia_toggles as toggles
    return toggles.runs("l1")


def current(con=None):
    """Build the line for her current state (the decayed emotion layer) and log it. Returns build_line()'s
    dict plus "shadow" and "layer".
    For logs and the admin view; not for prompts. May raise (unlike for_gemini)."""
    state, built = _build(con)
    built["shadow"] = SHADOW
    built["layer"] = LABEL_LAYER
    _log(built, state, SHADOW)
    return built


def for_gemini(con=None):
    """The one call a prompt builder may make. Builds the line and logs one row every time; returns it only
    when SHADOW is off and Layer 1 is on. Neutral, shadow, Layer 1 off, and any error all return "" (inject
    nothing). The logged line is what was returned."""
    try:
        state, built = _build(con)
        line = built["line"] if (not SHADOW and layer_on()) else ""
        _log(built, state, SHADOW, line)
        return line
    except Exception:
        return ""


def recent(n=20):
    """The last n logged lines, newest first: [{"at", "v", "a", "d", "name", "intensity", "line", "shadow"}]."""
    con = store.connect("emotion", LOG_SCHEMA)
    try:
        rows = con.execute("select at, v, a, d, name, intensity, line, shadow from inject_log "
                           "order by id desc limit ?", (int(n),)).fetchall()
    finally:
        con.close()
    keys = ("at", "v", "a", "d", "name", "intensity", "line", "shadow")
    return [dict(zip(keys, r[:7] + (bool(r[7]),))) for r in rows]
