# AdNova Player — Changelog

The Player version tracks the AdNova platform release, in lock-step with the
Dashboard and AIAgent. All three are **1.8.2**.

## 1.8.2 — 2026-08-26

Stop sending synthetic slot ids.

The player marks its own invented items with negative sentinels: -1 for the
bundled fallback loop, -2 for a test broadcast and for an emergency takeover.
None is a row in any table, and the heartbeat was putting -2 into
`current_slot_id`, a column Dashboard declares unsigned.

MySQL refused the value, the write threw, and **the entire heartbeat returned
500** — which meant the command channel never ran. For as long as a test
broadcast or a takeover was on screen, no queued command was ever handed to
the device. From Dashboard it looked exactly like a stand ignoring every
button; the device was healthy and had simply never been told.

Production logs show this firing once a minute, per beat, going back to
2026-08-16.

Only a real slot id is sent now. `state` and `test_active` already carry what
is actually on screen, which is what an operator was reading anyway.

## 1.8.1 — 2026-08-26

The actual cause, found on the device.

### The version the fleet reports is now the version it runs

`__version__` is a literal in `__init__.py` and pyproject.toml was bumped
without it, so devices running 1.8.x kept reporting 1.7.0 — the string the
heartbeat carries and the fleet view uses to flag a stale stand. An operator
looking at a stand that had already updated could not tell it apart from one
that had not. `tests/test_version.py` now fails the build if the two drift,
which is what makes keeping the literal safe: the venv is an editable install,
so an importlib.metadata lookup would only refresh when pip re-runs, and a
value read on every heartbeat must not depend on that.

1.8.0 hardened the display launcher against a failure that turned out not to
be the one that had taken a stand dark. Running the real diagnosis on the Pi
produced a better answer, and it is worse than the theory:

**A retired `adnova-kiosk.service` was still installed and enabled.** The
display was a system unit once, and system units run as the service account.
That account owns no graphical session, so every start died immediately — but
not before creating `/tmp/adnova-kiosk.lock`, owned by `adnova`, mode 0644.

From that moment the *real* launcher, running as the desktop user under its
own Wayland session, could not open that file. `exec 9>` failed, `set -e`
ended the shell on the redirection error, and the panel stayed black through
every reboot with nothing written down anywhere. A zero-byte file left by an
already-broken service held a stand dark indefinitely, while its heartbeats
kept Dashboard's dot green.

### Every path this script owns is now per-uid

A fixed name under `/tmp` is a cross-user landmine: whichever account creates
the file first can lock every other account out of it permanently. The lock
moves to `$XDG_RUNTIME_DIR` — per-user and per-boot by construction, which is
exactly the scope of "one display for this session" — and the launcher log,
which the player service must be able to read and so cannot live there, gains
the uid in its filename. Same for the helper's lock and its `.done` markers.
`diagnostics.kiosk_log_tail` now globs and reads the most recently written.

### Opening the lock is allowed to fail

It is the failure that cost a stand its screen, so it is now handled rather
than fatal: log it and carry on without the lock. A second display is a
visible, fixable annoyance; no display is an outage. The `flock` check is
skipped entirely when no real lock was obtained — falling back to `/dev/null`
would risk reading "another launcher owns the screen" and exiting, turning
the missing lock straight back into the black panel.

### The retired unit is removed fleet-wide

`setup-kiosk.sh` removed it, but only on devices that were re-run through it.
`provision.sh` and `adnova-update.sh` now both do, on every ops change — the
only path that reaches a Pi nobody is standing next to. The stale lock files
go with it.

### `StartLimitIntervalSec` was never in force

It sat in `[Service]`, where systemd ignores it, and said so in the journal on
every single start:

    Unknown key 'StartLimitIntervalSec' in section [Service], ignoring.

The key belongs to `[Unit]`. So the "never give up restarting" guarantee the
unit documented was not actually applied — systemd's default rate limit would
have stopped after five failures in ten seconds and left the stand dead until
somebody visited it.

## 1.8.0 — 2026-08-26

Something had to watch the screen.

Every safety net in this player guarded the brain. `Restart=always`, the
systemd watchdog, the board's hardware watchdog — three layers, all around the
Python process. The display stack that actually puts pixels on the panel
starts from a desktop autostart hook, has no systemd unit, and had none of
them. A stand could therefore sit dark for weeks while its heartbeats kept
arriving and Dashboard showed it green, and nothing anywhere would notice.

That is not hypothetical: it happened, and the cause was one unguarded line.

### The launcher can no longer die in silence

`ops/adnova-kiosk.sh` ran under `set -euo pipefail` with an unguarded
`sink="$(wpctl status | grep -i hdmi | ...)"`. A stand with no HDMI audio sink
at autostart — an ordinary race with PipeWire — made that `grep` return 1,
`pipefail` handed it to the assignment, and `set -e` killed the whole script
before the helper, the screenshot uploader and mpv were ever started. No error
reached anywhere, because nothing was watching and nothing was logged.

Three changes, in order of how much they matter:

- **`set -e` is gone**, deliberately. This launcher's one job is to reach mpv;
  every step before it is a best-effort tweak that must never be able to stop
  the screen from coming up. The failures that do matter are handled where
  they happen.
- **It logs.** `/tmp/adnova-kiosk-launch.log` records every start, every
  skipped tweak, and every exit with its status. The player's hardened view of
  `/tmp` is read-only rather than private, so it can read that file and ship
  the tail of it in the diagnostics bundle.
- **It supervises the driver** instead of `exec`-ing into it, with a backoff
  that distinguishes a crash after an hour from a release that cannot start at
  all. This is the `Restart=always` the display stack never had.

A lock held with no driver behind it — a wedged launcher from an earlier
session — is now named as such in the log, instead of looking identical to
the ordinary duplicate-autostart case.

### The device notices its own dark screen

`Agent._check_display_alive` runs on each heartbeat. It judges the panel only
when something should be on it — inside operating hours, with a plan loaded —
and it checks the driver's snapshot for freshness, not just its `playing`
flag: a driver that died leaves its last frame behind saying `playing: true`
forever.

Two escalations, deliberately far apart in consequence. After
`display_stale_restart_seconds` (10 min) it asks the in-session helper to
relaunch the display, which is a file write and costs nothing if it was a
false alarm. After `display_stale_reboot_seconds` (30 min) it reboots the
board — a real intervention on a customer's premises, so that step is a fleet
flag (`ADNOVA_DISPLAY_WATCHDOG_REBOOT`) that defaults **off** and fires at
most once per process. A watchdog that can reboot-loop a stand is worse than
the fault it was meant to fix.

### `diagnostics`: the safe answer to "let me SSH in and look"

A Pi sits behind a shop's NAT with no inbound route, so nothing can dial in to
examine it. The new command has the device examine itself and post the result
to `POST /api/v1/player/diagnostics`: which parts of the display stack are
running, the tail of the launcher log, what is really on the panel, and the
health snapshot. Redacted by construction, as the bundle always was — keys
never travel, only whether they are present.

Which of the in-session processes are alive is the single most useful fact
about a misbehaving stand, and until now the only way to learn it was to stand
next to the device.

### `local_ip` is finally sent

The heartbeat contract has specified this field all along; the device never
sent it and Dashboard never stored it. So the one address that lets somebody
reach a stand was known only for the few minutes between enrollment and
adoption, and then lost. `health.local_ip()` now owns it for both callers.

### `shutdown` works for the first time

The player has always issued `sudo -n systemctl poweroff` for it, and that
invocation was missing from the sudoers rule, so the command could not have
worked in any release. The rule now lives in one file —
`ops/sudoers-adnova-player.template` — rather than as a literal string copied
into both `provision.sh` and `adnova-update.sh`, which is exactly how the two
drifted apart in the first place.

## 1.7.0 — 2026-08-25

The device can now account for itself.

A player sits unattended in a shop, on a network nobody here controls, behind
a door anybody can walk through. If it is ever tampered with, the only witness
is the device — and before this release that witness recorded three things:
a refused manifest, a command run, and a lost key. Everything else lived in
the journal, which stays on the card that the attacker is holding.

### The forensic trail is now evidence

`event_log.py` rewritten around three properties that only work together:

- **Ordered** — a monotonic `seq` that survives reboots, drained rings and
  re-installs. Dashboard remembers the highest it has accepted per stand, so
  deleting events from the device does not delete them from the story: the
  next shipment arrives with a gap, and a gap is itself an alarm.
- **Chained** — each event's `hash` commits to the previous one. Editing a
  line breaks every hash after it, detectable on the device itself via
  `verify()` (run from the boot self-test).
- **Kept twice** — the ring is a shipping queue and empties on ack;
  `events-archive.jsonl` is the local copy, written at record time and
  rotated by size, so a technician in front of an offline device can read it.

Plus **flood control**: ordinary codes are capped per window and collapsed
into a `+N suppressed` note. `security` events are never suppressed — the
point of a flood may be to bury the one that matters.

Credentials are redacted before anything is written. (The first version of
that redactor let `stand_key=` through, because "key" in `stand_key` is not on
a word boundary; the test that caught it is in `tests/test_event_chain.py`.)

### The local admin page was the real hole

It accepted unlimited password guesses and left no trace of any of them — the
one authenticated surface a person standing in the shop can reach.

- Failed and **successful** logins are both recorded. After an incident the
  question is who got in, not only who failed.
- Five failures locks the address out for five minutes. While locked, even the
  correct password is refused, so an attacker never learns they found it.
- Actions (`refetch`, `update`) are recorded with their outcome; an action
  name outside the closed set raises a security event.
- New `GET /admin/events` and a section on the page showing the trail and the
  chain check — most needed exactly when the device cannot reach Dashboard.

### Instrumented everywhere else

Enrollment and adoption, TLS refusals (told apart from ordinary network
failures — a certificate that stopped verifying is what interception looks
like from in here), auth rejections, media checksum mismatches, unsigned
manifests accepted during the signing rollout, unknown commands, clock
corrections over 60s, self-test failures, watchdog stalls, and service
start/stop/signal. A reboot command is recorded **and shipped** before the
argv runs, because the process will not come back to write its own log line.

Under all of it, `audit.py` mirrors every WARNING-or-worse into the trail
automatically, so a contributor who adds a `log.error(...)` and never thinks
about auditing still leaves a trace at Dashboard.

Full vocabulary: `docs/EVENT-CODES.md`.

### Notes

- The shipment payload gained `boot_id`, `sequence`, and per-event
  `seq`/`prev`/`hash`. All optional by contract — a 1.6.0 device sends none
  of them and keeps reporting unchanged.
- A 1.6.0 `events.json` (a bare list, no chain) is read on upgrade and chained
  onward rather than discarded.
- 236 pytest pass. Player still has no CI workflow — run `python -m pytest`
  and `ruff check src/` locally.

## 1.6.0 — 2026-08-19

Verified-capture producer (#8/#9/#11): with `ADNOVA_VERIFY_CAPTURE` on
(default off), a fully-played slot attaches a screenshot sha256 to its
playback log as billing-grade proof-of-render.

## 1.5.0 — 2026-08-19

The twenty Phase-21 features: outcome logging / no-bill, decode-probe,
delete-after-play + disk budget, offline→default, live test resume, freeze
watchdog, fallback ladder, gapless core, verified display in heartbeat,
trusted-clock correction, TLS enforce, auto re-enroll, SD armor, diagnostics
+ boot self-test, remote screen/shutdown commands, scheduled and synchronised
takeover.
