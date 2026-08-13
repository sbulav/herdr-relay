# herdr-remote → herdr-relay roadmap

Premise: this repo has exactly one production consumer — the Android app in
`sbulav/herdr-mobile`, over `wss://herdr-relay.sbulav.ru/native/ws`. Upstream
(`dcolinmorgan/herdr-remote`) is no longer a merge target. Everything below is
graded by one question: **does it make the relay a more reliable server for
herdr-mobile?**

Current state: `relay/herdr_relay.py` is 1319 lines vs upstream's 603. The
+888-line delta (`presets`, `hosts`, `launch_session`, `terminate_session`,
`wake_host`, `shutdown_host`, `subscribe_pane`, `output_blocks`, concurrent
remote polling, structured transcript parsing) exists only here. Upstream has
zero occurrences of any of those message types. The fork *is* the product.

---

## Phase 0 — Two hotfixes, then cut the cord

### 0a. Close the fail-open auth hole (today, not scheduled)

`if AUTH_TOKEN:` (`herdr_relay.py:871`) gates the WebSocket handshake. An
empty or unset `HERDR_RELAY_TOKEN` therefore disables authentication on a
public-facing socket that can drive a real terminal and run `shutdown_host`
over SSH. Production does set the token (SOPS-managed), so this is a
misconfiguration cliff rather than a live breach — but the blast radius of one
bad deploy is remote code execution, and the fix is small:

```python
if not AUTH_TOKEN:
    raise SystemExit("HERDR_RELAY_TOKEN is required")
...
if not hmac.compare_digest(token or "", AUTH_TOKEN):
```

Refuse to start without a token; compare in constant time. Ship before
anything else in this document.

### 0b. Stop blocking the event loop on every command

The poll path is correctly off-thread (`asyncio.to_thread` at `:303`, `:317`,
`:808`, `:1065`, `:1090`). The **command** path is not. `launch_session`
(`:1005`), `terminate_session` (`:1012`), `wake_host` (`:1019`),
`shutdown_host` (`:1026`), and the `respond` / `read_pane` / `send_keys` /
`send_text` branches all call `run_herdr` → `subprocess.run(..., timeout=15)`
synchronously inside the async handler. One phone tapping "shutdown a host" stalls
the entire relay — poll loop, broadcasts, every other client — for up to 15
seconds, which reads on the phone as the relay having died. Wrap each in
`asyncio.to_thread`. Roughly ten lines.

(Timeouts themselves are fine and need no work: `ConnectTimeout=5`,
`ServerAliveInterval=3`, `ServerAliveCountMax=2`, `BatchMode=yes`, and a hard
`timeout=15` on every `subprocess.run`.)

### 0c. Cut the cord

Stop paying merge tax and delete what no client runs.

**Declare the hard fork.** Drop the `upstream` remote. Retain AGPL attribution
in `LICENSE` and a one-line origin note in `README.md`; nothing else about
upstream stays.

**Delete unused clients.** None of these are consumed by herdr-mobile:

| Path | Lines | Status |
|---|---|---|
| `herdi-mac/` | Swift app | delete |
| `herdi-ios/` | Swift app | delete |
| `demo-worker/` | CF Worker mock | delete |
| `relay/herdr_tui.py` | 194 | delete |
| `relay/herdr_telegram_demo.py` | 160 | delete |
| `relay/herdr_telegram.py` | 560 | **decide** — keep only if you still run the bot |
| `web/` + `public/` | single-file PWA | **keep, conditionally** — see below |
| `.pi-kiro-auth.js` | 2.9 KB stray | delete |

`web/` is *not* free to delete. `herdr-mobile` still ships a WebView fallback
mode pointing at `https://herdr.sbulav.ru` (`ui/HerdrApp.kt:282`, `:427-478`;
allowlisted in `data/SettingsStore.kt:312`), and the relay is what serves that
page (`herdr_relay.py:905-948`). Deleting `web/` is a **cross-repo change**:
remove the WebView mode from the app first, then drop `web/`, `public/`, the
four static HTTP routes, and the Web Push stack (`_load_push_subs`,
`send_web_push`, `push_subscribe`/`push_unsubscribe`, the four `HERDR_VAPID_*`
vars) — mobile uses foreground WS monitoring, never Web Push. Until the app
change lands, keep all of it and mark it `LEGACY` in comments.

**Retarget the test suite.** `tests/run.sh` is 17 assertions of upstream
marketing: it asserts the macOS updater points at `dcolinmorgan/herdr-remote`
(#13), that `README.md` links `herdr-demo.pages.dev` (#15) and
`dcolinmorgan/herdr-push` (#16), and greps `web/index.html` for `sendKey`
(#9). After the deletions most of it references files that no longer exist.
Delete `tests/run.sh` outright; `tests/test_structured_output.py` (549 lines,
fork-authored, 30 real unit tests) becomes the whole suite. Reduce `Makefile`
to `check = lint + unittest`; drop `relay-install`, `relay-plugin`,
`ios-build`.

**Rename.** `herdr-remote` no longer describes it. `herdr-relay` matches the
deployed hostname and the one thing the repo now is.

*Exit criteria:* `make check` green with zero references to upstream, mac,
ios, tui, or demo-worker. Repo is relay + (conditionally) web + tests.

---

## Phase 1 — Make the wire contract executable

**This is the highest-value phase and should not be reordered behind
refactoring.** Today nothing connects the two repos. `herdr-mobile` has 20
JSON fixtures in `protocol-fixtures/` describing frames it will parse;
`herdr-remote` has unit tests for parsing internals. Nothing asserts the relay
*emits* what the app parses. Every contract break is currently discovered on
the phone.

1. **Publish the native contract from the relay side.** `protocol-fixtures/`
   documents protocol v1, but production speaks the *native* dialect
   (`agents` / `blocked` / `pane_content`, `pane_id`-keyed) via
   `protocol/LegacyProtocol.kt`. That dialect is documented nowhere. Write
   `docs/native-protocol.md` from `handle_client` (`herdr_relay.py:966-1157`)
   and `_poll_once` (`:743`): every `msg_type`, required fields, ack shape,
   error codes from `command_error` (`:1159`).

2. **Golden frames, generated not hand-written.** Add
   `tests/test_native_contract.py`: drive `_poll_once` and each command
   handler against fakes, snapshot the emitted JSON to
   `contract/native/*.json`. These are the relay's *output*.

3. **Cross-repo check — the cheap version.** Copy `contract/native/*.json`
   into herdr-mobile by hand and assert `LegacyProtocol.decode` parses each
   one into the expected model — the mirror of the existing
   `tools/test_protocol_fixtures.py`. The relay is the contract's home; the
   app follows. Deliberately *not* doing: pinned-SHA drift detection or
   cross-repo CI wiring. One developer with two terminals does not need
   machinery to notice they changed a frame; the value is entirely in the app
   having a test that fails on a real frame, and that is one `cp` plus one
   test file.

4. **Cover the mobile-critical paths that have no tests today.** Existing
   tests cover host power, chrome filtering, transcript mapping, and option
   detection. There is **no** test that the `agents` broadcast carries
   `presets` and `hosts`, and none for `launch_session` /
   `terminate_session` ack or error shapes — precisely the frames that gate
   every lifecycle control in the app.

*Exit criteria:* a breaking change to the `agents` frame fails CI in both
repos before it reaches a device.

---

## Phase 1b — Cross-repo moves

Everything above was written as if `herdr-mobile` were fixed. It is not — both
repos are yours, one user, one device, and you control when the APK ships.
That unlocks four moves that are unavailable to a relay-only plan, and the
first one is worth more than Phases 2, 3, and 6 combined.

### 1. Push structure into the relay and delete the terminal scraper

`ui/OutputReader.kt` is **2226 lines** — by a wide margin the largest file in
the Android app — and its entire job is to reverse-engineer structure out of
PTY-rendered text: re-wrapping hard-wrapped prose, guessing pane width,
detecting code fences, absorbing continuation lines. The relay meanwhile
*already* extracts real structure from the agents' own session stores
(`transcript_to_blocks`, `opencode_to_blocks`, `herdr_relay.py:463-705`) and
ships it as `output_blocks`. The app prefers blocks and falls back to scraping
(`OutputReader.kt:173` — `parsed.ifEmpty { parseRawOutput(...) }`).

So the scraper is a *fallback that runs too often*. `pane_blocks`
(`:680-703`) refuses structured output whenever the agent is not
`claude`/`opencode`, the `cwd` is unknown, or two live same-agent panes share
a `cwd` — and its own comment names the cause: *"Without an agent session
id/path, cwd is the only correlation available."*

The fix is on the relay side: correlate panes to transcripts by session
id/path rather than by `cwd`, and the ambiguity refusal mostly disappears.
Blocks become the normal case, and the 2226-line scraper degrades to a thin
last-resort path for unsupported agents.

**This deletes work from both roadmaps.** The mobile roadmap's Phase 1
(`../ROADMAP.md`) — pane-width detection, a paragraph re-flow engine,
indent-depth continuation tracking — is an elaborate effort to undo damage the
relay inflicts by shipping terminal-rendered text. Do not build it. Widen
block coverage instead, and most of that phase evaporates rather than getting
implemented. Its item 1 already concedes the point: *"Longer term: add an
optional `pane_width` field … so the relay just tells us."* Go further — if
the relay sends blocks, the phone never needs the wrap column at all.

### 2. Emit `attention_state` — the relay knows, the phone guesses

`LegacyProtocol.kt:139` reads `attention_state` on every frame, and
`protocol-fixtures/README.md` specifies it precisely (`working` / `waiting` /
`done` / `idle`, where `waiting` means waiting *on the user*). The relay emits
it **nowhere** — zero occurrences in `herdr_relay.py`. So the app falls back
to inferring "does this need me?" from `status` strings, which is exactly the
inference the field exists to remove.

The relay is where the truth lives: it already detects `blocked` transitions
in `_poll_once` (`:773-776`) and holds the previous status in
`last_statuses`. Emitting `attention_state`, `updated_at`, and
`output_revision` is a small relay change that deletes client-side guesswork
and makes notification decisions correct rather than heuristic. Fold into
Phase 1's contract work — these are additive fields on frames you are already
snapshotting.

### 3. Stop designing for back-compat you don't need

The mobile fixtures carefully pin old-relay behaviour
(`reconnect-snapshot.json` "deliberately omits all three to pin old-relay
behaviour"), and the protocol README is full of *"clients must fall back
when…"*. That discipline is correct for a public protocol with unknown
clients. You have one client, one user, and you control both deploys.

Replace the fallback ceremony with a **version handshake**: relay advertises
`min_client`, app shows a blocking "update required" screen below it. Then
breaking changes cost one coordinated release instead of a permanent
compatibility branch in both codebases. Keep additive-and-ignorable as the
default for convenience, not as a constraint.

This also changes the Phase 5 calculus: opaque session IDs are cheap if you
are allowed to break the wire.

### 4. Retire the WebView fallback on a date

Phase 0c leaves `web/` alive because the app embeds it. With both sides in
play this stops being conditional: delete the WebView mode from
`ui/HerdrApp.kt` (`:282`, `:427-478`) and the `herdr.sbulav.ru` allowance in
`data/SettingsStore.kt:312`, ship that APK, then in the relay drop `web/`,
`public/`, the four static routes (`:905-948`), and the entire Web Push stack.
Two commits, one release apart.

### Considered and rejected

**FCM push instead of a permanent socket.** `MonitoringService` holds a
foreground service and a live WebSocket for as long as monitoring is on; there
is no Firebase dependency in the project. On a BOOX with App Freeze that is
the documented-fragile path. Server-pushed FCM data messages on
`blocked`/dialog would fix the battery and freeze story properly. Rejected for
now anyway: it adds a Google dependency, a token-registration endpoint, and a
second delivery path to keep correct, in exchange for a problem you currently
work around with two device settings. Revisit only if monitoring actually
proves unreliable in daily use.

**Generating the Kotlin models from the relay's schema.** Tempting with both
repos in hand; not worth a code generator for ~15 message types and one
developer. Phase 1's golden frames give you the safety without the build step.

---

## Phase 2 — Restructure the relay for change

`herdr_relay.py` is one 1319-line module with ~15 module-level mutable
dicts/sets (`clients`, `pane_remote_map`, `session_target_map`,
`subscriptions`, `stream_sigs`, `pane_cwd_map`, `request_results`, …). Tests
already reach for `unittest.mock.patch` on module globals. Split, keeping
behaviour byte-identical and Phase 1's golden frames as the safety net:

```
relay/
  herdr_relay/
    __init__.py
    config.py       # env vars, currently :42-70
    herdr.py        # run_herdr, get_agents_from_host, get_all_agents, read_pane
    panes.py        # detect_options, respond_action/text, chrome filtering
    transcripts/    # claude.py, opencode.py, blocks.py  (:463-705)
    lifecycle.py    # launch/terminate/wake/shutdown     (:1159-1257)
    protocol.py     # frame builders — the only place JSON shapes are written
    server.py       # handle_client, process_request, poll_loop
    state.py        # the mutable maps, behind one object
```

Ordering note: do Phase 1 first. Refactoring without executable golden frames
is how a silent wire change ships.

Two things to kill during the split, both dead for mobile: mDNS/zeroconf
(`start_mdns`, `:1266`) advertises LAN discovery no client uses, and the UDP
plugin listener (`UDPPlugin`, `:1258`) duplicates the HTTP event path. Drop
`zeroconf` from `flake.nix` and the PEP 723 block once confirmed.

---

## Phase 3 — Harden the public edge

With 0a shipped, the remaining edge work is real but not urgent:

- **Token accepted as a query parameter** (`:877-881`), so it lands in
  reverse-proxy access logs and Cloudflare analytics. Mobile sends a header;
  the query path exists only for the browser PWA. Retire it with `web/`.
- **One token for all callers.** No per-device identity, no revocation short
  of rotating for everyone. Move to per-device tokens with a `kid`, so losing
  a phone revokes one credential. This also makes `audit()` (`:117`) name a
  device rather than an IP.
- **No rate limiting** on `respond` / `send_keys` / `send_text`, which drive a
  real terminal. Add a per-connection token bucket.
- **Blast radius.** `shutdown_host` runs SSH against an allowlisted host, and
  `launch_session` spawns agents. These are correctly allowlisted and
  confirmation-gated (`HostPowerTests` covers this) — keep that property
  under the Phase 2 split; it is the one place a refactor regression is
  genuinely dangerous.

---

## Phase 4 — Own the deployment

Deployment currently lives outside the repo, and one critical piece is
undocumented: **the relay has no `/native/ws` route.** It serves WebSocket on
`/`. The public `wss://herdr-relay.sbulav.ru/native/ws` only works because a
reverse proxy on zanoza rewrites it — config that exists in no repo. If that
box is rebuilt from scratch, the app cannot connect and nothing in either
repo says why.

1. Make the flake produce a real package, not just a devShell. Today
   `flake.nix` gives `devShells` + a `checks.relay` that runs `make check`; add
   `packages.herdr-relay`.
2. Ship a NixOS module: systemd unit, SOPS-managed token, `HERDR_REMOTES`,
   `HERDR_HOSTS_FILE`, hardening (`DynamicUser`, `ProtectSystem=strict`).
   (`HERDR_PRESETS_FILE` and `HERDR_POWER_HOST_*` were named here when this was
   written; #45 retired all three, and the host file replaced them.)
3. Commit the reverse-proxy config, including the `/native/ws` rewrite.
4. Then delete `relay/install-service.sh` (31 KB of launchd/systemd/brew
   bootstrap) and `relay/start.sh` (cloudflared tunnel orchestration). Both
   describe a laptop-and-Cloudflare-tunnel deployment you no longer run.
5. Pin `nixpkgs` in `flake.nix` — it currently tracks `nixos-unstable`, so the
   relay's Python and `websockets` version drift with every `nix flake update`.

---

## Phase 5 — Decide protocol v1, in writing

**Decided: v1 is deleted.** The native dialect in
[`docs/native-protocol.md`](docs/native-protocol.md) is *the* protocol. There
is no v1, no v1 migration, and no dual-dialect client. Recorded here and in the
protocol document itself so it does not have to be re-argued; the deletion is
sbulav/herdr-relay#17 and sbulav/herdr-mobile#33.

The problem it settled: `herdr-mobile` carried a full v1 implementation
(`protocol/Protocol.kt`) plus ~19 fixtures for a protocol **the relay does not
speak** — v1 used `snapshot` / opaque `session_id` / `dialog_id` + `revision`;
the relay emits none of those. The app's own README called it "not deployed
yet." Dead weight in one repo and an unbuilt obligation in the other.

For the record, the two options were:

- **Build it.** v1 was genuinely nicer on paper than the native dialect:
  opaque session IDs instead of `pane_id` (which changes when tmux renumbers),
  idempotent request IDs, monotonic `revision` for dialog races,
  `attention_state` so the phone stops re-deriving "does this need me" from
  `status` strings. The relay-side gap was a session-identity layer and
  per-session revision counters.
- **Delete it.** Remove the v1 codec and fixtures from herdr-mobile and
  promote the native dialect to *the* protocol with the Phase 1 docs.

**Why delete won.** Two of v1's four real advantages arrived without it:
Phase 1b.2 shipped `attention_state` and `output_revision` on native `agents`
and `pane_content` frames, additively, and the native lifecycle commands
already carry `request_id` for idempotency and ack correlation. What remained
exclusive to v1 was opaque session identity and per-dialog `revision` — one
deferrable, one solving a race that has not been observed. A solo developer
should not carry a second protocol implementation for a migration with no date.

The one caveat worth naming, because it is the strongest argument for the
other choice: `pane_id` is tmux-assigned and changes when tmux renumbers, so
session identity in the native dialect is not stable across a pane restart.
That is accepted, not solved. If it bites in practice, the fix is an opaque
session-ID layer — v1's core idea, addable to the native dialect on its own,
without adopting the rest of v1. With the Phase 1b.3 `min_client` handshake in
place that is a cheap breaking change rather than a migration, which is
precisely what makes deleting v1 safe rather than merely cheaper.

Note what this drops from the mobile roadmap (`../ROADMAP.md` Phase 1): an
additive `pane_width` field on `session_output`. There is no `session_output`
frame, and per Phase 1b.1, if the relay ships blocks instead of rendered text
the phone never needs the wrap column. Dropped rather than moved.

---

## Phase 6 — Reduce polling cost

`POLL_INTERVAL = 2` (`:37`) drives a full `herdr` invocation per host every
two seconds — over SSH for every entry in `HERDR_REMOTES` — plus a
`TRANSCRIPT_MAX_BYTES = 262144` tail read per subscribed pane. Concurrency
already landed (`test_hosts_are_polled_concurrently`), so this is now about
volume, not latency:

- Persistent SSH `ControlMaster` sockets instead of per-poll connections.
- Adaptive interval: back off toward 10 s when every session is idle and no
  client is subscribed; snap to 2 s on subscribe or on any push event.
- Send snapshot deltas rather than the full agent list every 2 s — but only
  after Phase 1, and only as an additive frame the app may ignore.
- Feed the existing HTTP/UDP push path from a herdr hook so state changes
  arrive on an edge instead of being discovered by the next poll.

---

## Suggested order

**0a, 0b** (hours) → **0c** (a weekend) → **1** → **1b** → **4** → **2** →
**5** → **3** → **6**.

Phase 1b.1 (widen block coverage, delete the scraper) is the highest
value-per-line item in either repo and the only one that removes a planned
phase from the mobile roadmap. If time runs short, do 0, 1, and 1b.1 and stop.

Phase 4 moves ahead of Phase 2 deliberately. Restructuring the relay into nine
modules while the production route (`/native/ws`) exists only as an
undocumented proxy rewrite on one machine means the first deploy of the
refactored code is a blind roll — you would be unable to distinguish "the
split broke a frame" from "the proxy was never in the repo." Codify the
deployment, reproduce the real routing locally, *then* refactor against it.

Phases 0 and 1 are worth doing regardless of every later decision. Phase 2 is
the only phase that is purely optional: 1319 lines in one file is unpleasant
but not dangerous, and if the appetite runs out after Phase 4, stopping there
leaves the relay in a genuinely good state.

---

*Reviewed against an independent critique (Gemini 3.1 Pro) on 2026-07-31. It
correctly flagged the fail-open auth as mis-scheduled, the Phase 4/2 ordering,
and the cross-repo CI as over-engineered for one developer — all folded in
above. Its claim that SSH timeout and zombie-process handling were missing was
checked and rejected: every `subprocess.run` already carries `timeout=15` with
`ConnectTimeout=5` and SSH keepalives. Checking that claim is what surfaced
0b, the blocking-command-handler bug, which the critique missed. Phase 1b was
added after that review and has not been independently critiqued.*
