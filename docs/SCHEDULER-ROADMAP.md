# Scheduler on the tablet: full spec

As of Nov 14 2026. The detailed design behind `docs/SCHEDULER-ROADMAP.md`. Written from the code in the project (`rivia_planner.py` 4.1, `rivia_server.py`, `rivia_auth.py`, `rivia_bots.py`, `rivia.sh`) and checked against Google's and FastMCP's current documentation. **None of it has been built or run.** It assumes no roadmap step is done yet; skip any that are.

## 1. What this builds

The scheduler becomes three layers on the tablet's MCP server:

1. **Actions** are fixed Python on the tablet: ride times, day plans, arrivals, ride logging, value changes, calendar writes. They are tested and can never be edited over the network.
2. **Tools** are rows in a table. Each row gives one action a name, a description and some locked-in arguments. Rivia's model sees tools, never actions. The web app (Server Hub) can add, rename, re-describe and remove rows.
3. **Settings** are the planner's numbers (durations, floors, buffers, ride estimates, gym hours). They move out of the code into the database, behind one change tool that checks every change before it takes effect.

Three clients reach it through three doors (section 4): the phone app, the web app, and outside connectors.

## 2. Rules that don't bend

1. **The planner does all time math.** Every clock time and duration in a reply comes out of `rivia_planner.py`. No tool computes or rounds a time anywhere else.
2. **A failed step stays failed.** Planner, Calendar, database or Drive failure returns `ok: false` with the step named. No stale data, no older answer, no guess.
3. **Jordan's main calendar is read-only.** Writes go only to a calendar this server created. Google enforces this through the scope it grants (section 10), not just our code.
4. **Bounds and logic live in code.** The network can change a value inside its range. It cannot change a range, an action or a line of Python.
5. **Standing changes and calendar writes wait for Jordan's approval.** No verdict means no. Day-only changes apply at once because they are bounded, logged, undoable and expire.
6. **Every tool is safe to call twice.** The phone app retries a tablet call once on a timeout (`app/REMOVAL-MAP.md`), so a repeat must never double-write.
7. **Rivia is one system.** Replies never name a tool, a door or the tablet.

## 3. Files and where things live

Code, in the `rivia` folder on internal storage (`~/storage/shared/rivia/`):

| File | Status | Job |
|---|---|---|
| `rivia_planner.py`, `rivia_planner_tests.py` | copy from `grok-bot/`, then 4.2 (section 12) | The scheduling math. No network. |
| `rivia_gcal.py` | new | Google sign-in and Calendar calls over plain HTTPS (`urllib`). No Google library. |
| `rivia_sched.py` | new | Settings, day overrides, change log, approval queue, and the scheduler actions. Also the `rivia sched ...` commands. |
| `rivia_tools.py` | new | The action catalog and the tool table; turns rows into FastMCP tools. |
| `rivia_server.py` | edit | Registers the tools and adds the admin door. |
| `rivia_auth.py` | edit | A third door and its token. |
| `test_sched.py` | new | Added to `rivia test`. |
| `rivia.sh` | edit | `rivia sched ...`, `rivia admin-token`, the longer test list. |

Data, in Termux home (never in shared storage, which is not safe for SQLite or secrets):

| Path | Holds |
|---|---|
| `~/rivia/scheduler.db` | Rides (planner tables `rides`, `meta`) plus the new tables in section 11. |
| `~/rivia/places.json` | Place pins. Rewritten by the server when a pin setting changes; the planner keeps reading it. |
| `~/rivia/backup/` | The planner's backup copy. |
| `~/.rivia/.env` | Adds `RIVIA_GOOGLE_CLIENT_ID`, `RIVIA_GOOGLE_CLIENT_SECRET`. |
| `~/.rivia/google_token.json` | Google refresh token, mode 600. |

New package: `tzdata` only (pure Python).

## 4. Doors and tokens

| Door | Path | Token | Used by | Can do |
|---|---|---|---|---|
| Bot | `/mcp` | OAuth sign-in (passphrase + authenticator) | Grok, Claude or other connectors | Read-only tools you choose to expose. Default: none of the scheduler. |
| App | `/app/mcp` | `rivia token` | The phone app and Gemini's tool calls | Run tools. Day-only changes. Propose standing changes and calendar writes. |
| Admin | `/admin/mcp` | `rivia admin-token` | The web app | Edit tools and settings, approve or reject, read status and logs. |

Changes to `rivia_auth.py`:
- Replace the single `APP_CLIENT` / `APP_PREFIX` pair with a table: `DOORS = {"rivia-app": "/app/", "rivia-admin": "/admin/"}`. A token whose `client_id` is in the table opens only its own prefix. Every other token opens only the rest. Same `wrong_door` 403 as today.
- `mint-app-token` becomes `mint-token <app|admin> [days]`. Minting one kind revokes only that kind, so a new web-app token no longer logs the phone out. `rivia token` keeps working.
- Admin tokens default to 7 days (the phone's stays 365). The web app keeps it in session storage.
- `build_app` takes a third MCP app and mounts it at `/admin`, the same way `/app` is mounted. The admin door is stateless plain JSON like the app door, so the web page uses the same one-POST-per-call client as the phone.

The app door cannot approve anything. That is the point of the split: the model can ask, only a person can say yes.

## 5. Actions (fixed code)

Each action declares its parameters. A ★ marks a parameter a tool row may lock in.

| Action | Parameters | Class | Doors |
|---|---|---|---|
| `sched.ride_time` | `from`★, `to`★ (place ids) | read | app, bot |
| `sched.plan_day` | `day`★ | read, uses Calendar | app, bot |
| `sched.plan_week` | none | read, uses Calendar | app, bot |
| `sched.arrival` | `place`, `title`, `coords`, `time`, `ride` | logs a ride | app, code only |
| `sched.log_ride` | the ride fields | logs a ride | app, code only |
| `sched.get_settings` | `group`★, `day` | read | app |
| `sched.change_setting` | `key`★, `value`, `scope`★, `day`, `reason`, `request_id` | bounded write | app |
| `sched.undo_change` | `change_id` | bounded write | app |
| `sched.write_day` | `day`★ | needs approval | app |
| `tablet.status` | `report`★ | read | app, bot |

What each does:

- **`ride_time`** runs the planner's `legs` command for one pair. Reply `kind: "legs"` with `minutes` and `source` ("learned from N rides", "your estimate"). No Calendar. Target: under a second.
- **`plan_day`** resolves `day` (`today`, `tomorrow`, a weekday name meaning the next one, or `YYYY-MM-DD`), reads the main calendar from 2 days before to 2 days after, and runs `plan` for that one day with that day's settings. Reply `kind: "plan"`: the planner's `message` plus `data` = `{day, status, shift, gym, blocks, problems, rides, written: false}`, unchanged. It never writes.
- **`plan_week`** does the same for the next 7 days and returns each day's `status`, `problems` and `message` without blocks.
- **`arrival`** logs the ride if one is given (`log-ride`, safe to repeat on `ride_id`), reads the calendar one day each side of `time`, runs `arrival`, and returns `message`, `alerts` and `fallback_text`. `alerts` keeps the planner's meaning: `null` = leave current alerts, `[]` = clear them, a list = replace them. The app sets the alarms. Nothing is written to a calendar.
- **`get_settings`** returns key, label, value, default, unit, range and any override for the day asked about.
- **`change_setting`**, **`undo_change`**: section 7.
- **`write_day`**: section 8.
- **`tablet.status`** is today's `get_tablet_battery` as an action: battery percent and charging state now; other readings only after someone adds and tests them in code.

Network actions are `async` and push blocking work to a thread (`asyncio.to_thread`, as `rivia_bots.ask_bot` already does), so a slow Google call can't stall other requests.

A ride with no source is a hard stop. The planner answers `need_maps` when a pair has no learned rides and no estimate; with no Maps lookup on the tablet this becomes `ok: false`, code `need_estimate`, listing the pairs. See risk 1 in section 19.

## 6. Tools (the editable layer)

A tool row: `name`, `door` (app or bot), `action`, `preset` (locked arguments), `description`, `model` (offered to Gemini or called by app code), `enabled`.

Rules the server enforces on every save:
- `name` matches `^[a-z][a-z0-9_]{2,40}$`, is unique on its door, and is not reserved (`ask_bot`, `check_reply`, `post_reply`, any admin tool name).
- `action` exists and is allowed on that door. Admin functions are not actions and can never become tools.
- `preset` may only set ★ parameters, and each value passes the same check as a live argument. Locked parameters disappear from what the model sees and cannot be overridden by the caller.
- `description` is 20 to 600 characters. The server appends fixed lines the web app cannot remove: the failed-tool rule; for time-bearing actions, "say times exactly as their `spoken` value, never work one out, say every `must_say` fact, offer `decisions_for_jordan` and never pick one"; for approval actions, "a `pending` result has not happened yet".
- Tools that app code calls directly (`model: false`) must be named `app_...`.
- At most 30 tools per door.

Seeded on first start, so the server works before the web app exists:

| Tool | Action | Preset |
|---|---|---|
| `ride_time` | `sched.ride_time` | none |
| `plan_day` | `sched.plan_day` | none |
| `get_schedule_settings` | `sched.get_settings` | `group: schedule` |
| `change_schedule_setting` | `sched.change_setting` | none |
| `undo_schedule_change` | `sched.undo_change` | none |
| `write_day` | `sched.write_day` | none |
| `app_arrival` | `sched.arrival` | none (`model: false`) |
| `app_log_ride` | `sched.log_ride` | none (`model: false`) |

Examples of what the web app can then make without code: `plan_tomorrow` (`sched.plan_day`, `day: tomorrow`), `ride_farm_to_gym` (`sched.ride_time`, both ends locked), `farm_today` (`sched.change_setting`, `key: farm_minutes`, `scope: day`, so Gemini only supplies the number).

How a row becomes a tool: each action is one FastMCP tool object created with the standalone `@tool` decorator and never registered itself. A row is `Tool.from_tool(base, name=..., description=..., transform_args={param: ArgTransform(hide=True, default=value)})`, then `add_tool` on that door's server. FastMCP documents exactly this for hiding an argument and passing a constant. After any save the door's row-built tools are rebuilt. Whether 4.1.x can remove a tool while running is unverified; if the test in section 15 fails, a save returns `restart_needed: true` and the admin `restart_server` call applies it (the server exits and the watchdog in `rivia.sh` starts it again within about a minute).

Editing a description changes when Gemini calls the tool. Rerun the H10 and H7 sheets after editing one, as after a layer change.

## 7. Settings and the change tool

Defaults stay in `rivia_planner.py` (`CFG`, `MEASURED_LEGS`, `GYM_HOURS`, `PLACES`). The database stores only values that differ. The catalog below (type, range, who may change it) is code in `rivia_sched.py`, so a range can't be widened over the network. This differs from the roadmap's wording ("minimum, maximum and default in `scheduler.db`") for that reason.

**The values a day is built from.** Ranges are proposals; confirm or change them.

| Key | Planner name | Default | Proposed range | Day change | Rivia may ask |
|---|---|---|---|---|---|
| `arrive_early_minutes` | `bufferM` | 10 | 0–30 | yes | yes |
| `prework_minutes` | `preworkM` | 30 | 0–90 | yes | yes |
| `prework_floor_minutes` | `preworkFloorM` | 15 | 0–60 | yes | yes |
| `workout_minutes` | `workoutM` | 120 | 30–180 | yes | yes |
| `workout_floor_minutes` | `workoutFloorM` | 60 | 15–120 | yes | yes |
| `farm_minutes` | `farmM` | 45 | 10–180 | yes | yes |
| `alert_lead_minutes` | `leadM` | 5 | 0–30 | yes | yes |
| `wind_down_minutes` | `windDownM` | 60 | 0–180 | yes | yes |
| `wake_up_minutes` | `wakeUpM` | 60 | 15–180 | yes | yes |
| `sleep_target_hours` | `sleepTargetH` | 8 | 4–10 | yes | yes |
| `place_radius_meters` | `placeRadiusM` | 400 | 50–2000 | no | no |
| `overnight_hours` | `overnightH` | 8 | 4–16 | no | no |
| `home_lookahead_hours` | `homeLookaheadH` | 20 | 8–36 | no | no |
| `learn_keep_rides` | `learnKeep` | 10 | 3–50 | no | no |
| `learn_min_rides` | `learnMin` | 3 | 1–10 | no | no |
| `learn_rank` | `learnRank` | 0.75 | 0.5–1.0 | no | no |
| `ride_max_hours` | `rideMaxH` | 3 | 1–8 | no | no |
| `clean_low_ratio` | `cleanLow` | 0.25 | 0.05–0.9 | no | no |
| `clean_high_ratio` | `cleanHigh` | 2 | 1.1–5 | no | no |

Families with the same rules for each member:
- `ride_estimate.<a>_<b>`: your own minutes for a ride, either direction, for any two of `home`, `farm`, `gym`, `pf`, `hmc` (ids in alphabetical order). Whole minutes, 1–180. Standing only. Rivia may ask.
- `gym_hours.<gym|pf>.<weekday_open|weekday_close|weekend_open|weekend_close>`: hour of day, 0–24. Standing only. Rivia may ask.
- `place.<id>.address`, `.spoken_name`, `.pin`: web app only. Place ids are fixed; the planner's logic is written around them, so a new place is a code change.

**What a day uses.** Code defaults, then standing values, then that day's overrides. For `plan_day` the day is the day planned; for `arrival` it is the arrival's New York date. The server holds one lock, swaps these values into the planner module for the length of one call, and restores the defaults, so the planner's selftest always runs against the numbers its checks expect.

**Every change passes four checks, in order:**
1. **Known and in range.** Right type, inside the catalog range, and this door may change this key at this scope.
2. **Consistent with the others.** `prework_floor ≤ prework`, `workout_floor ≤ workout`, `learn_min ≤ learn_keep`, `clean_low < 1 < clean_high`, `overnight_hours < home_lookahead_hours`, gym open before close.
3. **Dry run.** Build three sample workdays with the planner (a weekday after a day off, a weekday after a shift, a Saturday at Planet Fitness) and run `check_day` on each, before and after the change. If the change adds a problem that wasn't there, it is refused and the new problems are returned. The Saturday sample is skipped while its ride estimates are missing (risk 1).
4. **Logged.** Old value, new value, door, reason and time go in `changes` before the value is used.

**Then, by scope and door:**

| Asked through | `scope: day` | `scope: standing` |
|---|---|---|
| App door (Rivia) | Applied now for that date (today to 14 days ahead). Gone after that date. | Stored as `pending`. Nothing changes until approved (section 8). |
| Admin door (you) | Applied now. | Applied now. |

`undo_change` restores the old value and logs that as its own change. With no id it undoes the latest applied change made through the same door in the last 24 hours. Undoing a standing change through the app door is itself a standing change, so it goes to pending.

Repeats: a second identical call within 60 seconds, or one carrying the same `request_id`, returns the first result with `duplicate: true`.

Two things the result always says: where a value now comes from (default, standing, day override), and, for a ride estimate, whether it is in use. The planner prefers learned rides once there are three clean ones, so an edited estimate may change nothing; the result says so.

**Alerts already set.** A day change for today can make a leave-by alarm on the phone wrong (shorten the farm and the old "leave for the gym" time is stale). So `arrival` stores the last arrival (place, title, time), and a change that applies today runs that arrival again and returns the new `message` and `alerts` in its own reply. If that rerun fails, the change still stands, `alerts` is `null` and `data.alerts_refresh` says `failed`.

## 8. The approval queue

Two things create a pending item instead of acting: a standing change asked through the app door, and `write_day`.

- A pending item holds what was asked, a preview (old and new value, or the blocks to be written), who asked and when. It expires after 24 hours.
- Approve or reject happens on the admin door (`approve_change`, `reject_change`) or in Termux (`rivia sched approve <id>`). A Watch tap can call the same function when that pass exists.
- On approve the server runs every check again against current values. For `write_day` it plans the day again; if the result no longer matches the preview it refuses with `changed_since_preview` and writes nothing.
- The reply to the model is `status: "pending"` with the id. The fixed description line tells Gemini to say it is waiting for approval and never that it is done.
- A second identical request while one is pending returns the existing item.

`write_day`, once approved, for a day whose status is `planned`: create the new events first, tagged in each event's private extended properties (`rivia_day`, `rivia_kind`, `rivia_batch`), then delete that day's older Rivia-written events found by the `rivia_day` tag. If creating fails partway, the new batch is removed and the old plan is left alone. `no_shift` deletes that day's Rivia-written events. `not_written` and `under_way` write nothing, as in Ard's rules.

## 9. Reply shape

Every scheduler tool returns Ard's envelope, with one change: `alerts` carries the list itself (Ard wrote alerts to the calendar and returned a count).

```json
{ "v": 1, "ok": true,
  "kind": "legs | plan | arrival | ride | settings | change | pending | undo | status",
  "ref": "<day, ride_id or change_id>",
  "message": { "...the planner's message, unchanged, or null" },
  "data": { },
  "alerts": null,
  "fallback_text": null,
  "error": null }
```

Failure: `ok: false`, `kind: "error"`, `message: null`, and `error` = `{code, msg, step}` with `step` one of `input`, `planner`, `calendar`, `settings`, `database`, `drive`. `message` is exactly what `grok-bot/PAYLOAD.md` describes.

If the planner's selftest fails at startup, every scheduler action returns `selftest_failed` until it passes.

## 10. Google sign-in and Calendar

**Setup, once (roadmap step 3):**
1. A Google Cloud project with the Calendar API enabled.
2. OAuth consent screen: External, published to **In production**. In Testing, Google ends the sign-in after seven days. Expect an "unverified app" warning at sign-in; that is normal for a personal app.
3. An OAuth client of type **Desktop app**. Put its id and secret in `~/.rivia/.env`.
4. `rivia sched google-login` on the tablet.

**The sign-in:** the script listens on `127.0.0.1` on a free port, prints a Google link (and opens it with `termux-open-url`), you sign in in the tablet's browser, and Google sends the browser back to that local address with a code. The script checks `state`, swaps the code for tokens with PKCE, and saves the refresh token. If the page can't load because Android paused Termux, copy the address from the browser's bar and paste it at the prompt. Google's code-on-another-device flow is not an option: its allowed scopes include no Calendar scope.

**Scopes, least first:**

| When | Scope | Grants |
|---|---|---|
| Step 3 | `calendar.events.readonly` | Read events on your calendars. |
| Step 8 | `calendar.app.created` | Create secondary calendars and manage events only on calendars this app created. |
| Step 8 | `drive.file` | Only files this app created (the backup copy). |

Adding a scope means signing in again (`rivia sched google-login --write`). Because `calendar.app.created` can't touch a calendar it didn't make, the server creates its own calendar named Rivia on the first approved write and stores its id. Rename or delete the old Rivia calendar that Ard wrote to, or there will be two.

**Reading events:** `GET /calendar/v3/calendars/primary/events` with `timeMin`, `timeMax`, `singleEvents=true`, `orderBy=startTime`, following `nextPageToken`. Each becomes `{title, start, end, location, all_day}` for the planner; cancelled events are dropped. Only `primary` is ever read, so Rivia-written events never reach the planner.

**Failure handling:** 10-second timeout, one retry on a timeout or 5xx. A refresh that returns `invalid_grant` becomes `calendar_login_expired` with the command to fix it. No result is cached across calls except the access token.

Clock check: the server compares the `Date` header on Google's replies with the tablet's clock and reports a gap over two minutes in status. The planner trusts the tablet's clock for "now".

## 11. Database

Added to `~/rivia/scheduler.db`. The file is always opened through the planner's `db_open()` first, so the planner still creates it and its new-and-empty guard (`restore_first`) still works.

| Table | Columns | Notes |
|---|---|---|
| `settings` | `key`, `value`, `updated`, `change_id` | Standing values that differ from the default. Delete the row to reset. |
| `overrides` | `day`, `key`, `value`, `created`, `change_id` | One row per day and key. Rows older than yesterday are purged on start. |
| `changes` | `change_id`, `created`, `door`, `kind`, `scope`, `day`, `key`, `old`, `new`, `preview`, `status`, `decided`, `reason`, `request_id` | The audit log and the queue. `status`: `applied`, `pending`, `rejected`, `expired`, `undone`, `failed`. |
| `tools` | `name`, `door`, `action`, `preset`, `description`, `model`, `enabled`, `updated` | Key is `(door, name)`. |
| `calls` | `ts`, `door`, `tool`, `action`, `ok`, `code`, `ms` | Last 500 calls, for the web app's Logs card. Never arguments, results or event titles. |

`meta` gains `gcal_rivia_id`, `drive_file_id` and the last selftest result. The planner's `backup` copies the whole file, so these tables ride along in the Drive copy. `restore` still merges rides only; to bring settings back after losing the tablet, put the backup file in place before the first start.

## 12. Planner changes (4.2)

Small, and each needs a check added to `rivia_planner_tests.py`:
1. Two fallback lines hardcode the workout floor: "a 60-minute workout no longer fits" in `farm_plan` and "under the 60-minute floor" in `gym_plan`. Both must read `CFG['workoutFloorM']`, or the spoken fallback is wrong the day the floor changes.
2. Nothing else in the planner changes. Settings reach it through the swap in section 7, not through new parameters.

## 13. What the phone app must change

Each is a small AI Studio pass, not part of the tablet work:
1. **Don't offer `app_...` tools to Gemini** (step 6). Today Lab chats offer every listed tool with a description and a schema.
2. **Check the argument filter.** Bots pass 1 hardcoded an allowed-argument list per tool; if any of it survived the removal, new tools arrive with their arguments stripped.
3. **Call `app_arrival` and set exact alarms from `alerts`** (step 6). Apply `alerts` from any tablet reply where it isn't `null`, not only from arrivals, so a day change can replace an alarm (step 7a).
4. **Drop the three hidden names** (`ask_bot`, `check_reply`, `post_reply`) after the server stops serving them (step 8).
5. **Nothing checks spoken times anymore.** `BotReplyVerifier` was deleted with the bot pipeline. Until an equivalent exists, the only guard on what Gemini says about a time is the fixed description line. See risk 3.

## 14. Admin door (what the web app calls)

Fixed tools, not rows, not renameable: `hub_status`, `list_actions`, `list_tools`, `save_tool`, `delete_tool`, `test_tool` (read actions only), `get_settings`, `set_setting`, `list_changes`, `approve_change`, `reject_change`, `undo_change`, `list_calls`, `restart_server`.

These back the Server Hub's cards: Tools (`list_actions`, `list_tools`, `save_tool`), Logs (`list_calls`), a Settings page (`get_settings`, `set_setting`), a Pending list (`list_changes`, `approve_change`). `restart_server` makes the Restart button real. Start and Stop can't be: a stopped server can't hear a start, and the watchdog undoes a stop.

Raw calendar events are deliberately not on any door. `rivia sched events <day>` prints them in Termux only.

## 15. Tests (`test_sched.py`)

Same style as `test_bots.py`: a throwaway data folder, the real server with all three doors, and a fake Google on loopback (token, event list, calendar create, event create and delete). `rivia test` also runs `python rivia_planner.py selftest`. It must show:

- Each token opens only its own door; a new admin token leaves the app token working.
- Seeded tools list on the app door; a saved row appears in `tools/list` without a restart (or the save says `restart_needed`); a locked parameter is absent from the tool's schema and a caller can't override it.
- Saves are refused for a reserved name, an unknown action, an admin function, a non-★ preset, and a short description.
- `ride_time` matches the planner's `legs`; a pair with no source returns `need_estimate`, never a number.
- `plan_day` on fake events returns the same leave-home time as the planner called directly; a Google failure returns `ok: false`, step `calendar`, and no times.
- `change_setting`: out of range refused; floor above target refused; a change that breaks a sample day refused with the problems listed; a day change alters only that day and is gone the day after; a standing change through the app door stays pending and changes no plan until approved.
- The app token cannot approve. Approve applies once; a repeat is refused. Undo restores the old value.
- A repeated call returns `duplicate: true` and leaves one row in `changes`.
- `write_day`: nothing is written before approval; after it, the fake Rivia calendar holds exactly the planner's `calendar_events`; any write aimed at `primary` fails the test; a plan that changed since the preview is refused.
- A failing selftest blocks every scheduler action.

## 16. Build order

Follows the roadmap's steps. Each ends with `rivia test` passing.

| Step | Build | Done when |
|---|---|---|
| 1 | Planner files on the tablet, `tzdata` | `rivia sched selftest` passes every check. |
| 2 | `rivia_tools.py` with the catalog and seeded rows (no editing yet); `sched.ride_time` | `ride_time` answers in under a second from the app's Tablet screen. |
| 3 | `rivia_gcal.py`, `google-login`, `rivia sched events` | `rivia sched events tomorrow` prints tomorrow, and still does 8 days later. |
| 4 | `sched.plan_day`, `sched.plan_week` | "What does tomorrow look like?" in a Labs chat gets a line in a few seconds. |
| 5 | `rivia sched restore <file>` with Ard's Drive copy | The tablet's ride count matches Ard's. |
| 6 | `sched.arrival`, `sched.log_ride`; app passes 13.1 and 13.3 | A real arrival speaks the leave-by line and the alarm fires on time. |
| 7a | Planner 4.2, settings, `change_setting`, `undo_change` | The three checks in the roadmap's step 7 pass from a Labs chat. |
| 7b | Third door, approval queue, admin tools | A standing change asked in chat applies only after approval in the web app. |
| 7c | Tool editing in the web app (`save_tool`, `delete_tool`) | `plan_tomorrow` made in the web app shows up in a new Labs chat. |
| 8 | `--write` sign-in, `sched.write_day`, Drive backup, retire Ard's tools | "Write tomorrow" creates blocks in the new Rivia calendar only, after approval; a backup exists off the tablet. |
| 9 | Cloud copy, only if the watchdog log shows the tablet dropping | As in the roadmap. |

7c is the least important part. The scheduler is complete without it; it only saves editing eight rows by hand.

## 17. What a stolen token can do

| Token | Worst case | Cannot |
|---|---|---|
| App | Read plans, ride times and settings; set day-only values inside their ranges; log false rides; fill the pending queue. | Approve anything, change a standing value, write a calendar, edit a tool. |
| Admin | All of the above, plus change standing values inside their ranges, approve writes to the Rivia calendar, rewrite tool descriptions (which can mislead Rivia), restart the server. | Run code, read `.env` or the Google token, touch the main calendar, change a range or an action, mint tokens. |
| Bot (OAuth) | Whatever read-only tools you exposed there. | Everything else. |

A plan reveals where Jordan will be and when, so the app and admin tokens are more sensitive than the battery tool ever was. `rivia revoke` voids all of them. An authenticator code on approve would stop a stolen admin token from approving; it is not in this spec because it adds a step to every approval.

## 18. Open decisions

1. **The ranges in section 7.** They are proposals.
2. **`write_day` through the queue, or direct?** The queue matches "writes wait for a tap" and the README's rule for tablet write tools. Direct is how Ard did it and is lower friction, with Google's scope still keeping it off the main calendar.
3. **Estimates for farm to Planet Fitness and Planet Fitness to work.** Needed before the first weekend shift (risk 1).
4. **What the bot door gets.** Recommended: nothing from the scheduler, or `ride_time` and `plan_day` only.
5. **Maps lookups.** Not in this spec. Without them a ride with no rides and no estimate stops the answer.
6. **A spoken-line check in the app** to replace the deleted verifier.
7. **Admin token lifetime** (7 days proposed).

## 19. Risks and what is unverified

1. **Weekend plans fail today.** `MEASURED_LEGS` has no entry for `farm|pf` or `hmc|pf`. A Saturday or Sunday shift needs both, ride history only helps once it holds three clean rides for each, and nothing on the tablet looks up Maps. Until you enter two estimates, every weekend `plan_day` returns `need_estimate`. The weekend rotation starts late November.
2. **One device at home.** Unchanged from the roadmap: if the tablet is down while you're out, there is no scheduler until step 9. A reboot without an unlock also keeps Termux:Boot from starting it.
3. **Unchecked speech.** With the verifier gone, a wrong time in Gemini's line would be spoken. The planner's facts are right; the sentence built from them is unguarded.
4. **Retry on a slow call.** The app waits 30 seconds, retries once, and can stack three tool rounds with nothing on screen. Section 2 rule 6 makes that harmless on the tablet, but a slow Google call can still mean a long silence.
5. **Not verified on the tablet:** removing a FastMCP tool while running on 4.1.x; Chrome on the tablet reaching the Termux listener on `127.0.0.1`; the Drive upload in step 8; the whole sign-in from a small screen.
6. **Google can end the sign-in** for reasons other than Testing mode, including six months without use and revoking the app's access. The server reports `calendar_login_expired`; it cannot repair it.
7. **Tool descriptions are prompt text.** Anyone who can save a tool can steer Rivia's wording and when she calls it. Only the admin door can, and the fixed lines can't be removed, but it is the widest opening the web app has.

## Sources

- Project: `docs/SCHEDULER-ROADMAP.md`, `grok-bot/rivia_planner.py`, `grok-bot/PAYLOAD.md`, `grok-bot/BOT-INSTRUCTIONS.md`, `parked/tablet-router/` (server, auth, bots, `rivia.sh`, README), `app/REMOVAL-MAP.md`, `app/passes/08-bots-1.md`
- [Google: OAuth 2.0 for desktop apps (loopback redirect, PKCE)](https://developers.google.com/accounts/docs/OAuth2InstalledApp)
- [Google: loopback flow stays supported for Desktop app clients](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration)
- [Google: limited-input device flow and its allowed scopes](https://developers.google.com/identity/protocols/oauth2/limited-input-device)
- [Google Cloud help: Testing status expires authorizations after seven days](https://support.google.com/cloud/answer/15549945)
- [Google Calendar API: scopes](https://developers.google.com/calendar/api/auth)
- [Google Calendar API: Events list](https://developers.google.com/calendar/api/v3/reference/events/list)
- [Google Calendar API: extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties)
- [Google Calendar API: Events insert](https://developers.google.com/calendar/v3/reference/events/insert)
- [Nango: reasons Google revokes refresh tokens](https://www.nango.dev/blog/google-oauth-invalid-grant-token-has-been-expired-or-revoked)
- [FastMCP: tool transformation (`Tool.from_tool`, `ArgTransform`)](https://gofastmcp.com/servers/transforms/tool-transformation.md)
- [FastMCP changelog (4.1.x)](https://gofastmcp.com/docs/changelog.md)
