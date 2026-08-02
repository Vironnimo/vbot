# Home Assistant Dashboard Design

Use this reference after exporting the live dashboard and discovering the actual entities. Home Assistant dashboard config is JSON-compatible YAML: a normal root contains `views`, while a strategy dashboard contains `strategy`. The bundled script deliberately performs structural checks only; Home Assistant remains authoritative for card-specific behavior.

## Design defaults

- Prefer a Sections view for new general-purpose dashboards. Use Tile cards for primary controls, Heading cards to establish groups, and compact sensor cards only when a trend or comparison matters.
- Design around user tasks and rooms, not integration names. Put frequent controls first, status second, diagnostics last, and destructive or security-sensitive controls behind deliberate actions.
- Keep mobile behavior primary: avoid very wide grids, dense entity dumps, and long labels. Use subviews for detailed climate, energy, media, or maintenance information.
- Use entity names already exposed by Home Assistant unless a shorter contextual label improves comprehension. Never invent entity ids.
- Preserve existing `custom:*` cards only when their resource is present in `lovelace/resources`. Prefer built-in cards for new work unless the user explicitly wants an installed custom card.

## Minimal normal dashboard

```json
{
  "views": [
    {
      "title": "Home",
      "path": "home",
      "type": "sections",
      "max_columns": 3,
      "sections": [
        {
          "type": "grid",
          "cards": [
            {"type": "heading", "heading": "Living room", "icon": "mdi:sofa"},
            {"type": "tile", "entity": "light.living_room"},
            {"type": "tile", "entity": "sensor.living_room_temperature"}
          ]
        }
      ]
    }
  ]
}
```

## View selection

- `sections`: default for grouped responsive layouts.
- `masonry`: useful when cards naturally vary in height and exact grouping matters less.
- `panel`: exactly one dominant full-width card, such as a map.
- `sidebar`: one wide primary column plus a narrow secondary column.
- `subview: true`: detailed pages reached through a `navigate` action; set a stable `path` and, when useful, `back_path`.

## Before apply

- Confirm every referenced entity exists and inspect capabilities needed by card features.
- Preserve view `path` values unless the user asked to change navigation.
- Check that each view title is non-empty, each card has a type, and Sections views contain section objects with card lists.
- Compare the proposal with the export. An unchanged SHA-256 means no write is needed.
- Keep the export, proposal, and script-created backup until the user has inspected the dashboard.

## Primary references

- Home Assistant dashboard cards: https://www.home-assistant.io/dashboards/cards/
- Dashboard views: https://www.home-assistant.io/dashboards/views/
- Sections layout: https://www.home-assistant.io/dashboards/sections/
- Multiple dashboards: https://www.home-assistant.io/dashboards/dashboards/
- Core Lovelace WebSocket implementation: https://github.com/home-assistant/core/blob/dev/homeassistant/components/lovelace/websocket.py
- Frontend Lovelace config calls: https://github.com/home-assistant/frontend/blob/dev/src/data/lovelace/config/types.ts
