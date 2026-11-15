# Extension presentation

## Ownership

`core/extensions/contributions.py` owns framework-independent UI declaration validation. `ExtensionAPI` collects declarations; `ExtensionRegistry` owns their exact registration lifetime. `server/rpc/extension_presentation.py` projects descriptors and validates calls. WebUI owns composition, context and rendering; Settings owns the shared layout preferences used by WebUI and Desktop. Public authoring guidance is in `resources/skills/vbot-docs/references/extension-platform.md`; composition details are in `webui/extension-pages.md`.

## Manifest and declaration contracts

A directory with only `extension.json` is discoverable. Its `pages` and `contributions` apply before optional Python registration and use the same validation and duplicate-id rules. `required_features` rejects an unsupported required contract before Python imports; API-version checks remain unchanged. Disabled Extensions retain metadata without executing either host's code. Optional `desktop` metadata describes a local Desktop entry, which server discovery never imports (`desktop/computer-sharing.md`).

Contributions have owner-local ids and project as `extension:<owner>:<id>` with the current epoch. Built-in WebUI contributions use `core:<id>`. A view names a page of the same owner; an unavailable page contributes no view. Pages can declare `navigation: false` to embed without adding a main-menu route. Icons accept known names or bounded SVG path geometry, never markup, remote URLs or executable content.

After all synchronous or asynchronous registration finishes, the loader checks contribution page and operation references against that owner's final declarations. Unknown page references and undeclared source, action or dynamic operations skip only the contribution and appear in `capability_errors`, surfaced by `extensions.list` and the CLI. Missing or unreadable page assets produce an `unavailable` diagnostic for the page and each dependent view while keeping their registration identities. Page/contribution projection and `extensions.list` refresh these transient diagnostics off the Event Loop; recovered assets clear only their availability errors, retain permanent declaration diagnostics and become visible without re-registering the Extension. Concurrent projections serialize availability observations per record and replace diagnostics without accumulating duplicates.

`when` keeps API30 equality maps and adds `all`, `any`, `not`, and explicit scalar predicates (`eq`, `ne`, `in`, `exists`, `gt`, `gte`, `lt`, `lte`), bounded to eight levels and 64 nodes. Missing context fails comparisons; `exists: false` tests absence. Numeric comparisons require finite numbers. The public context includes Message, Session-row and editor state for the corresponding surfaces. Text and source parameters may explicitly bind supported local context values; arbitrary JavaScript expressions and private client state are not part of the contract. The source receives only its declared parameters. A source reads an owner operation and the owner publishes ordinary scoped invalidations when its data changes. Its dynamic actions remain confined to the contribution's declared operation names.

## Dispatch and lifetime

`extensions.contribution_operation` checks owner, local id, epoch and declared operation before invocation. It calls the existing ExtensionOperations owner, not a second dispatcher. The declared source operation is a read projection and rechecks the registration before returning; admitted actions retain their definitive result even if they retire or reload their owner. Actions are never replayed after a transport error or reconnect.

`extensions.page_service` requires a current declared page and one of that page's explicit service names. It uses the owner-bound service catalog and preserves structured domain refusal codes and data. Queries recheck the page after awaiting; admitted executes retain their result even if they reload their own page. Discovery exposes only that page's declared services and their observation resources. Public catalog binding and authority belong to `extensions/host.md`.

Manifest-only discovery, declaration validation and retirement are covered by `tests/core/extensions/test_contributions.py`; projection, operation scope and declared services by `tests/server/rpc/test_extensions_pages.py`. Computer Use's source and Stop actions are covered through its management-operation contract in `tests/resources/extensions/computer_use/test_computer_use_tools.py`.
