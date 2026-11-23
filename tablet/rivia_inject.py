# rivia_inject.py: the injection line for Rivia's emotion state (emotion spec, Step 1.6).
#
# Turns the current V, A, D point into words, never raw numbers, as the spec's injection format says:
#   "mildly angry, dry style, intensity 2"
# The word and the intensity come from rivia_emotion.label() (1.4); this file only phrases them.
#
# SHADOW MODE (default on): the line is built and logged every time, but for_gemini() returns ""
# so nothing reaches Gemini and her replies stay exactly the same (spec: "Done when ... her replies
# stay exactly the same"). Turning SHADOW off is a one-line change and a deliberate step after the
# 2-3 week shadow run; it is not something any tool or door can do at runtime.
#
# Rules:
#   - The only function a Gemini-feeding caller may use is for_gemini(). build_line() is for logs,
#     the admin view and tests; never pass its result into a prompt.
#   - for_gemini() never raises: any failure (no DB, bad row, log write fails) returns "", because
#     emotion colors how she speaks, never how she works.
#   - Reads only. Never changes the state; the one writer stays rivia_emotion._apply().
#   - The log (inject_log in emotion.db) is separate from the change log (1.7, not built here).
import rivia_emotion as emo
import rivia_store as store
import rivia_time as rt

SHADOW = True  # True: build and log only. False: for_gemini() returns the line. Edit here only.

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

# label() intensity word -> (adverb in the line, the spec's "intensity N").
INTENSITY = {
    "faint": ("faintly", 1),
    "mild": ("mildly", 2),
    "moderate": ("moderately", 3),
    "strong": ("strongly", 4),
}

# Reference-point name -> the adjective in the line. Plain words on purpose: a softer word for mild
# anger ("irritated", as in the spec's example) is Step 8's job, once there's data on how it reads.
ADJECTIVE = {
    "joy": "happy",
    "anger": "angry",
    "sadness": "sad",
    "surprise": "surprised",
    "annoyance": "annoyed",
    "contentment": "content",
    "relief": "relieved",
    "calm": "calm",
    "focused": "focused",
    "distressed": "distressed",  # in place of fear (Jordan, Nov 22: no fear; distressed is fine)
    "worried": "worried",        # Step 4.5 (Jordan, Nov 22: "Yes add worried"); plain style
}

# PLACEHOLDERS for Step 8's mood -> style map; not tuned, kept minimal. Anything unlisted is "plain".
# From the spec's starting map: irritated -> dry, good mood -> playful, focused -> crisp.
STYLE = {
    "anger": "dry",
    "annoyance": "dry",   # spec: irritated -> dry
    "joy": "playful",
    "contentment": "playful",
    "focused": "crisp",
}
DEFAULT_STYLE = "plain"


def build_line(v, a, d, *, rest=None):
    """The words for a V, A, D point: {"line", "name", "intensity", "level", "style"}.
    Near 0, or near rest (her baseline, 2.3), the line is NEUTRAL_LINE and level is 0. Pure; reads nothing,
    writes nothing. Never feed the result to Gemini; for_gemini() is the only door for that."""
    lab = emo.label(v, a, d, rest=rest)
    name = lab["name"]
    if name == "neutral":
        return {"line": NEUTRAL_LINE, "name": "neutral", "intensity": "neutral", "level": 0, "style": DEFAULT_STYLE}
    adverb, level = INTENSITY[lab["intensity"]]
    style = STYLE.get(name, DEFAULT_STYLE)
    line = "%s %s, %s style, intensity %d" % (adverb, ADJECTIVE[name], style, level)
    return {"line": line, "name": name, "intensity": lab["intensity"], "level": level, "style": style}


def _log(built, state, shadow):
    con = store.connect("emotion", LOG_SCHEMA)
    try:
        con.execute("insert into inject_log(at, v, a, d, name, intensity, line, shadow) values(?,?,?,?,?,?,?,?)",
                    (rt.iso(rt.now_s()), state["v"], state["a"], state["d"],
                     built["name"], built["intensity"], built["line"], 1 if shadow else 0))
    finally:
        con.close()


def current(con=None):
    """Build the line for her current state (the decayed emotion layer) and log it. Returns build_line()'s
    dict plus "shadow" and "layer".
    For logs and the admin view; not for prompts. May raise (unlike for_gemini)."""
    layers = emo.read_all(con)  # decayed (2.2); the label names LABEL_LAYER, measured against REST_LAYER (2.3)
    state, base = layers[LABEL_LAYER], layers[REST_LAYER]
    built = build_line(state["v"], state["a"], state["d"], rest=(base["v"], base["a"], base["d"]))
    built["shadow"] = SHADOW
    built["layer"] = LABEL_LAYER
    _log(built, state, SHADOW)
    return built


def for_gemini(con=None):
    """The one call a prompt builder may make. Builds and logs the line every time; returns it only
    when SHADOW is off. In shadow mode, and on any error, returns "" (inject nothing)."""
    try:
        built = current(con)
    except Exception:
        return ""
    return "" if SHADOW else built["line"]


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
