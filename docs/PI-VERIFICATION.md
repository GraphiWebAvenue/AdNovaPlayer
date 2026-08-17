# On-Pi verification checklist

Everything in this repo is unit-tested on x86, but a few features touch the
display hardware, the Wayland session, or systemd in ways only a real Pi
exercises. Per the working rules ("test on a real Pi before merging anything
that touches playback, hardware access, or systemd"), verify these on a stand
before trusting them in the field. Each item is safe to ship as-is — none
changes mpv's start-up flags — but the on-Pi behaviour is unproven until
checked here.

## Display driver (`ops/adnova-mpv-driver.py`)

- [ ] **Freeze recovery.** Force a stall (e.g. pause the decoder / pull the
      media mid-play) and confirm the driver reloads the file, and on a second
      strike relaunches mpv, within ~12 s. Watch `/tmp/adnova-mpv.log`.
- [ ] **`mpv_get("time-pos")`** returns a number over the IPC socket on this
      mpv build; a wedged read returns None and does not trigger a false
      reload.
- [ ] **Display health file** `/var/lib/adnova-player/display.json` is written
      each tick and is readable by the player service (group perms), so the
      heartbeat's `display` block is populated.

## Gapless (safe core shipped; true-gapless deferred)

- [ ] The kiosk page preloads `next_src` and the image/video switch has no
      black flash on the panel.
- [ ] **Only if measured beneficial:** evaluate mpv `--prefetch-playlist=yes`
      + `playlist-next` for video→video gaplessness. DO NOT add the flag
      fleet-wide until confirmed present in `mpv --list-options` on the Trixie
      image — a bad flag makes mpv refuse to start (fleet-wide black screen).

## Verified now-playing (pixel level, deferred)

- [ ] Optional: compare a `grim` screenshot's signature to the expected
      media's first frame to catch a wrong/blank panel that the clock-side
      watchdog cannot. Needs an image lib decision (PIL not assumed present).

## Remote screen power (`screen_on` / `screen_off`)

- [ ] Wire the in-session helper (`ops/adnova-kiosk-helper.sh`) to read
      `/var/lib/adnova-player/ipc/screen.req` ("on"/"off") and drive the panel
      (wlr-randr / CEC / DPMS). The player side (writing the request) is done
      and tested; the helper consuming it is the remaining on-Pi piece.
- [ ] Confirm `shutdown` (systemctl poweroff) works with the sudoers rule.
      Remember there is no remote power-on without a smart plug / WoL.

## Auto re-enroll

- [ ] Revoke a stand key in Dashboard and confirm that after ~10 minutes of
      401s the device clears just `ADNOVA_STAND_KEY`, restarts, and re-enrolls
      onto the same device id for a fresh key — without losing its identity.

## SD armor

- [ ] Remount the card read-only (or use a worn card) and confirm the
      heartbeat reports `storage_writable: false` and the "replace the card"
      warning, while the screen keeps playing from cache.
