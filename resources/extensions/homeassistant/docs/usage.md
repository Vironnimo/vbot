# Home Assistant setup

The `homeassistant` Extension connects vBot to Home Assistant for entity states and service calls. Configure it through the CLI or Settings -> Integrations -> Extensions.

Inspect its connection settings:

```bash
vbot extensions show homeassistant
```

Set `url` to the Home Assistant instance's base URL; the default is `http://homeassistant.local:8123`. Save its long-lived access token through `vbot extensions set homeassistant token --stdin` or the Extension settings form. The token stays in the Secret Store, and a supplied token makes the Tools available without a restart; a saved token alone does not verify connectivity.

The CLI configures the connection; entity and service work uses the Agent Tools `ha_list_entities`, `ha_get_state`, `ha_list_services` and `ha_call_service`. An Agent also needs access to those Tools. Use an entity or service id returned by the discovery Tools for the requested action, and report a connection failure with its diagnostic instead of repeating a mutation whose outcome is unknown.

For Desktop wakeword commands, save a phrase, optional aliases and one Home Assistant Tool with fixed arguments under Settings -> Integrations -> Extensions -> Home Assistant wakeword commands. Include the intended action and target in the phrase or aliases so the Decision Model can distinguish similar commands. Exact phrases work without a Decision Model. Home Assistant's settings independently control recognition of rephrased commands (on by default) and a System Reminder with the executed call and its result (off by default).
