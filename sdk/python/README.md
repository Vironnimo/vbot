# vBot Python Extension SDK

`vbot-extension-sdk` provides a versioned authoring namespace and self-contained
type declarations for Python Extensions. The server and every Desktop package
already include `vbot_extension_sdk`; an Extension does not install it into the
managed dependency environment. Python 3.14 is the supported interpreter.

## Author outside the application

Copy `resources/sdk/vbot_extension_sdk-32.0.0-py3-none-any.whl` from any installed
vBot package into your authoring environment:

```bash
python -m pip install --no-deps ./vbot_extension_sdk-32.0.0-py3-none-any.whl
```

The wheel contains no application Runtime and requires no third-party packages.
Editors and type checkers use its `.pyi` files without a vBot source checkout.
Importing SDK version metadata works independently. Accessing runtime classes,
executing an Extension, or calling `require_api()` requires the matching installed
vBot host. A missing host raises `HostUnavailableError`; installing this wheel is
not a way to run vBot or its adapters independently.

The public imports are:

| Namespace | Contracts |
|---|---|
| `vbot_extension_sdk` | `ExtensionAPI`, `ExtensionHost`, operations, services, subscription handles, hook decisions, interactions, settings and databases |
| `vbot_extension_sdk.providers` | `ProviderAdapter`, Model/configuration values, wire profiles, reasoning and transport errors |
| `vbot_extension_sdk.channels` | `ChannelAdapter`, configuration, factory/context, conversation engine/transport, routing facts, attachments and content blocks |
| `vbot_extension_sdk.tools` | `Tool`, `ToolContext`, errors and result envelopes |
| `vbot_extension_sdk.commands` | command outcomes, feedback and Run requests |
| `vbot_extension_sdk.desktop` | independently versioned native registration, capability and execution contracts |

These values are aliases of the host's existing owners, so subclass checks and
exception handling use the same class identity as built-ins. Importing the root
or native namespace does not load Providers, Channels or the server. Opaque host
capabilities outside these contracts remain typed `Any`; use `host.services`
for product integration instead of navigating internal Runtime objects.

The checked public contract is the exported namespace plus its declarations,
not every implementation member reachable on an aliased class. `ExtensionOperations`
comes from `api.operations`; `ExtensionCommandContext` comes from a command
callback. Their authoring types intentionally reject direct construction because
the host supplies private wiring. Other declared constructors remain callable.

## Version contract and migration from API 31

The SDK version is `32.0.0`, and `API_VERSION` describes the SDK's server contract.
Declare `"api_version": 32` in `extension.json` for Extensions using the new
contract; the host refuses newer requirements before registration. Existing API
31 manifests and `core.extensions` imports remain supported. Change imports to
the SDK without copying classes or adding adapter wrappers. The host and SDK
must provide the same API generation when running separately installed SDK code.

API 32 adds actual Identity Agent execution in owner-managed Sessions and
`host.identity_agents` for creating/configuring an Extension's own specialists.
The original `identity_profile` remains a temporary configuration snapshot.
Owner-bound identity creation accepts a stable `request_id`; retrying the exact
owner, Agent id, request and configuration returns the original identity.

For optional support in shared code, `require_api(minimum=31)` returns the
running host's API version or raises; `maximum=` can bound a tested version
range. Use the manifest's `required_features` for load-time feature negotiation,
and a live `host.services.describe()` to discover available product services.
Do not infer the server API from the native Desktop version:
`vbot_extension_sdk.desktop.DESKTOP_EXTENSION_API_VERSION` is independently 2.

API 31 also makes service observers resilient: `subscribe(..., on_error=...)`
returns an `ExtensionSubscription`. Calling it remains compatible with the API
30 disposer; `.close()` is equivalent and `.status` exposes current state and
sanitized failure diagnostics. A callback error or timeout marks it degraded,
and later invalidations can recover it. Invalidation is a signal to read current
state, not an event log or an instruction to repeat a mutation.

## Services owned by an Extension

This complete entry registers a query and an observable resource. Install it in
an Extension directory with an API 31 manifest and reload that server's
Extensions. Its resource and service are `extension:<percent-encoded-directory-name>:entries`
and `extension:<percent-encoded-directory-name>:list`.

```python
from typing import Any
from vbot_extension_sdk import ExtensionAPI, ExtensionHost


def register(api: ExtensionAPI) -> None:
    api.register_resource("entries", description="Current entries")

    async def list_entries(params: dict[str, Any]) -> dict[str, Any]:
        return {"items": []}

    api.register_service(
        "list",
        kind="query",
        handler=list_entries,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        resources=("entries",),
        description="List current entries",
    )

    async def start(host: ExtensionHost) -> None:
        assert host.services is not None
        host.services.publish("entries")

    api.operations.startup.append(start)
```

Publish after an owned state change with `host.services.publish("entries",
scope={...})`; a declared scope schema validates the scope. Consumers discover
the canonical service/resource ids through `describe()` and use `query`,
`execute` and `subscribe` on their owner-bound host. Pages additionally declare
their service allowlist. A disabled or reloaded owner retires its services,
subscriptions and old host; retain no old registration handles.

## Provider and Channel adapters

Import `ProviderAdapter` from `vbot_extension_sdk.providers`. A custom subclass
implements `send`, `stream`, `normalize_response` and `aclose`. The host calls
its constructor with four positional arguments: `ProviderConfig`, `TokenGetter`,
the Connection's optional base URL, and `AuthConfig`. It also passes keyword
arguments `model_lookup`, `debug_recorder`, `connection_mode` and `http_transport`;
accept them and forward the lookup/recorder to `super().__init__`.
`TokenGetter` is an async callable returning the current credential; the supplied
HTTP transport is the host's pooled `httpx.AsyncBaseTransport`, not an HTTP client.
Use it when creating the adapter's HTTP client and close your client in `aclose`.
Normalize stream deltas and terminal
outcomes using the exported helpers. Register it with
`api.register_provider(definition, adapter_type=MyAdapter)`; omit `adapter_type`
to use an existing named wire adapter.

Import `ChannelAdapter`, `ChannelConfig`, `ChannelAdapterContext`,
`ConversationTransport`, `ConversationFacts` and `RouteFacts` from
`vbot_extension_sdk.channels`. The synchronous factory receives configuration
and the context, and constructs the adapter without I/O. `start()` connects,
reports `_report_connected()` and attaches its `ConversationTransport` to
`context.engine`; inbound messages go through `handle_inbound_text` or
`handle_inbound_media` so common authorization and routing remain active.
`ensure_outbound_session()` delegates to `engine.ensure_channel_session()`.
Implement `send()` and `stop()`; mark uncertain delivery with `ChannelError`
and use `DeliveryProgress` for multipart writes. The public type declarations
include the full adapter and transport signatures and their data values.

## Build the distributable

From a vBot development checkout with its declared `hatchling` build dependency:

```bash
python -m pip wheel --no-deps --no-build-isolation ./sdk/python --wheel-dir ./resources/sdk
```

Publish/copy the resulting wheel, not this metadata-only build directory. The
runtime package's source is `vbot_extension_sdk/`; update its declarations and
rebuild the wheel together. `tests/core/extensions/test_python_sdk.py` checks
every exported declaration against its canonical owner: namespace inventory,
method and attribute annotations, aliases, async behavior, abstract and Protocol
implementation obligations, callable argument kinds, and defaults. Concrete
immutable defaults are recorded in the declarations and compared by value;
factory and opaque object defaults use `...`, whose presence and type are checked.
Declared `Any` positions intentionally permit more specific host types.

The two host-supplied constructors above are the only named shape exemptions.
PEP 695/Literal aliases are structurally compared separately because stubtest
does not understand all of their runtime representations. Internal extra members
are permitted, but new exported names and new required subclass methods cannot
silently escape the check. The package contract also type-checks an isolated
authoring consumer and compares every shipped SDK source/type file with the wheel.
