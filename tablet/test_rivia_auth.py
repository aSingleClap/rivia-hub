"""Replays a Grok Bot-style OAuth handshake against rivia_auth. Stdlib only: python test_rivia_auth.py"""
import asyncio, base64, hashlib, html, json, os, re, secrets, tempfile
from urllib.parse import urlencode, urlparse, parse_qs

PW = "correct horse battery staple"
_salt = secrets.token_bytes(16)
_DOT = tempfile.mkdtemp()
os.environ.update(RIVIA_DB=os.path.join(_DOT, "t.db"),
                  RIVIA_DOTDIR=_DOT,
                  RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(PW.encode(), salt=_salt, n=2**14, r=8, p=1).hex(),
                  RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="))
import rivia_auth as ra
APP = ra.app
RELAY = "https://www.cursor.com/agents/mcp/oauth/callback"  # Grok Bot cloud callback
ROOT = "https://mcp.rivia.observer"


class R:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.text = status, headers, body.decode()
    def json(self): return json.loads(self.text)


def call(method, path, *, headers=None, form=None, js=None):
    path, _, qs = path.partition("?")
    body = b""
    hdrs = [(b"host", b"mcp.rivia.observer")]
    if form is not None:
        body = urlencode(form).encode(); hdrs.append((b"content-type", b"application/x-www-form-urlencoded"))
    if js is not None:
        body = json.dumps(js).encode(); hdrs.append((b"content-type", b"application/json"))
    hdrs.append((b"content-length", str(len(body)).encode()))
    for k, v in (headers or {}).items(): hdrs.append((k.lower().encode(), v.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": qs.encode(),
             "root_path": "", "headers": hdrs, "server": ("mcp.rivia.observer", 443), "client": ("1.2.3.4", 1)}
    sent, out = [False], {"status": 0, "headers": {}, "body": b""}
    async def receive():
        if sent[0]: return {"type": "http.disconnect"}
        sent[0] = True; return {"type": "http.request", "body": body, "more_body": False}
    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]; out["headers"] = {k.decode(): v.decode() for k, v in m["headers"]}
        elif m["type"] == "http.response.body": out["body"] += m.get("body", b"")
    asyncio.run(APP(scope, receive, send))
    return R(out["status"], out["headers"], out["body"])


def follow(resp): return html.unescape(re.search(r"url=([^']+)'", resp.text).group(1))
def ok(cond, label): assert cond, label; print("  ok ·", label)

# 0. TOTP matches RFC 6238 Appendix B vector (SHA1, T=59 -> 94287082 -> last 6 digits)
ok(ra.totp_now(base64.b32encode(b"12345678901234567890").decode(), 59) == "287082", "TOTP matches RFC 6238 vector")

# 1. tokenless probes -> 401 + resource_metadata pointer
for m, p in (("HEAD", "/"), ("POST", "/mcp"), ("DELETE", "/mcp"), ("GET", "/mcp")):
    r = call(m, p)
    ok(r.status == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""), f"{m} {p} -> 401 w/ resource_metadata")

# 2. discovery (bare and path-suffixed)
prm = call("GET", "/.well-known/oauth-protected-resource/mcp").json()
ok(prm["authorization_servers"] == [ROOT] and prm["resource"] == ROOT + "/mcp", "PRM /mcp")
ok(call("GET", "/.well-known/oauth-protected-resource").status == 200, "PRM bare")
asm = call("GET", "/.well-known/oauth-authorization-server").json()
ok(asm["registration_endpoint"] == ROOT + "/register" and asm["code_challenge_methods_supported"] == ["S256"], "ASM")

# 3. DCR
ok(call("POST", "/register", js={"redirect_uris": ["https://evil.example/cb"]}).status == 400, "DCR rejects foreign redirect")
ok(call("POST", "/register", js={"redirect_uris": ["http://oauth-redirect.googleusercontent.com/r/x"]}).status == 400, "DCR rejects http")
ok(call("POST", "/register", js={"redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/x"]}).status == 400,
   "DCR rejects old Spark relay (not on allowlist)")
ok(call("POST", "/register", js={"redirect_uris": ["https://www.cursor.com/agents/mcp/oauth/callback/../x"]}).status == 400,
   "DCR rejects near-miss of an allowed URI")
ok(call("POST", "/register", js={"redirect_uris": "https://www.cursor.com/agents/mcp/oauth/callback"}).status == 400,
   "DCR rejects redirect_uris that isn't a list")
uris = ["cursor://anysphere.cursor-mcp/oauth/callback", RELAY,
        "https://www.cursor.com/bot/mcp/oauth/callback", "http://127.0.0.1:54321/callback",
        "https://unknown.example/cb"]
reg = call("POST", "/register", js={"client_name": "Grok Bot", "redirect_uris": uris})
ok(reg.status == 201 and "client_secret" not in reg.json(), "DCR accepts Grok Bot's mixed array, public client")
ok(reg.json()["redirect_uris"] == uris[:4], "unknown redirect dropped, allowed ones kept in order")
cid = reg.json()["client_id"]
with ra.db() as conn:
    rows = conn.execute("select client_name, refused, client_id from registrations order by ts").fetchall()
ok(rows[-1][0] == "Grok Bot" and json.loads(rows[-1][1]) == ["https://unknown.example/cb"] and rows[-1][2] == cid,
   "registration log records the refused redirect")
ok(any(r[2] is None for r in rows), "fully refused registration is logged too")
reg_web = call("POST", "/register", js={"client_name": "Grok", "redirect_uris": ["https://grok.com/connectors-oauth-exchange-code/"]})
ok(reg_web.status == 201, "grok.com DCR fallback accepted")
ok(call("POST", "/register", js={"redirect_uris": ["https://grok.com/connectors-oauth-exchange-code/x"]}).status == 400,
   "grok.com near-miss refused")

# 4. consent
ver = secrets.token_urlsafe(48)
chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
state = "s" * 1400  # Spark's state is ~1,400 chars
q = dict(client_id=cid, redirect_uri=RELAY, response_type="code", code_challenge=chal,
         code_challenge_method="S256", state=state, resource=ROOT + "/mcp")
g = call("GET", "/authorize?" + urlencode(q))
ok(g.status == 200 and "form-action" not in g.headers["content-security-policy"], "consent page, no form-action CSP")
ok("Connect Grok Bot to RIVIA?" in g.text and "<b>www.cursor.com</b>" in g.text, "consent page names client and code destination")
ok(call("GET", "/authorize?" + urlencode({**q, "redirect_uri": "https://unknown.example/cb"})).status == 400,
   "dropped redirect can't be used at /authorize")
ok(call("GET", "/authorize?" + urlencode({**q, "redirect_uri": "https://evil.example/cb"})).status == 400, "unregistered redirect -> error page, no bounce")
ok(call("POST", "/authorize", form={**q, "act": "allow", "pw": "nope", "otp": "000000"}).status == 401, "wrong creds rejected")
good = call("POST", "/authorize", form={**q, "act": "allow", "pw": PW, "otp": ra.totp_now(ra.TOTP_SECRET)})
ok(good.status == 200 and "http-equiv=refresh" in good.text, "approve -> 200 meta-refresh (not 302)")
cb = parse_qs(urlparse(follow(good)).query)
ok(cb["state"][0] == state, "1,400-char state returned byte for byte")
code = cb["code"][0]

# 4b. grok.com via CIMD (client_id is a URL, no /register)
GROK_CID = "https://grok.com/oauth/mcp-client.json"
GROK_CB = "https://grok.com/connectors-oauth-exchange-code/"
ok(asm.get("client_id_metadata_document_supported") is True, "ASM advertises CIMD")
gq = {**q, "client_id": GROK_CID, "redirect_uri": GROK_CB}
gg = call("GET", "/authorize?" + urlencode(gq))
ok(gg.status == 200 and "Connect Grok to RIVIA?" in gg.text and "<b>grok.com</b>" in gg.text, "grok.com CIMD consent page")
ok(call("GET", "/authorize?" + urlencode({**gq, "redirect_uri": RELAY})).status == 400, "grok.com CIMD can't use a Cursor redirect")
ok(call("GET", "/authorize?" + urlencode({**gq, "client_id": "https://evil.example/client.json"})).status == 400,
   "other URL client_ids rejected (nothing fetched)")
gapp = call("POST", "/authorize", form={**gq, "act": "allow", "pw": PW, "otp": ra.totp_now(ra.TOTP_SECRET)})
gcode = parse_qs(urlparse(follow(gapp)).query)["code"][0]
gtr = call("POST", "/token", form=dict(grant_type="authorization_code", client_id=GROK_CID, redirect_uri=GROK_CB,
                                       code=gcode, code_verifier=ver))
ok(gtr.status == 200 and "access_token" in gtr.json(), "grok.com CIMD token exchange")
ok(call("POST", "/mcp", headers={"Authorization": "Bearer " + gtr.json()["access_token"]}).status == 200, "grok.com token works at /mcp")

# 5. token exchange
base = dict(grant_type="authorization_code", client_id=cid, redirect_uri=RELAY, code=code)
ok(call("POST", "/token", form={**base, "code_verifier": "wrong"}).status == 400, "bad PKCE verifier rejected")
# the bad-verifier attempt burned the code (single use) -> need a fresh one
good = call("POST", "/authorize", form={**q, "act": "allow", "pw": PW, "otp": ra.totp_now(ra.TOTP_SECRET)})
base["code"] = parse_qs(urlparse(follow(good)).query)["code"][0]
tr = call("POST", "/token?resource=" + ROOT + "/mcp", form={**base, "code_verifier": ver})
ok(tr.status == 200 and tr.headers.get("cache-control") == "no-store", "PKCE exchange with ?resource= -> tokens")
tok = tr.json()
ok(call("POST", "/token", form={**base, "code_verifier": ver}).status == 400, "code replay rejected")
ok(call("POST", "/token", form={**base, "client_id": "rivia-bogus", "code_verifier": ver}).status == 401, "unknown client rejected")

# 6. protected calls
h = {"Authorization": "Bearer " + tok["access_token"]}
ok(call("POST", "/mcp", headers=h).json()["ok"], "POST /mcp with bearer")
ok(call("POST", "/", headers=h).status == 200, "POST / with bearer (root)")
ok(call("POST", "/mcp", headers={"Authorization": "Bearer " + tok["refresh_token"]}).status == 401, "refresh token can't be used as access")
ok(call("POST", "/mcp", headers={"Authorization": "Bearer " + tok["access_token"][:-4] + "AAAA"}).status == 401, "tampered JWT rejected")

# 7. refresh rotation
rr = call("POST", "/token", form=dict(grant_type="refresh_token", client_id=cid, refresh_token=tok["refresh_token"]))
ok(rr.status == 200 and "access_token" in rr.json(), "refresh issues new pair")
ok(call("POST", "/token", form=dict(grant_type="refresh_token", client_id=cid, refresh_token=tok["refresh_token"])).status == 400, "old refresh token dead after rotation")

# 8. deny + kill switch
ok("error=access_denied" in follow(call("POST", "/authorize", form={**q, "act": "deny"})), "deny -> access_denied bounce")
with ra.db() as conn: conn.execute("update tokens set revoked=1")
ok(call("POST", "/mcp", headers=h).status == 401, "revoke-all kills live tokens")

# 9. brute-force lockout (1 failure already recorded above)
for _ in range(4): call("POST", "/authorize", form={**q, "act": "allow", "pw": "x", "otp": "1"})
ok(call("POST", "/authorize", form={**q, "act": "allow", "pw": PW, "otp": ra.totp_now(ra.TOTP_SECRET)}).status == 429,
   "5 failures -> lockout even with correct creds")

# 10. door table and admin token (never printed)
import stat, subprocess, sys
app_info = ra.mint_door_token("app", 30)
admin_info = ra.mint_door_token("admin", 7)
ok(admin_info["path"] == ra.admin_token_path() and os.path.isfile(admin_info["path"]),
   "mint_door_token admin writes the token file under RIVIA_DOTDIR")
ok(stat.S_IMODE(os.stat(admin_info["path"]).st_mode) == 0o600, "admin token file mode is 600")
ok(ra.tokens_equal(admin_info["token"], ra.load_admin_token()),
   "stored admin token matches in constant time")
ah = {"Authorization": "Bearer " + admin_info["token"]}
ph = {"Authorization": "Bearer " + app_info["token"]}
# echo_stub is mounted at /mcp and /; /app and /admin are not mounted here, but
# the gate still enforces the door table on the path.
ok(call("POST", "/admin/mcp", headers=ah).status in (200, 404),
   "admin token is accepted for the admin path")
ok(call("POST", "/app/mcp", headers=ah).status == 403
   and call("POST", "/app/mcp", headers=ah).json()["error"] == "wrong_door",
   "admin token on the app path is 403 wrong_door")
ok(call("POST", "/mcp", headers=ah).status == 403
   and call("POST", "/mcp", headers=ah).json()["error"] == "wrong_door",
   "admin token on the bot path is 403 wrong_door")
ok(call("POST", "/admin/mcp", headers=ph).status == 403
   and call("POST", "/admin/mcp", headers=ph).json()["error"] == "wrong_door",
   "app token on the admin path is 403 wrong_door")
ok(call("POST", "/app/mcp", headers=ph).status in (200, 404),
   "a new admin token leaves the app token able to open the app path")
cli = subprocess.run([sys.executable, __file__.replace("test_rivia_auth.py", "rivia_auth.py"),
                      "mint-token", "admin", "7"],
                     capture_output=True, text=True, env=os.environ)
ok(cli.returncode == 0 and "eyJ" not in cli.stdout and "eyJ" not in cli.stderr,
   "mint-token admin CLI does not print the token")
ok(call("POST", "/app/mcp", headers=ph).status in (200, 404),
   "reminting the admin token still leaves the app token working")
ra.clear_admin_token_file()
# subprocess revoke-all
rev = subprocess.run([sys.executable, __file__.replace("test_rivia_auth.py", "rivia_auth.py"),
                      "revoke-all"],
                     capture_output=True, text=True, env=os.environ)
ok(rev.returncode == 0 and not os.path.exists(ra.admin_token_path()),
   "revoke-all removes the stored admin token")
ok("eyJ" not in rev.stdout and "eyJ" not in rev.stderr, "revoke-all does not print a token")
ok(call("POST", "/admin/mcp", headers=ah).status == 401,
   "a revoked admin token is 401 on the admin path")

print("\nALL PASS")
