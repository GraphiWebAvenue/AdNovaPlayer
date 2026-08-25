"""
The on-site status page, behind a login.

A technician standing next to a misbehaving Pi needs to see what it thinks
it is doing — which stand it is, whether it has a plan, when it last
reached Dashboard, the temperature, the recent log — without carrying a
laptop with SSH keys. This serves exactly that, on the same local server,
behind a username and password so a customer on the shop wifi cannot read
it.

It is deliberately small and read-mostly. The playback view needs no login
because it shows only what Dashboard already sent and is bound to
loopback; this page can expose a little more (health, identity, logs), so
it gets the lock. The password is checked in constant time and stored only
as a hash — a device on a customer's premises must not carry a readable
password even for itself.

Nothing here can change the schedule or touch a credential. The most it
offers is a "check for updates now" and a "refetch" — operations the
device already does on its own, exposed so a technician need not wait for
the next timer.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import event_log
from .config import Config

log = logging.getLogger("adnova.admin")

_security = HTTPBasic(realm="AdNova Player")

# Failed logins allowed from one address before it is locked out, and for how
# long. This page is the only authenticated surface a person standing in the
# shop can reach, so an unlimited guess rate against a hand-typed password is
# the realistic attack. Small numbers: a technician who mistypes twice waits a
# minute; somebody working through a word list gets nowhere.
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 300


class _Lockout:
    """
    Failed-login counters, per source address, in memory.

    In memory on purpose: a restart clearing the counters is not a weakness
    worth a disk write on an SD card, because a restart is not something an
    attacker on the shop wifi can cause. What matters is that the count and
    the lock are *recorded* — the event log is the durable half.
    """

    def __init__(self, clock=None) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}
        self._now = clock or time.monotonic

    def locked_for(self, who: str) -> float:
        """Seconds remaining on the lock, or 0 when the address may try."""
        with self._lock:
            count, last = self._fails.get(who, (0, 0.0))
            if count < LOCKOUT_THRESHOLD:
                return 0.0
            remaining = LOCKOUT_SECONDS - (self._now() - last)
            if remaining <= 0:
                del self._fails[who]
                return 0.0
            return remaining

    def failed(self, who: str) -> int:
        """Count one failure. Returns the running total for this address."""
        with self._lock:
            count, _ = self._fails.get(who, (0, 0.0))
            count += 1
            self._fails[who] = (count, self._now())
            return count

    def passed(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)


def _who(request: Request) -> str:
    """The source address, as far as we can tell. Only ever used as a key."""
    client = request.client
    return client.host if client else "unknown"


def hash_password(password: str) -> str:
    """A salted PBKDF2 hash, stored in the env at provisioning."""
    import hashlib

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored PBKDF2 hash."""
    import hashlib

    try:
        algo, iterations, salt, expected = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


def attach_admin(
    app: FastAPI,
    config: Config,
    status: Callable[[], dict],
    on_action: Callable[[str], bool] | None = None,
) -> None:
    """
    Mount the admin routes on the existing local app.

    `status` returns the live snapshot the page shows. `on_action` runs a
    named operation (refetch, update) and reports whether it was accepted —
    kept as a callback so this module stays free of the agent's internals.
    """

    lockout = _Lockout()

    def _require_login(
        request: Request,
        credentials: HTTPBasicCredentials = Depends(_security),
    ) -> None:
        # An unconfigured password locks the page entirely rather than
        # leaving it open — the safe direction for a page that shows the
        # device's identity.
        if not config.admin_password_hash:
            event_log.record(
                "admin.auth.unconfigured", "warn",
                "Someone reached the admin page on a device with no password set.",
            )
            raise HTTPException(status_code=503, detail="Admin access is not configured.")

        who = _who(request)

        remaining = lockout.locked_for(who)
        if remaining > 0:
            # Recorded every time, not just on the lock: a caller that keeps
            # hammering a locked address is the signature of a script, and the
            # count at Dashboard is what makes that visible.
            event_log.record(
                "admin.auth.locked", "security",
                f"Refused a login from {who} — locked for another {int(remaining)}s.",
            )
            raise HTTPException(
                status_code=429,
                detail="Too many failed logins. Try again later.",
                headers={"Retry-After": str(int(remaining))},
            )

        user_ok = hmac.compare_digest(credentials.username, config.admin_user)
        pass_ok = verify_password(credentials.password, config.admin_password_hash)
        if not (user_ok and pass_ok):
            count = lockout.failed(who)
            # The username is recorded, the password never is — knowing which
            # account was guessed is the useful half, and the guess itself is
            # the half that must not end up in a log at Dashboard.
            log.warning("Failed admin login from %s (attempt %d).", who, count)
            event_log.record(
                "admin.auth.failed", "security",
                f"Failed login from {who} as {credentials.username[:32]!r} (attempt {count}).",
            )
            if count >= LOCKOUT_THRESHOLD:
                event_log.record(
                    "admin.auth.lockout", "security",
                    f"{who} locked out for {LOCKOUT_SECONDS}s after {count} failed logins.",
                )
            raise HTTPException(
                status_code=401,
                detail="Wrong username or password.",
                headers={"WWW-Authenticate": 'Basic realm="AdNova Player"'},
            )

        lockout.passed(who)
        # Success is as forensically interesting as failure: after an incident
        # the question is who was on the box, not only who failed to get on.
        event_log.record(
            "admin.auth.ok", "warn",
            f"Admin login from {who} as {credentials.username[:32]!r}.",
        )

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(_require_login)])
    def admin_page() -> HTMLResponse:
        return HTMLResponse(_ADMIN_HTML)

    @app.get("/admin/status", dependencies=[Depends(_require_login)])
    def admin_status() -> JSONResponse:
        return JSONResponse(status())

    @app.get("/admin/events", dependencies=[Depends(_require_login)])
    def admin_events() -> JSONResponse:
        """
        The device's own forensic trail, newest first.

        Worth having on the page rather than only at Dashboard: the moment a
        technician most needs to read it is when the device cannot reach
        Dashboard to ship it. The chain check rides along so a tampered card
        announces itself here too.
        """
        events = event_log.current()
        if events is None:
            return JSONResponse({"events": [], "chain_ok": True, "sequence": 0})
        ok, broken_at = events.verify()
        return JSONResponse({
            "events": events.recent(100),
            "chain_ok": ok,
            "broken_at": broken_at,
            "sequence": events.sequence,
            "pending": events.pending(),
        })

    @app.post("/admin/action/{name}", dependencies=[Depends(_require_login)])
    def admin_action(request: Request, name: str) -> JSONResponse:
        if on_action is None or name not in {"refetch", "update"}:
            event_log.record(
                "admin.action.rejected", "security",
                f"Unknown admin action {name[:40]!r} from {_who(request)}.",
            )
            raise HTTPException(status_code=400, detail="Unknown action.")

        accepted = bool(on_action(name))
        # An update is the one action here that changes what the device runs,
        # so it is logged at warn: it should be visible in the same view as
        # the version change it causes.
        event_log.record(
            "admin.action", "warn",
            f"{name} triggered from {_who(request)} (accepted={accepted}).",
        )
        return JSONResponse({"ok": accepted})


# The status page. Self-contained, polls /admin/status, no external
# anything — it works on a device with no internet, which is exactly when
# a technician is standing in front of it.
_ADMIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AdNova Player</title>
<style>
  :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
  body { margin: 0; padding: 1.5rem; max-width: 40rem; }
  h1 { font-size: 1.1rem; letter-spacing: .04em; text-transform: uppercase; opacity: .7; }
  .row { display: flex; justify-content: space-between; padding: .5rem 0;
         border-bottom: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
  .k { opacity: .65; } .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .ok { color: #16a34a; } .warn { color: #d97706; } .bad { color: #dc2626; }
  button { font: inherit; padding: .5rem 1rem; margin-right: .5rem; margin-top: 1rem;
           border: 1px solid currentColor; border-radius: .5rem; background: transparent;
           cursor: pointer; }
  #log { white-space: pre-wrap; font: .8rem/1.4 ui-monospace, monospace;
         margin-top: 1rem; opacity: .8; }
  h2 { font-size: .8rem; letter-spacing: .04em; text-transform: uppercase;
       opacity: .6; margin: 2rem 0 .5rem; }
  #chain { font-size: .8rem; margin-bottom: .5rem; }
  .ev { display: grid; grid-template-columns: 9rem 5rem 1fr; gap: .5rem;
        padding: .35rem 0; font-size: .78rem;
        border-bottom: 1px solid color-mix(in srgb, currentColor 8%, transparent); }
  .ev time { opacity: .55; font-variant-numeric: tabular-nums; }
  .ev .c { font-family: ui-monospace, monospace; }
  .sev-security, .sev-error { color: #dc2626; font-weight: 600; }
  .sev-warn { color: #d97706; }
  .sev-info { opacity: .7; }
</style></head><body>
<h1>AdNova Player</h1>
<div id="rows"></div>
<button onclick="act('refetch')">Refetch schedule</button>
<button onclick="act('update')">Check for updates</button>
<div id="log"></div>
<h2>Recent events</h2>
<div id="chain"></div>
<div id="events"></div>
<script>
async function load() {
  const r = await fetch('/admin/status', {cache:'no-store'});
  const s = await r.json();
  const cls = s.online ? 'ok' : 'bad';
  document.getElementById('rows').innerHTML = [
    ['Stand', s.stand_id],
    ['Version', s.player_version],
    ['Schedule', s.schedule_version || '—'],
    ['Now playing', s.now_playing || 'fallback'],
    ['Reached Dashboard', `<span class="${cls}">${s.online ? 'yes' : 'no'}</span>`],
    ['Temperature', s.temp_c != null ? s.temp_c + '°C' : '—'],
    ['Disk free', s.disk_free_mb != null ? s.disk_free_mb + ' MB' : '—'],
    ['Pending logs', s.pending_logs],
    ['Warnings', s.warnings && s.warnings.length ? `<span class="warn">${s.warnings.join('; ')}</span>` : 'none'],
  ].map(([k,v]) => `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}
async function act(name) {
  document.getElementById('log').textContent = name + '…';
  const r = await fetch('/admin/action/' + name, {method:'POST'});
  const j = await r.json();
  document.getElementById('log').textContent = name + ': ' + (j.ok ? 'done' : 'not accepted');
  load();
}
// The device's own forensic trail. Shown here as well as at Dashboard
// because the moment a technician most needs to read it is the moment the
// device cannot reach Dashboard to ship it.
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function events() {
  const r = await fetch('/admin/events', {cache:'no-store'});
  const s = await r.json();
  document.getElementById('chain').innerHTML = s.chain_ok
    ? `<span class="ok">Chain verified</span> · ${s.sequence} events recorded · ${s.pending} waiting to ship`
    : `<span class="bad">Chain broken at #${esc(s.broken_at)} — this card was altered</span>`;
  document.getElementById('events').innerHTML = (s.events || []).map(e => `
    <div class="ev">
      <time>${esc((e.at || '').replace('T', ' ').slice(0, 19))}</time>
      <span class="sev-${esc(e.sev)}">${esc(e.sev)}</span>
      <span><span class="c">${esc(e.code)}</span> ${esc(e.detail || '')}</span>
    </div>`).join('');
}
function refresh() { load(); events(); }
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""
