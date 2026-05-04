# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not
import core/server business logic and it does not manage vBot server processes.
Phase 6 keeps Desktop intentionally thin: it loads the same server-served WebUI
that a browser would load from `/`, but inside a pywebview window.

## Interfaces

- `python desktop/main.py [--host] [--port]`
  - resolves the target server URL from Desktop-local settings plus CLI args
  - opens a pywebview window pointed at `http://<host>:<port>/`
  - if the target server is reachable but has no WebUI, shows an in-window
    message instead of crashing
- Desktop-local settings file
  - stores at least the last-used host and port
  - belongs to the Desktop app itself, not the shared server `data_dir`

## Conventions

- Desktop is an accessor only, not a server manager.
- Desktop may connect to localhost or LAN vBot servers over normal HTTP.
- The loaded UI is the normal WebUI root path `/`; no separate desktop-only
  frontend build or route is part of Phase 6.
- Closing the window ends only the Desktop process, never the target server.
- No Python↔JavaScript bridge is part of the Phase 6 contract.

## External Dependencies

- **pywebview** — native window wrapper used to host the existing WebUI.

## Constraints & Gotchas

- A healthy vBot server may exist without `webui/dist`; in that case Desktop
  must show a user-facing in-window message that the target server has no WebUI.
- Desktop-local preferences must not be written into the shared server
  `data_dir`, because that directory belongs to the selected vBot instance.
