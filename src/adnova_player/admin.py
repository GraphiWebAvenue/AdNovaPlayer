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
import secrets
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Config

_security = HTTPBasic(realm="AdNova Player")


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

    def _require_login(
        request: Request,
        credentials: HTTPBasicCredentials = Depends(_security),
    ) -> None:
        # An unconfigured password locks the page entirely rather than
        # leaving it open — the safe direction for a page that shows the
        # device's identity.
        if not config.admin_password_hash:
            raise HTTPException(status_code=503, detail="Admin access is not configured.")

        user_ok = hmac.compare_digest(credentials.username, config.admin_user)
        pass_ok = verify_password(credentials.password, config.admin_password_hash)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="Wrong username or password.",
                headers={"WWW-Authenticate": 'Basic realm="AdNova Player"'},
            )

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(_require_login)])
    def admin_page() -> HTMLResponse:
        return HTMLResponse(_ADMIN_HTML)

    @app.get("/admin/status", dependencies=[Depends(_require_login)])
    def admin_status() -> JSONResponse:
        return JSONResponse(status())

    @app.post("/admin/action/{name}", dependencies=[Depends(_require_login)])
    def admin_action(name: str) -> JSONResponse:
        if on_action is None or name not in {"refetch", "update"}:
            raise HTTPException(status_code=400, detail="Unknown action.")
        return JSONResponse({"ok": bool(on_action(name))})


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
</style></head><body>
<h1>AdNova Player</h1>
<div id="rows"></div>
<button onclick="act('refetch')">Refetch schedule</button>
<button onclick="act('update')">Check for updates</button>
<div id="log"></div>
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
load(); setInterval(load, 5000);
</script>
</body></html>
"""
