# Database Agent-facing text

Why each sentence of the Agent-facing text the Database domain owns exists: the data-format and data-store marker refusals Agents read through startup failures, `vbot data-store status` and `vbot doctor config`. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

Operators and Agents read these refusals through startup failures, `vbot data-store status` and `vbot doctor config`.

| Text | Reason |
|---|---|
| `Data written by vBot 0.4.4 or earlier predates the current data format, and this version cannot use it. ...` (`older_format_hint`) | The reader needs the version boundary and a next step that exists: a fresh data directory, or copying data out with the version that wrote it. No converter is offered (user decision, 2026-10-01). |
| `vBot writes the marker into every data directory it creates and never adopts an existing directory without one` (`missing_marker_error`) | An existing directory vBot did not create, such as an empty folder chosen as the data directory, is refused as well; the sentence says that only a path that does not exist yet gets a fresh data directory. Reasoning only, no Session evidence. |
