# Extension presentation

## Ownership

`core/extensions/contributions.py` owns framework-independent UI declaration validation. `ExtensionAPI` collects declarations; `ExtensionRegistry` owns their exact registration lifetime. `server/rpc/extension_presentation.py` projects descriptors and validates calls. WebUI owns composition, context, rendering and device-local layout preferences; Desktop uses that same WebUI. Public authoring guidance is in `resources/skills/vbot-docs/references/extension-platform.md`; composition details are in `webui/extension-pages.md`.

## Manifest and declaration contracts

A directory with only `extension.json` is discoverable. Its `pages` and `contributions` apply before optional Python registration and use the same validation and duplicate-id rules. `required_features` rejects an unsupported required contract before Python imports; API-version checks remain unchanged. Disabled Extensions retain metadata without executing either host's code. Optional `desktop` metadata describes a local Desktop entry, which server discovery never imports (`desktop/computer-sharing.md`).

Contributions have owner-local ids and project as `extension:<owner>:<id>` with the current epoch. Built-in WebUI contributions use `core:<id>`. A view names a page of the same owner; an unavailable page contributes no view. Pages can declare `navigation: false` to embed without adding a main-menu route. Icons accept known names or bounded SVG path geometry, never markup, remote URLs or executable content.

`when` compares public scalar context fields. Text and source parameters may explicitly bind supported local context values; arbitrary JavaScript expressions and private client state are not part of the contract. The source receives only its declared parameters. A source reads an owner operation and the owner publishes ordinary scoped invalidations when its data changes. Its dynamic actions remain confined to the contribution's declared operation names.

## Dispatch and lifetime

`extensions.contribution_operation` checks owner, local id, epoch and declared operation before invocation and before returning the result. It calls the existing ExtensionOperations owner, not a second dispatcher. Source results are read projections; actions are never replayed after a transport error or reconnect. An admitted operation can settle while its owner is retired, but the old UI registration cannot receive its late result.

`extensions.page_service` requires a current declared page and one of that page's explicit service names. It uses the owner-bound service catalog and preserves structured domain refusal codes and data. Discovery exposes only that page's declared services and their observation resources. Public catalog binding and authority belong to `extensions/host.md`.

Manifest-only discovery, declaration validation and retirement are covered by `tests/core/extensions/test_contributions.py`; projection, operation scope and declared services by `tests/server/rpc/test_extensions_pages.py`. Computer Use's source and Stop actions are covered through its management-operation contract in `tests/resources/extensions/computer_use/test_computer_use_tools.py`.
