"""
rivia_bots.py: the tablet's side of "ask a Grok bot, get the answer later".

MCP only runs one way with Grok Bot: the Bot calls our tools. So a request reaches a bot
through its webhook routine (official, returns at once), and the bot answers by calling
our post_reply tool when it's done. Answers wait here, keyed by ticket, until the app
collects them with check_reply.

    app (Gemini) --ask_bot--> tablet --webhook--> bot's "Ask" routine
    bot --post_reply(ticket, json)--> tablet <--check_reply(ticket)-- app

Bots are set in ~/.rivia/.env:
    RIVIA_BOTS='ard'                                  names, comma-separated
    RIVIA_BOT_ARD_URL='https://api2.cursor.sh/...'    the Ask routine's webhook URL
    RIVIA_BOT_ARD_KEY='crsr_...'                      its key
    RIVIA_BOT_ARD_ABOUT='...'                         optional: what it's for (Gemini reads this)

Nothing here guesses: a failed send, a missing answer or an expired ticket comes back
as ok: false, and the reply is stored exactly as the bot sent it.
"""
import asyncio
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DB_PATH = os.environ.get("RIVIA_MAILBOX_DB", os.path.expanduser("~/rivia/mailbox.db"))
TICKET_TTL = int(os.environ.get("RIVIA_TICKET_TTL", 15 * 60))   # app gives up after this
KEEP_DAYS = 14                                                   # old tickets are deleted
MAX_REQUEST = 4000
MAX_REPLY = 64 * 1024
SEND_TIMEOUT = 15
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

DEFAULT_ABOUT = {
    "ard": "Scheduling: day plans, ride times between Jordan's places, leave-by times, "
           "writing a day to the Rivia calendar, backing up the ride database.",
}


def bots():
    """Configured bots: {name: {url, key, about}}. A bot without a URL and key is left out."""
    out = {}
    for name in (n.strip().lower() for n in os.environ.get("RIVIA_BOTS", "").split(",")):
        if not name:
            continue
        up = name.upper()
        url = os.environ.get("RIVIA_BOT_%s_URL" % up, "").strip()
        key = os.environ.get("RIVIA_BOT_%s_KEY" % up, "").strip()
        if url and key:
            about = os.environ.get("RIVIA_BOT_%s_ABOUT" % up, "").strip() or DEFAULT_ABOUT.get(name, "")
            out[name] = {"url": url, "key": key, "about": about}
    return out


def bot_list_text():
    b = bots()
    if not b:
        return "No bots are configured yet."
    return " ".join("%s: %s" % (n, v["about"] or "no description") for n, v in b.items())


def _db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.execute("pragma journal_mode=wal")
    c.execute("""create table if not exists tickets(
        ticket text primary key, bot text, request text, created real,
        status text, info text, reply text, replied real)""")
    c.execute("delete from tickets where created < ?", (time.time() - KEEP_DAYS * 86400,))
    return c


def _url_ok(url):
    u = urlparse(url)
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)


def _send(url, key, body):
    """POST the request to the bot's webhook. Returns (http status, short body)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "User-Agent": "rivia-tablet/1"})
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as r:
            return r.status, r.read(300).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode(errors="replace")


def _fail(code, msg, **extra):
    return dict({"ok": False, "error": {"code": code, "msg": msg}}, **extra)


async def ask_bot(bot: str, request: str) -> dict:
    name = str(bot or "").strip().lower()
    conf = bots().get(name)
    if not conf:
        return _fail("unknown_bot", "No bot named %r. Configured: %s" % (bot, ", ".join(bots()) or "none"))
    text = str(request or "").strip()
    if not text:
        return _fail("empty_request", "request is empty.")
    if len(text) > MAX_REQUEST:
        return _fail("request_too_long", "request is over %d characters." % MAX_REQUEST)
    if not _url_ok(conf["url"]):
        return _fail("bad_bot_url", "The %s webhook URL must be https." % name)
    ticket = "t-" + secrets.token_urlsafe(12)
    now = time.time()
    with _db() as c:
        c.execute("insert into tickets values(?,?,?,?,?,?,?,?)", (ticket, name, text, now, "sending", "", None, None))
    body = {"v": 1, "ticket": ticket, "request": text, "reply_with": "post_reply"}
    try:
        status, info = await asyncio.to_thread(_send, conf["url"], conf["key"], body)
    except Exception as e:  # network down, DNS, timeout
        status, info = 0, "%s: %s" % (type(e).__name__, e)
    ok = 200 <= status < 300
    with _db() as c:
        c.execute("update tickets set status=?, info=? where ticket=?",
                  ("sent" if ok else "failed", ("%s %s" % (status, info))[:400], ticket))
    if not ok:
        return _fail("send_failed", "%s's webhook didn't accept the request (%s)." % (name, status or info[:120]),
                     ticket=ticket, bot=name)
    return {"ok": True, "ticket": ticket, "bot": name, "status": "sent",
            "note": "Started. The answer comes later; don't wait and don't guess it. Tell Jordan in "
                    "first person that you're on it, without naming the bot. The app collects the "
                    "answer with check_reply."}


def check_reply(ticket: str) -> dict:
    with _db() as c:
        row = c.execute("select bot, created, status, info, reply, replied from tickets where ticket=?",
                        (str(ticket or ""),)).fetchone()
    if not row:
        return _fail("unknown_ticket", "No ticket %r." % ticket)
    bot, created, status, info, reply, replied = row
    if reply is not None:
        return {"ok": True, "ticket": ticket, "bot": bot, "status": "answered",
                "seconds": round(replied - created), "reply": json.loads(reply)}
    if status == "failed":
        return _fail("send_failed", "The request never reached %s (%s)." % (bot, info), ticket=ticket, status="failed")
    waited = time.time() - created
    if waited > TICKET_TTL:
        return _fail("no_answer", "No answer from %s within %d minutes. Tell Jordan you couldn't get it "
                     "done; never guess the answer." % (bot, TICKET_TTL // 60), ticket=ticket, status="expired")
    return {"ok": True, "ticket": ticket, "bot": bot, "status": "pending", "waited_seconds": round(waited)}


def post_reply(ticket: str, reply: dict | str) -> dict:
    if isinstance(reply, str):
        try:
            reply = json.loads(reply)
        except ValueError:
            return _fail("not_json", "reply must be your JSON reply envelope (an object), not prose.")
    if not isinstance(reply, dict):
        return _fail("not_json", "reply must be a JSON object (your reply envelope).")
    raw = json.dumps(reply)
    if len(raw) > MAX_REPLY:
        return _fail("reply_too_big", "reply is over %d bytes." % MAX_REPLY)
    with _db() as c:
        row = c.execute("select reply from tickets where ticket=?", (str(ticket or ""),)).fetchone()
        if not row:
            return _fail("unknown_ticket", "No ticket %r. Use the ticket from the request body exactly." % ticket)
        if row[0] is not None:
            return _fail("already_answered", "Ticket %s already has a reply; nothing changed." % ticket)
        c.execute("update tickets set reply=?, replied=?, status='answered' where ticket=?", (raw, time.time(), ticket))
    return {"ok": True, "ticket": ticket, "stored": True}
