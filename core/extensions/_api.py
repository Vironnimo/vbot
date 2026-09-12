"""Collect Extension declarations through the public registration facade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.extensions._declarations import (
    CommandDeclaration,
    CommandHandler,
    ExtensionDeclarations,
    HookHandler,
    LifecycleHandler,
    PageDeclaration,
    PromptBlockDeclaration,
    RecallBackendDeclaration,
    SessionPromptBlockDeclaration,
    SessionRuntimeDeclaration,
    ToolDeclaration,
    ToolFamilyDeclaration,
    _is_page_id,
)
from core.extensions.interactions import (
    InteractionHandlerDeclaration,
)
from core.extensions.operations import ExtensionOperations
from core.extensions.settings_schema import parse_settings_fields


class ExtensionAPI:
    """Registration facade passed into an extension's ``register(api)``.

    Every call only *collects a declaration* onto the extension's record;
    nothing goes live until the loader's apply phase runs after all extensions
    have registered. ``config`` is the per-extension settings **snapshot** taken
    at register time (empty dict by default) — for structural decisions inside
    ``register()``. ``logger`` is a ``vbot.extensions.<name>`` logger.

    ``config_provider`` and ``credential_resolver`` (both already bound to this
    extension) back the **live** per-call reads :meth:`get_config` /
    :meth:`resolve_credential`, so values and secrets set through the settings
    UI take effect without a restart. Both default to ``None`` for standalone
    construction (tests), in which case the live reads fall back to the snapshot
    / an empty string.
    """

    def __init__(
        self,
        extension_name: str,
        declarations: ExtensionDeclarations,
        *,
        config: dict[str, Any],
        logger: Any,
        config_provider: Callable[[], dict[str, Any]] | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._extension_name = extension_name
        self._declarations = declarations
        self.config = config
        self.logger = logger
        self._config_provider = config_provider
        self._credential_resolver = credential_resolver
        self.operations = ExtensionOperations(extension_name)
        declarations.operations = self.operations

    def register_settings(self, fields: list[Any]) -> None:
        """Declare this extension's settings schema (see :mod:`settings_schema`).

        Validates *fields* through :func:`parse_settings_fields` (a violation
        raises ``ValueError`` naming the bad field, failing the extension) and
        stores the parsed schema on the declarations. Calling it twice raises
        ``ValueError`` — an extension declares exactly one schema.
        """
        if self._declarations.settings_schema is not None:
            raise ValueError("settings schema already declared")
        self._declarations.settings_schema = parse_settings_fields(fields)

    def get_config(self) -> dict[str, Any]:
        """Return the extension's config, read **live** per call (fresh dict).

        Unlike :attr:`config` (the register-time snapshot), this reflects a
        config change persisted through the settings UI without a restart. Falls
        back to a copy of the snapshot when no live provider is wired.
        """
        if self._config_provider is None:
            return dict(self.config)
        return self._config_provider()

    def resolve_credential(self, key: str) -> str:
        """Resolve one credential **live** per call (process env, then ``.env``).

        Returns ``""`` when no resolver is wired (standalone construction).
        """
        if self._credential_resolver is None:
            return ""
        return self._credential_resolver(key)

    def on(self, event: str, handler: HookHandler) -> None:
        """Declare a hook handler for *event*. Called as ``handler(ctx, **payload)``."""
        self._declarations.hooks[event].append(handler)

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        *,
        internal: bool = False,
        catalog_visible: bool = True,
        requires_opt_in: bool = False,
        display: Any = None,
        ready: Callable[[], bool] | None = None,
        readiness_hint: str | None = None,
        result_schema: dict[str, Any] | None = None,
        parallel_safe: bool = True,
        open_input_schema: bool = False,
        family: str | None = None,
    ) -> None:
        """Declare an agent tool, mirroring ``ToolRegistry.register``.

        Only collects the declaration; the runtime applies it into the live
        ``ToolRegistry`` after the last built-in tool is registered. A name
        that collides with a built-in or another extension's tool is skipped
        and diagnosed on this extension's record — extensions never override
        an existing tool.

        ``family`` references an Extension-local id declared through
        :meth:`register_tool_family`. ``ready`` is an optional zero-arg readiness
        predicate (cheap, I/O-free):
        a not-ready tool stays registered but is hidden from the System Prompt,
        the provider tool definitions, and the tool picker until it is ready
        (e.g. once the extension's credential is set). ``None`` means always
        ready. ``readiness_hint`` is optional English text explaining that
        precondition, surfaced by the ``tool.list`` RPC.
        """
        self._declarations.tools.append(
            ToolDeclaration(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                internal=internal,
                catalog_visible=catalog_visible,
                requires_opt_in=requires_opt_in,
                display=display,
                ready=ready,
                readiness_hint=readiness_hint,
                result_schema=result_schema,
                parallel_safe=parallel_safe,
                open_input_schema=open_input_schema,
                family=family,
            )
        )

    def register_page(
        self,
        page_id: str,
        title: str,
        entry: str,
        *,
        icon: str = "network",
    ) -> None:
        """Declare one page whose asset remains under this Extension's root."""
        if not _is_page_id(page_id):
            raise ValueError("page_id must use lowercase letters, digits, hyphens, or underscores")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("page title must be a non-empty string up to 120 characters")
        entry_path = Path(entry) if isinstance(entry, str) else None
        if (
            entry_path is None
            or not entry.strip()
            or "\x00" in entry
            or entry.startswith(("/", "\\"))
            or ":" in entry
            or entry_path.is_absolute()
            or ".." in entry.replace("\\", "/").split("/")
            or entry_path.suffix.lower() != ".html"
        ):
            raise ValueError("page entry must be a relative HTML asset path")
        if not isinstance(icon, str) or icon not in {"network", "panel", "grid", "sparkles"}:
            raise ValueError("page icon is not supported")
        if any(item.page_id == page_id for item in self._declarations.pages):
            raise ValueError(f"page already declared: {page_id}")
        self._declarations.pages.append(PageDeclaration(page_id, title.strip(), entry, icon))

    def register_session_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        """Declare a hidden Tool available only through an exact Session grant."""
        self._declarations.tools.append(
            ToolDeclaration(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                catalog_visible=False,
                open_input_schema=True,
                coerce_arguments=False,
                session_scoped=True,
                activation="session_grant",
                **kwargs,
            )
        )

    def register_session_prompt_block(self, slug: str, *, render: Callable[..., str]) -> None:
        if not isinstance(slug, str) or not slug or not callable(render):
            raise ValueError("invalid session prompt block declaration")
        self._declarations.session_prompt_blocks.append(SessionPromptBlockDeclaration(slug, render))

    def register_session_runtime(
        self,
        *,
        before_request: Callable[..., Any],
        run_finished: Callable[..., Any],
        quiesce: Callable[..., Any],
        acknowledge_delivery: Callable[..., Any] | None = None,
        reconcile_tool_batch: Callable[..., Any] | None = None,
    ) -> None:
        if self._declarations.session_runtime is not None:
            raise ValueError("session runtime already declared")
        if not all(callable(item) for item in (before_request, run_finished, quiesce)) or any(
            item is not None and not callable(item)
            for item in (acknowledge_delivery, reconcile_tool_batch)
        ):
            raise ValueError("session runtime handlers must be callable")
        self._declarations.session_runtime = SessionRuntimeDeclaration(
            before_request, run_finished, quiesce, acknowledge_delivery, reconcile_tool_batch
        )

    def register_tool_family(self, family_id: str, label: str) -> None:
        """Declare a presentation family that this Extension's Tools may join.

        The id is local to this Extension. The apply phase namespaces it by the
        Extension identity so unrelated Extensions cannot merge families by
        accidentally choosing the same id.
        """
        self._declarations.tool_families.append(ToolFamilyDeclaration(id=family_id, label=label))

    def register_command(
        self,
        name: str,
        description: str,
        handler: CommandHandler,
        *,
        argument: str = "optional",
        catalog_result: str = "notice",
        execution_mode: str = "serialized",
        argument_execution_mode: str | None = None,
        unavailable_surfaces: frozenset[str] | set[str] | tuple[str, ...] = (),
    ) -> None:
        """Declare a slash command for later application by Chat.

        Declaration is intentionally inert here. ``CommandDispatcher`` validates
        the metadata and installs the handler after Runtime has created its
        canonical dispatcher. A malformed or colliding command is diagnosed as
        one skipped capability without failing the rest of the Extension.
        """
        self._declarations.commands.append(
            CommandDeclaration(
                name=name,
                description=description,
                handler=handler,
                argument=argument,
                catalog_result=catalog_result,
                execution_mode=execution_mode,
                argument_execution_mode=argument_execution_mode,
                unavailable_surfaces=unavailable_surfaces,
            )
        )

    def register_recall_backend(self, name: str, factory: Callable[..., Any]) -> None:
        """Declare a session-recall backend (``RecallBackendContext -> RecallBackend``).

        Only collects the declaration; the runtime applies it onto the recall
        registry before the persisted ``recall.backend`` is resolved. A
        duplicate or non lowercase-snake_case name is skipped and diagnosed on
        this extension's record.
        """
        self._declarations.recall_backends.append(
            RecallBackendDeclaration(name=name, factory=factory)
        )

    def register_interaction_handler(self, prefix: str, handler: Callable[..., Any]) -> None:
        """Declare a channel-interaction (button-tap) handler for *prefix*.

        Only collects the declaration; the runtime builds the prefix map after
        every extension has registered. The handler is called
        ``handler(event, responder)`` for each tap whose callback ``data`` begins
        with ``"<prefix>:"`` (see :mod:`core.extensions.interactions`). A prefix
        already claimed by an earlier-loaded extension is skipped and diagnosed
        on this extension's record — extensions never override an existing
        prefix.
        """
        self._declarations.interaction_handlers.append(
            InteractionHandlerDeclaration(prefix=prefix, handler=handler)
        )

    def register_prompt_block(
        self,
        slug: str,
        *,
        default_text: str | None = None,
        render: Callable[..., str] | None = None,
    ) -> None:
        """Declare a System Prompt block (D6), static **or** dynamic.

        Only collects the declaration; the runtime rebuilds the block-definition
        list on every extension (re)load and hands it to the prompt manager (no
        live registry, no per-run reload). The block id is ``extension:<slug>`` and
        its owner is ``extension:<extension-name>`` — so gate 2 renders the block
        only while this extension is loaded, and an extension may declare several
        blocks by using distinct slugs (e.g. a static one and a dynamic one).

        Pass **exactly one** of ``default_text`` (a static, editable block whose
        text flows through the override cascade) or ``render`` (a dynamic,
        non-editable block whose text is produced at build time; a raising render
        drops only that block). Passing both or neither raises ``ValueError`` at
        declaration so the mistake surfaces in ``register()``, not deep in assembly.
        A slug colliding with another contributor's block id is resolved first-wins
        with a diagnostic when the definitions are built (mirroring tool collisions).
        """
        has_text = default_text is not None
        has_render = render is not None
        if has_text == has_render:
            raise ValueError("register_prompt_block requires exactly one of default_text / render")
        self._declarations.prompt_blocks.append(
            PromptBlockDeclaration(slug=slug, default_text=default_text, render=render)
        )

    def on_startup(self, handler: LifecycleHandler) -> None:
        """Declare a startup handler (sync or async, no args) fired post-bootstrap."""
        self._declarations.startup.append(handler)

    def on_shutdown(self, handler: LifecycleHandler) -> None:
        """Declare a shutdown handler (sync or async, no args) fired on runtime stop."""
        self._declarations.shutdown.append(handler)
