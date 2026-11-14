# Home Assistant setup

The `homeassistant` Extension connects vBot to Home Assistant for entity states and service calls. Configure it through the CLI or Settings -> Integrations -> Extensions.

Inspect its connection settings:

```bash
vbot extensions show homeassistant
```

Set `url` to the Home Assistant instance's base URL; the default is `http://homeassistant.local:8123`. Save its long-lived access token through `vbot extensions set homeassistant token --stdin` or the Extension settings form. The token stays in the Secret Store, and a supplied token makes the Tools available without a restart; a saved token alone does not verify connectivity.

The CLI configures the connection; entity and service work uses the Agent Tools `ha_list_entities`, `ha_get_state`, `ha_list_services` and `ha_call_service`. An Agent also needs access to those Tools. Use an entity or service id returned by the discovery Tools for the requested action, and report a connection failure with its diagnostic instead of repeating a mutation whose outcome is unknown.
