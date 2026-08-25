# Player event codes

The closed vocabulary the device ships to Dashboard on
`POST /api/v1/player/logs`. Shapes are pinned by
`Dashboard/contracts/player_logs.v1.json`.

This is not the journal. `journalctl -u adnova-player` holds the running
commentary and stays on the device; these are the events worth keeping
*off* the device, because the device is the thing that might be
compromised. Keep the list short — a vocabulary that grows to cover
everything is a vocabulary nobody can alert on.

## Severity

| `sev` | Means | Dashboard |
|---|---|---|
| `info` | Routine, audit-only. | `Audit::INFO` |
| `warn` | Worth seeing in context. | `Audit::WARNING` |
| `error` | Something failed. | `Audit::WARNING` |
| `security` | Something an attacker would cause. | `Audit::CRITICAL` + alert |

`security` events are **never** rate-limited on the device. Everything else
is capped at 12 per code per 5 minutes and collapsed into a `+N suppressed`
note — because the point of a flood may be to bury the one event that
matters, and a ring that a flapping fault can fill is a ring an attacker
can empty.

## Codes

### Identity and trust

| Code | Sev | Raised when |
|---|---|---|
| `enroll.introduced` | warn | The device asked to join, with its hardware fingerprint. |
| `enroll.adopted` | security | Dashboard handed it a stand key — the most security-relevant moment in the device's life. |
| `enroll.credentials_written` | security | That key was written to `/etc/adnova-player/env`. |
| `enroll.rejected` | security | Dashboard refused this device. |
| `enroll.closed` | info | Enrollment is not open; the device is waiting. |
| `enroll.failed` | warn | The introduction call returned an error status. |
| `auth.lost` | security | The stand key was rejected past the limit; re-enrolling. |
| `manifest.refused` | security | A plan arrived that could not be proved to be Dashboard's, or was a replay. |
| `manifest.unsigned_accepted` | security | An unsigned plan was accepted because signing is not yet required. Every device in that window should be visible. |
| `service.unverified` | security | No trusted signing keys are provisioned, so no manifest can be verified. |
| `audit.chain_broken` | security | The device's own event chain does not verify — a line on this card was edited or removed. |

### The network

| Code | Sev | Raised when |
|---|---|---|
| `api.auth.rejected` | security | Dashboard answered 401/403. Either the clock drifted past the signing window or the key is wrong. |
| `api.tls.refused` | security | The server certificate did not verify. This is what interception looks like from inside the device. |
| `api.unreachable` | warn | A transport error that was not TLS — the ordinary shop-network case. |
| `api.server_error` | warn | Dashboard answered 5xx. |
| `api.rejected` | warn | Dashboard answered 4xx other than 401/403. |
| `media.checksum_failed` | security | Downloaded bytes did not match the signed manifest's checksum — substituted content. |
| `media.plaintext_refused` | security | A media URL was not https. |

### The local admin page

The only authenticated surface a person standing in the shop can reach.

| Code | Sev | Raised when |
|---|---|---|
| `admin.auth.failed` | security | A wrong username or password. The username is recorded; the password never is. |
| `admin.auth.ok` | warn | Somebody logged in. After an incident the question is who got in, not only who failed. |
| `admin.auth.lockout` | security | An address crossed the failure threshold and was locked out. |
| `admin.auth.locked` | security | A request arrived from an address already locked — the signature of a script rather than a technician. |
| `admin.auth.unconfigured` | warn | The page was reached on a device with no admin password set. |
| `admin.action` | warn | `refetch` or `update` was triggered from the page. |
| `admin.action.rejected` | security | An action name outside the closed set was attempted. |

### Lifecycle and control

| Code | Sev | Raised when |
|---|---|---|
| `service.boot` | warn | The process started, before the config is read. |
| `service.started` | warn | The loops are running. A start with no matching stop is how a crash-loop or a yanked power lead reads afterwards. |
| `service.stopping` / `service.signal` | warn | A clean shutdown, and which signal caused it. |
| `service.config_error` | error | The device cannot start; its provisioning is unusable. |
| `command.executing` | warn | A restart/reboot argv is about to run. Recorded **and shipped** first, because the process will not come back to write its own log line. |
| `command.executed` / `command.failed` | warn / error | A whitelisted operation ran, with its outcome. |
| `command.unknown` | security | Dashboard sent a command name this build has no map entry for. A fleet mid-upgrade and a forged control response look the same from here. |
| `clock.corrected` | warn | The trusted-clock offset moved by 60s or more. |
| `selftest.failed` | warn / error | A boot self-test check failed. |
| `watchdog.stalled` | error | The loops stopped making progress; systemd is about to restart us. Edge-triggered. |

### The safety net

| Code | Sev | Raised when |
|---|---|---|
| `log.<module>` | warn / error | Anything the process logged at WARNING or above, mirrored automatically (see `audit.py`). |

The mirror exists because the list above is one somebody has to keep
complete, and the incident worth reviewing is the one nobody anticipated. A
contributor who adds a `log.error(...)` and never thinks about auditing
still leaves a trace at Dashboard.

## What makes this evidence

Three properties, and they only work together:

**Ordered.** `seq` is monotonic for the life of the `boot_id` and survives
restarts, drained rings and re-installs. Dashboard remembers the highest it
has accepted per stand.

**Chained.** Each event's `hash` commits to the one before. Editing a
recorded event breaks every hash after it.

**Kept twice.** The ring is a shipping queue and empties on ack;
`events-archive.jsonl` is the local copy, written at record time and
rotated by size.

So a compromised device can delete its own events, but it cannot make the
numbers add up afterwards. The next shipment arrives with a gap and
Dashboard raises `player.audit.gap`; a chain that goes backwards under the
same `boot_id` raises `player.audit.rewound`. A genuinely re-imaged device
starts a new `boot_id` at seq 1, which raises the much calmer
`player.audit.chain_restarted`.

The one attack this does not catch is deleting the tail *and* never letting
the device talk again — at which point the silence is the alarm, and
fleet-health alerting already covers it.

## Reading the trail on-site

`https://<device>:8080/admin/events`, behind the admin login. Shows the
archive newest-first and re-walks the chain, so a tampered card announces
itself even when the device cannot reach Dashboard to ship anything.
