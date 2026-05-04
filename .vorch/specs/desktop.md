# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not
import core/server business logic and it does not manage vBot server processes.
Phase 6 keeps Desktop intentionally thin: it loads the same server-served WebUI
that a browser would load from `/`, but inside a pywebview window.

## Interfaces

- `python desktop/main.py [--host] [--port]`
  - resolves the target server URL from CLI args, then Desktop-local settings,
    then defaults `127.0.0.1:8420`
  - persists the resolved host/port to Desktop-local settings
  - probes `GET /health` first and treats HTTP 200 with `{"status":"ok"}` as
    the vBot identity contract
  - probes `/` after health succeeds and opens a pywebview window pointed at
    `http://<host>:<port>/` only when the WebUI root returns 2xx/3xx
  - if the server is unreachable, is not a vBot server, or is reachable but has
    no WebUI, shows an escaped in-window message instead of crashing
- Desktop-local settings file
  - stores at least the last-used host and port
  - lives alongside `desktop/main.py`
  - belongs to the Desktop app itself, not the shared server `data_dir`
  - current source-run filename: `desktop/settings.json` (gitignored)

## Conventions

- Desktop is an accessor only, not a server manager.
- Desktop may connect to localhost or LAN vBot servers over normal HTTP.
- The loaded UI is the normal WebUI root path `/`; no separate desktop-only
  frontend build or route is part of Phase 6.
- The Desktop window title is `vBot`.
- A custom `desktop/icon.png` is optional; when absent, pywebview's platform
  default icon is used.
- Closing the window ends only the Desktop process, never the target server.
- No Python↔JavaScript bridge is part of the Phase 6 contract.
- If the server is unreachable or has no WebUI, Desktop stays open and shows an
  in-window message instead of crashing.

## External Dependencies

- **pywebview** — native window wrapper used to host the existing WebUI.

## Constraints & Gotchas

- A healthy vBot server may exist without `webui/dist`; in that case Desktop
  must show a user-facing in-window message that the target server has no WebUI.
- Desktop-local preferences must not be written into the shared server
  `data_dir`, because that directory belongs to the selected vBot instance.
- Phase 6 assumes a source-run Desktop shell, so settings live beside
  `desktop/main.py` rather than in a later packaging-specific app directory.
- pywebview is imported lazily so backend tests and non-desktop development
  workflows do not require the optional GUI package.
