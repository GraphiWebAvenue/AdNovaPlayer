"""
The local server the kiosk browser talks to.

Chromium runs full-screen pointed at http://127.0.0.1:<port>/ and does
nothing but render. It never reaches Dashboard, never holds a credential,
never makes a decision — it asks this server "what do I show now?" and
plays what comes back. Everything sensitive stays on the Python side of
this loopback boundary.

Three things are served:

  GET /            the kiosk page — a single self-contained HTML document
  GET /state       what to play right now, as JSON, polled by the page
  GET /media/<sum> a cached, checksum-verified media file
  GET /fallback    the bundled loop, shown when nothing real can be

Bound to loopback only. The browser is the sole client and it runs on
this machine, so there is nothing to expose and no auth to add on the
playback path — the admin/status page (P-later) is the only thing that
gets a login.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .cache import MediaCache
from .schedule import PlayItem, Schedule

log = logging.getLogger("adnova.server")

# A checksum is 64 hex characters. Anything else is not one of ours, and
# the guard turns a path-traversal attempt into a clean 404 rather than a
# filesystem lookup.
_HEX = set("0123456789abcdef")


def is_checksum(value: str) -> bool:
    return len(value) == 64 and all(c in _HEX for c in value)


def build_app(
    cache: MediaCache,
    current_schedule: Callable[[], Schedule],
    now: Callable[[], datetime] | None = None,
    fallback_path: Path | None = None,
    on_playing: Callable[[PlayItem], None] | None = None,
) -> FastAPI:
    """
    Assemble the local app.

    `current_schedule` is a callable, not a value, so the server always
    reads the latest plan the download loop has installed without being
    restarted. `now` is injectable for tests. `on_playing` lets the agent
    record what actually went on screen, for the playback log.
    """
    clock = now or (lambda: datetime.now(tz=UTC))
    app = FastAPI(title="AdNova Player (local)", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def kiosk() -> HTMLResponse:
        return HTMLResponse(_KIOSK_HTML)

    @app.get("/state")
    def state() -> JSONResponse:
        """
        What belongs on screen right now, and when to ask again.

        The page polls this, but only rarely — the `next_change_at` field
        tells it exactly when the current item ends, so it can switch on
        time without busy-polling.
        """
        moment = clock()
        schedule = current_schedule()
        item = schedule.now_playing(moment)

        if on_playing is not None:
            on_playing(item)

        change_at = schedule.next_change_after(moment)

        return JSONResponse({
            "slot_id": item.slot_id,
            "ad_id": item.ad_id,
            "kind": item.kind,
            "src": item.local_src,
            "muted": item.muted,
            "loop": item.loop,
            "duration_seconds": item.duration_seconds,
            "priority": item.priority,
            "label": item.label,
            "is_fallback": item.is_fallback,
            "schedule_version": schedule.schedule_version,
            "server_time": moment.isoformat(),
            "next_change_at": change_at.isoformat() if change_at else None,
        })

    @app.get("/media/{checksum}")
    def media(checksum: str) -> Response:
        """A cached file, but only if it is present and still hashes true."""
        if not is_checksum(checksum):
            return Response(status_code=404)
        if not cache.has(checksum):
            return Response(status_code=404)

        # No filename guessing: the browser decides image-vs-video from the
        # /state kind, and a wrong content-type here cannot cause it to
        # execute anything, because the CSP on the page forbids scripts.
        return FileResponse(
            cache.path_for(checksum),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/fallback", response_class=HTMLResponse)
    def fallback() -> Response:
        """The bundled loop, when there is nothing real to show."""
        if fallback_path and fallback_path.exists():
            return FileResponse(fallback_path)
        return HTMLResponse(_FALLBACK_HTML)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # For the watchdog and the admin page to confirm the server itself
        # is answering, distinct from whether a manifest has arrived.
        return JSONResponse({"ok": True})

    return app


# The kiosk page. One self-contained document: no external anything, so it
# works with no network and cannot be told to load a script from elsewhere.
# It preloads the next item into a hidden layer and crossfades, so a switch
# between slots has no black flash.
_KIOSK_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AdNova</title>
<!-- An empty favicon so the browser never fires a 404 request that could
     flash a loading indicator. -->
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: dark; }
  html, body {
    margin: 0; padding: 0; width: 100vw; height: 100vh;
    background: #000; overflow: hidden;
  }
  /* No pointer, ever, anywhere. A stray cursor — including the "busy"
     spinner a hard-working CPU shows — has no place on a shop screen. */
  *, *::before, *::after { cursor: none !important; }
  .layer {
    position: fixed; inset: 0;
    display: flex; align-items: center; justify-content: center;
    opacity: 0; transition: opacity 500ms ease-in-out;
    background: #000;
    will-change: opacity;
  }
  .layer.show { opacity: 1; }
  .layer img, .layer video {
    width: 100%; height: 100%; object-fit: contain;
    background: #000;
  }
  /* Kill any native media UI: a signage video is never scrubbed. */
  video::-webkit-media-controls,
  video::-webkit-media-controls-enclosure { display: none !important; }
  #splash {
    position: fixed; inset: 0; display: flex;
    align-items: center; justify-content: center;
    background: #0b0b0f; color: #cbd5e1;
    font: 600 2.2vw/1.4 system-ui, sans-serif; letter-spacing: .08em;
    transition: opacity 500ms ease-in-out;
  }
  #splash.hidden { opacity: 0; pointer-events: none; }
  .brand { text-transform: uppercase; opacity: .85; }
</style>
</head>
<body>
  <div class="layer" id="a"></div>
  <div class="layer" id="b"></div>
  <div id="splash"><span class="brand">AdNova</span></div>
<script>
(() => {
  "use strict";

  // Two layers, swapped on each change so the outgoing media fades out as
  // the incoming one fades in. The outgoing element is torn down the moment
  // the fade ends, so a video never keeps decoding behind the scenes — two
  // simultaneous decodes is exactly what makes a Pi stutter.
  const layers = [document.getElementById("a"), document.getElementById("b")];
  const splash = document.getElementById("splash");
  let front = 0;
  let currentKey = null;
  let timer = null;

  const keyOf = (s) => s ? `${s.slot_id}:${s.src}:${s.schedule_version}` : null;

  async function poll() {
    let state;
    try {
      const res = await fetch("/state", { cache: "no-store" });
      state = await res.json();
    } catch (e) {
      // The local server is briefly unavailable (a restart). Keep showing
      // whatever is on screen and try again shortly; never blank.
      schedule(2000);
      return;
    }

    const key = keyOf(state);
    if (key !== currentKey) {
      currentKey = key;
      show(state);
    }

    schedule(nextPollMs(state));
  }

  function nextPollMs(state) {
    const floor = 1000, ceil = 15000;
    // A looping fallback never changes on its own; poll it slowly so we
    // notice when real content returns without hammering the server.
    if (state.loop) return 4000;
    if (state.next_change_at) {
      const dt = new Date(state.next_change_at) - new Date(state.server_time);
      if (isFinite(dt) && dt > 0) return Math.min(ceil, Math.max(floor, dt + 200));
    }
    return Math.min(ceil, Math.max(floor, (state.duration_seconds || 10) * 1000));
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  function show(state) {
    const back = layers[1 - front];
    const oldFront = layers[front];

    const el = state.kind === "video" ? makeVideo(state) : makeImage(state);
    back.innerHTML = "";
    if (el) back.appendChild(el);

    if (!state.is_fallback) splash.classList.add("hidden");

    // Bring the back layer forward, drop the old one, then tear the old
    // one down once the fade has finished so its video stops decoding.
    requestAnimationFrame(() => {
      back.classList.add("show");
      oldFront.classList.remove("show");
      front = 1 - front;
      setTimeout(() => teardown(oldFront), 520);
    });
  }

  function teardown(layer) {
    const v = layer.querySelector("video");
    if (v) { try { v.pause(); v.removeAttribute("src"); v.load(); } catch (e) {} }
    layer.innerHTML = "";
  }

  function makeImage(state) {
    if (state.is_fallback && state.src === "/fallback") return null; // dark filler
    const img = new Image();
    img.decoding = "async";
    img.src = state.src;
    return img;
  }

  function makeVideo(state) {
    const v = document.createElement("video");
    v.src = state.src;
    v.autoplay = true;
    v.muted = !!state.muted;         // sound only when the manifest allows it
    v.loop = !!state.loop;           // the fallback loops; a slot plays once
    v.playsInline = true;
    v.preload = "auto";
    v.disablePictureInPicture = true;
    v.controls = false;
    // Start playback as soon as there is enough to show, so there is no
    // frozen first frame while the rest buffers.
    v.addEventListener("canplay", () => { v.play().catch(() => {}); }, { once: true });
    v.play().catch(() => {});
    return v;
  }

  poll();
})();
</script>
</body>
</html>
"""

# The absolute-minimum fallback, if no branded loop was bundled. A calm
# dark field, never an error message — the screen is in a shop window.
_FALLBACK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100vh;background:#0b0b0f}
</style></head><body></body></html>
"""
