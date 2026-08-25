# AdNova Player — Changelog

The Player version tracks the AdNova platform release, in lock-step with the
Dashboard and AIAgent. All three are **1.7.0**.

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
