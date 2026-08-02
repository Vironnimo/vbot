---
name: home-assistant
description: Inspect and safely change advanced Home Assistant configuration through the bundled WebSocket script, especially Lovelace dashboards, dashboard metadata, and registry-backed planning. Use when the user asks to create, redesign, back up, export, restore, or deeply edit Home Assistant dashboards or needs Home Assistant configuration beyond entity state inspection and service calls. Do not use for ordinary device control when the ha_* Tools suffice.
metadata:
  vbot:
    requirements:
      env: HASS_TOKEN
---

# Home Assistant

Assume the Home Assistant connection is already configured and working. Do not guide connection setup or token creation. Use the four `ha_*` Tools for entity discovery, state inspection, service discovery, and ordinary device control; use the bundled WebSocket script only for deeper configuration that those Tools do not cover.

## Script contract

- Run `python {baseDir}/scripts/ha_ws.py --help` before first use.
- Include `HASS_TOKEN` in `bash.env_keys` on every script call. The script reads it directly and has no token argument; never place the value in a command, file, or output.
- The script defaults to the bundled extension's standard Home Assistant URL. For an already-configured nondefault instance, pass its existing base URL with global `--url`; never change connection settings as part of this Skill.
- Keep task files under `tmp/home-assistant/<task>/`. Store exported configs, proposed configs, and backups there unless the user requests a durable location.
- Treat script JSON output as data. A nonzero exit code or `"ok": false` means the operation did not complete.

## Dashboard workflow

1. Discover real entity ids and capabilities with `ha_list_entities` and `ha_get_state`. Use the script's read-only `call` command for area, floor, device, label, or entity registries when the dashboard needs those relationships.
2. Run `dashboard-list`, then export the target with `dashboard-export`. Never design against guessed entities or overwrite a dashboard that has not been exported in the current task.
3. Read [references/dashboard-design.md](references/dashboard-design.md) when composing or substantially restructuring dashboard JSON. Preserve unknown existing keys unless the requested change removes them.
4. Write the proposed JSON to a new task-local file and run `dashboard-validate`.
5. Run `dashboard-apply` without `--apply` first. Use the SHA-256 returned by the fresh export as `--expected-sha256`.
6. When the user requested the change, rerun the same command with `--apply` and a new `--backup` path. The script refuses races, creates the backup before saving, and verifies the stored config afterward.
7. Export once more and summarize the changed views, cards, and backup path. Do not claim visual quality without asking the user to inspect the rendered Home Assistant dashboard on its intended desktop or mobile display.

Example sequence; pass the Skill-granted environment key through each Bash call:

```text
python {baseDir}/scripts/ha_ws.py dashboard-list
python {baseDir}/scripts/ha_ws.py dashboard-export tmp/home-assistant/kitchen/before.json --url-path kitchen-wall
python {baseDir}/scripts/ha_ws.py dashboard-validate tmp/home-assistant/kitchen/proposed.json
python {baseDir}/scripts/ha_ws.py dashboard-apply tmp/home-assistant/kitchen/proposed.json --url-path kitchen-wall --expected-sha256 <export-sha256> --backup tmp/home-assistant/kitchen/backup.json
python {baseDir}/scripts/ha_ws.py dashboard-apply tmp/home-assistant/kitchen/proposed.json --url-path kitchen-wall --expected-sha256 <export-sha256> --backup tmp/home-assistant/kitchen/backup.json --apply
```

For a new storage dashboard, first write a metadata object containing `title` and `url_path`, plus optional `icon`, `show_in_sidebar`, and `require_admin`. Validate with a dry run, then add `--apply` only when the requested dashboard is ready:

```text
python {baseDir}/scripts/ha_ws.py dashboard-create tmp/home-assistant/kitchen/metadata.json tmp/home-assistant/kitchen/proposed.json
python {baseDir}/scripts/ha_ws.py dashboard-create tmp/home-assistant/kitchen/metadata.json tmp/home-assistant/kitchen/proposed.json --apply
```

## Read-only WebSocket inspection

Write one command object to a JSON file, then pass it to `call`. The script accepts only its read-only allowlist. Useful command types include `config/area_registry/list`, `config/floor_registry/list`, `config/device_registry/list`, `config/label_registry/list`, `config/entity_registry/list_for_display`, `lovelace/info`, and `lovelace/resources`.

```text
python {baseDir}/scripts/ha_ws.py call tmp/home-assistant/kitchen/areas-request.json
```

## Safety boundaries

- Never edit Home Assistant `.storage` files directly.
- Dashboard writes require an admin token. A YAML-mode dashboard may be exported, but the WebSocket API does not save it; stop on that error instead of attempting a filesystem workaround.
- Never bypass the script's read-only raw-command allowlist. Use purpose-built `ha_*` Tools for service calls so their validation and blocked-domain policy remain active.
- Do not delete dashboards, resources, helpers, automations, registry entries, or devices through this Skill. Ask for a separately scoped workflow if deletion is genuinely required.
- Prefer built-in cards. Before using `custom:*`, verify the resource is already installed and listed; never install frontend resources implicitly.
