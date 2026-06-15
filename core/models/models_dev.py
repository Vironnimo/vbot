"""models.dev catalog client + projection — the DUMB half of refresh.

This module owns everything the Model DB needs from models.dev: fetching the
public ``catalog.json`` once, re-verifying its live shape against the handoff's
field table before trusting it, and PROJECTING it onto disk in the file-format
the at-load assembly (``core.models.assembly``) reads. It does **no** cross-file
merging and **no** cross-provider joins — those are LOAD's job (Phase 2).

What it produces:

* the canonical base ``models.json`` (keyed by canonical id ``lab/model``), each
  entry carrying the projected base facts **plus the lifted lab-spec reasoning
  ladder** (the "Hebe-Mechanismus" — lifted from the lab's own provider section,
  no union across providers);
* the per-model **provider reasoning** the discovery layer stamps onto each
  ``<provider>.json`` model (its own ``reasoning_options``-derived control, which
  may *deviate* from the lab spec), via :func:`provider_reasoning_block`;
* the **auto canonical pointer** for a provider model whose wire-id exactly
  matches a canonical provider section (via the provider's models.dev id), via
  :func:`auto_canonical_pointer`;
* the raw ``catalog.json`` dump kept as a safety net so a later wanted field is a
  projection edit, not a re-fetch.

The runtime read path never imports this module — refresh does.

Source of truth for the data facts: ``stuff/HANDOFF-model-db.md`` (sections
"Datenquelle: models.dev", "reasoning_options — das Herzstück", "Feld-Mapping",
"Feld-Projektion", "Reasoning — Steuerung/Quelle/Snapping"). Verified against the
live public catalog 2026-06-15.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from core.models.assembly import (
    CANONICAL_FILE_NAME,
    CANONICAL_OVERRIDES_FILE_NAME,
)
from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
)
from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.reasoning import THINKING_EFFORT_ORDER
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("models.models_dev")

# The public models.dev catalog endpoint. The handoff names ``catalog.json`` as
# the single-fetch ``{models, providers}`` form; the live public URL is the
# site root path (``/api/catalog.json`` 302-redirects to the marketing page).
# Verified 2026-06-15: 215 canonical models, 145 providers, no key required.
MODELS_DEV_CATALOG_URL = "https://models.dev/catalog.json"

# Raw safety-net dump filename under ``<resources_dir>/models/`` (analog to
# ``<provider>.raw.json``). NOT read by the runtime read path — kept so a later
# wanted field is a projection edit, not a re-fetch.
RAW_CATALOG_FILE_NAME = "models.dev.catalog.raw.json"

# Discovery HTTP timeout mirrors the catalog GET timeout in ``discovery.py``;
# catalog refresh shares the chat path's transient-failure handling.
_CATALOG_HTTP_TIMEOUT_SECONDS = 60.0

# models.dev ``reasoning_options[].type`` values → vBot ``reasoning.control``.
# Effort wins when both effort and another type are present (handoff decision).
_MODELS_DEV_TYPE_EFFORT = "effort"
_MODELS_DEV_TYPE_BUDGET_TOKENS = "budget_tokens"
_MODELS_DEV_TYPE_TOGGLE = "toggle"

# models.dev ``interleaved`` → the per-model reasoning RESPONSE field (Phase 5):
# which wire field the response returns reasoning in. Shapes:
# ``{"field": "reasoning_content"}`` / ``{"field": "reasoning_details"}`` (named
# field) and bare ``true`` (interleaved, no field-name override). Only the
# field-named shape carries a usable selector; bare ``true`` yields ``None`` so
# the adapter keeps its hardcoded default-key scan.
_MODELS_DEV_INTERLEAVED_FIELD_KEY = "field"


class ModelsDevError(VBotError):
    """A models.dev catalog fetch / shape-verification failure.

    Raised when the catalog cannot be fetched, parsed, or — critically — when
    the live shape diverges from the handoff's field table. A shape divergence
    aborts projection with a clear message rather than silently writing a wrong
    catalog (the raw dump is still the recovery path).
    """


class ModelsDevCatalog:
    """A parsed, shape-verified models.dev ``catalog.json``.

    Holds the two top-level maps the projection needs — ``models`` (canonical
    base, keyed ``lab/model``) and ``providers`` (per-provider sections, each
    with the rich ``reasoning_options`` / per-provider limits). Construction
    re-verifies the live shape (:meth:`_verify_shape`); a divergence raises
    :class:`ModelsDevError`.
    """

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = raw
        models = raw.get("models")
        providers = raw.get("providers")
        self._verify_shape(models, providers)
        # Verified above to be dict-of-dict maps.
        self._models: dict[str, dict[str, Any]] = dict(models)  # type: ignore[arg-type]
        self._providers: dict[str, dict[str, Any]] = dict(providers)  # type: ignore[arg-type]

    @staticmethod
    def _verify_shape(models: Any, providers: Any) -> None:
        """Re-verify the live catalog shape against the handoff field table.

        Aborts (raises :class:`ModelsDevError`) the moment the structure
        diverges from what the projection assumes — before any file is written.
        Checks the two top-level maps exist and are dict-keyed, then spot-checks
        one canonical model and one provider model for the fields the projection
        actually reads (``modalities.input/output``, ``limit``, ``reasoning``,
        provider ``models`` + ``reasoning_options`` shape). The raw dump remains
        the recovery path on divergence.
        """

        if not isinstance(models, Mapping) or not models:
            raise ModelsDevError(
                "models.dev catalog: top-level 'models' must be a non-empty object "
                f"(got {type(models).__name__}) — live shape diverged from the handoff"
            )
        if not isinstance(providers, Mapping) or not providers:
            raise ModelsDevError(
                "models.dev catalog: top-level 'providers' must be a non-empty object "
                f"(got {type(providers).__name__}) — live shape diverged from the handoff"
            )

        sample_id, sample_model = next(iter(models.items()))
        if not isinstance(sample_model, Mapping):
            raise ModelsDevError(
                f"models.dev catalog: model '{sample_id}' is not an object — live shape diverged"
            )
        modalities = sample_model.get("modalities")
        if not isinstance(modalities, Mapping) or "input" not in modalities:
            raise ModelsDevError(
                f"models.dev catalog: model '{sample_id}' missing 'modalities.input' — "
                "live shape diverged from the handoff field table"
            )
        if "reasoning" not in sample_model:
            raise ModelsDevError(
                f"models.dev catalog: model '{sample_id}' missing 'reasoning' flag — "
                "live shape diverged from the handoff field table"
            )

        provider_id, provider = next(iter(providers.items()))
        if not isinstance(provider, Mapping) or not isinstance(provider.get("models"), Mapping):
            raise ModelsDevError(
                f"models.dev catalog: provider '{provider_id}' missing a 'models' object — "
                "live shape diverged from the handoff field table"
            )

    @property
    def models(self) -> Mapping[str, dict[str, Any]]:
        """The canonical base map, keyed by canonical id ``lab/model``."""

        return self._models

    @property
    def providers(self) -> Mapping[str, dict[str, Any]]:
        """The per-provider sections, keyed by models.dev provider id."""

        return self._providers

    @property
    def raw(self) -> Mapping[str, Any]:
        """The full parsed catalog (for the raw safety-net dump)."""

        return self._raw

    def provider_model(self, models_dev_id: str, wire_id: str) -> Mapping[str, Any] | None:
        """Return one provider section's model by exact wire-id, or ``None``.

        The exact-match lookup the deterministic join relies on — no fuzzy
        matching. ``models_dev_id`` is the provider's models.dev key (which may
        differ from the vBot provider id); ``wire_id`` is the exact id the
        provider expects on the wire.
        """

        provider = self._providers.get(models_dev_id)
        if not isinstance(provider, Mapping):
            return None
        models = provider.get("models")
        if not isinstance(models, Mapping):
            return None
        model = models.get(wire_id)
        return model if isinstance(model, Mapping) else None


async def fetch_catalog(
    *,
    url: str = MODELS_DEV_CATALOG_URL,
    client: httpx.AsyncClient | None = None,
) -> ModelsDevCatalog:
    """Fetch and shape-verify the public models.dev ``catalog.json``.

    Reuses the project's HTTP + retry conventions: the GET runs inside
    ``retry_async`` with ``wrap_network_error`` / ``classify_http_status`` so
    transport/timeout errors and retryable statuses (429/502/503/504, honoring
    ``Retry-After``) are re-issued with backoff while fatal statuses abort. No
    auth header — the catalog is a public endpoint.

    Args:
        url: The catalog endpoint (defaults to :data:`MODELS_DEV_CATALOG_URL`).
        client: An optional pre-built ``httpx.AsyncClient`` (tests inject a
            mock transport). When ``None``, a short-lived client is created.

    Returns:
        A shape-verified :class:`ModelsDevCatalog`.

    Raises:
        ModelsDevError: On a non-retryable HTTP/parse failure or a shape
            divergence from the handoff field table.
    """

    async def _request() -> ModelsDevCatalog:
        owns_client = client is None
        active = client or httpx.AsyncClient(timeout=_CATALOG_HTTP_TIMEOUT_SECONDS)
        try:
            try:
                response = await active.get(url)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            if response.status_code >= 400:
                body = response.text
                detail = (
                    f"{response.status_code} {body}".strip() if body else str(response.status_code)
                )
                classify_http_status(
                    response.status_code,
                    detail=detail,
                    response_headers=response.headers,
                )
            payload = response.json()
        finally:
            if owns_client:
                await active.aclose()
        if not isinstance(payload, Mapping):
            raise ModelsDevError("models.dev catalog response was not a JSON object")
        return ModelsDevCatalog(payload)

    return await retry_async(_request)


def write_raw_catalog(catalog: ModelsDevCatalog, models_dir: Path) -> Path:
    """Write the raw ``catalog.json`` dump as the safety net; return its path.

    Analog to ``<provider>.raw.json``: kept so a later wanted field is a
    projection edit, not a re-fetch. The runtime read path never reads it
    (``is_provider_file`` rejects it by extension).
    """

    models_dir.mkdir(parents=True, exist_ok=True)
    raw_path = models_dir / RAW_CATALOG_FILE_NAME
    raw_path.write_text(
        json.dumps(catalog.raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return raw_path


def write_canonical_models(
    projected: Mapping[str, dict[str, Any]],
    models_dir: Path,
) -> Path:
    """Write the canonical ``models.json`` from a projected map; return its path.

    The file is the canonical base the at-load assembly reads (keyed by
    canonical id, no ``provider_id``). Overwritten wholesale on every refresh —
    "model catalogs are refreshable artifacts".
    """

    models_dir.mkdir(parents=True, exist_ok=True)
    canonical_path = models_dir / CANONICAL_FILE_NAME
    canonical_path.write_text(
        json.dumps({"models": dict(projected)}, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return canonical_path


def seed_canonical_overrides_structure(models_dir: Path) -> Path | None:
    """Create an empty ``models.overrides.json`` structure when none exists yet.

    The hand layer for the ~31 lab-keying / no-lab-provider cases whose ladder
    cannot be lifted deterministically. Seeded only with an empty ``models``
    object + a guidance comment so a human can add ``{control, levels}`` ladders
    by hand; existing files are left untouched (never clobber hand edits).

    Returns the path when freshly created, or ``None`` when it already existed.
    """

    overrides_path = models_dir / CANONICAL_OVERRIDES_FILE_NAME
    if overrides_path.exists():
        return None
    models_dir.mkdir(parents=True, exist_ok=True)
    seed = {
        "_comment": (
            "Hand layer for canonical reasoning ladders that cannot be lifted "
            "deterministically from a lab provider (lab keys the model differently, "
            "or there is no lab provider). Add entries keyed by canonical id "
            "lab/model with a 'capabilities.reasoning' block, e.g. "
            '{"capabilities": {"reasoning": {"supported": true, "control": "levels", '
            '"levels": ["low", "high"]}}}. See .vorch/FLAGGED.md for the unseeded list.'
        ),
        "models": {},
    }
    overrides_path.write_text(
        json.dumps(seed, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return overrides_path


async def refresh_canonical_layer(
    resources_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    catalog: ModelsDevCatalog | None = None,
) -> dict[str, Any]:
    """Fetch + project the canonical layer (``models.json`` + raw dump + seeds).

    The canonical half of refresh: fetch the public catalog (free, no key),
    write the raw safety-net dump, project ``catalog.models`` into
    ``models.json`` with lifted ladders, and seed an empty
    ``models.overrides.json`` structure when absent. Returns a small report
    (model count + how many models lift a ladder vs. fall to the hand path).

    Args:
        resources_dir: The resources root (a ``models/`` subdir is created).
        client: Optional injected HTTP client (tests pass a mock transport).
        catalog: Optional pre-fetched catalog to reuse instead of fetching
            (e.g. when a per-provider refresh already fetched it this run).

    Returns:
        ``{"model_count", "lifted_ladders", "hand_path_reasoning", "raw_path"}``.
    """

    resolved_catalog = catalog or await fetch_catalog(client=client)
    models_dir = resources_dir / "models"
    raw_path = write_raw_catalog(resolved_catalog, models_dir)
    projected = project_canonical_models(resolved_catalog)
    write_canonical_models(projected, models_dir)
    seed_canonical_overrides_structure(models_dir)

    lifted, hand_path_reasoning = _ladder_lift_counts(resolved_catalog)
    return {
        "model_count": len(projected),
        "lifted_ladders": lifted,
        "hand_path_reasoning": hand_path_reasoning,
        "raw_path": str(raw_path),
    }


def _ladder_lift_counts(catalog: ModelsDevCatalog) -> tuple[int, int]:
    """Count reasoning-capable canonical models that lift vs. need the hand path.

    Returns ``(lifted, hand_path_reasoning)``: how many reasoning-capable
    canonical models lifted a control block from their lab provider, and how
    many reasoning-capable ones did NOT (the hand-path candidates the orchestrator
    report and FLAGGED.md call out).
    """

    lifted = 0
    hand_path_reasoning = 0
    for canonical_id, model in catalog.models.items():
        if not isinstance(model, Mapping) or not model.get("reasoning"):
            continue
        if lift_canonical_ladder(catalog, canonical_id) is not None:
            lifted += 1
        else:
            hand_path_reasoning += 1
    return lifted, hand_path_reasoning


def derive_reasoning_control(
    reasoning_options: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Derive the typed reasoning control block from ``reasoning_options``.

    Data-driven, per the handoff (decision: "Effort gewinnt, wenn vorhanden"):

    * an ``effort`` option → ``control: "levels"`` with its ``values`` as
      ``levels`` (effort WINS when both effort and another type are present);
    * else a ``budget_tokens`` option → ``control: "budget"`` (plus
      ``budget_max`` when models.dev gives a ``max``);
    * else a ``toggle`` option → ``control: "on_off"``.

    Returns the partial reasoning dict (``control`` + any ``levels``/
    ``budget_max``), or ``None`` when no usable option is present so the caller
    can fall back to the bare ``{"supported": ...}`` form. Only thinking efforts
    known to vBot (``THINKING_EFFORT_ORDER``) are kept as levels.
    """

    if not reasoning_options:
        return None

    effort_option: Mapping[str, Any] | None = None
    budget_option: Mapping[str, Any] | None = None
    toggle_option: Mapping[str, Any] | None = None
    for option in reasoning_options:
        if not isinstance(option, Mapping):
            continue
        option_type = option.get("type")
        if option_type == _MODELS_DEV_TYPE_EFFORT and effort_option is None:
            effort_option = option
        elif option_type == _MODELS_DEV_TYPE_BUDGET_TOKENS and budget_option is None:
            budget_option = option
        elif option_type == _MODELS_DEV_TYPE_TOGGLE and toggle_option is None:
            toggle_option = option

    if effort_option is not None:
        levels = _known_effort_levels(effort_option.get("values"))
        return {"control": REASONING_CONTROL_LEVELS, "levels": levels}
    if budget_option is not None:
        block: dict[str, Any] = {"control": REASONING_CONTROL_BUDGET}
        budget_max = budget_option.get("max")
        if isinstance(budget_max, int) and not isinstance(budget_max, bool):
            block["budget_max"] = budget_max
        return block
    if toggle_option is not None:
        return {"control": REASONING_CONTROL_ON_OFF}
    return None


def reasoning_capability_block(
    *,
    supported: bool,
    reasoning_options: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Build the full typed ``reasoning`` capability block.

    Combines the ``supported`` flag (from the models.dev ``reasoning`` boolean)
    with the derived control (:func:`derive_reasoning_control`). A supported
    model with no usable ``reasoning_options`` serializes to the bare
    ``{"supported": true}`` form — no fabricated ladder.
    """

    block: dict[str, Any] = {"supported": supported}
    if not supported:
        return block
    control = derive_reasoning_control(reasoning_options)
    if control is not None:
        block.update(control)
    return block


def project_canonical_models(catalog: ModelsDevCatalog) -> dict[str, dict[str, Any]]:
    """Project ``catalog.models`` into the canonical ``models.json`` ``models`` map.

    Builds one record per canonical id, applying the field projection from the
    handoff/phase table and the lifted lab-spec reasoning ladder. The output
    matches the assembly file-format contract EXACTLY: each record carries
    ``name``, ``family`` (when known), ``capabilities`` (with the typed
    ``reasoning`` block, modalities stored VERBATIM incl. ``pdf``/``video``),
    ``context_window``, ``max_output_tokens``, plus the kept reserve fields. No
    ``provider_id`` (this is not a provider file).

    Returns:
        A mapping ``canonical_id -> record`` ready to write under ``{"models": …}``.
    """

    projected: dict[str, dict[str, Any]] = {}
    for canonical_id, model in catalog.models.items():
        if not isinstance(model, Mapping):
            continue
        projected[canonical_id] = _project_canonical_model(catalog, canonical_id, model)
    return projected


def lift_canonical_ladder(catalog: ModelsDevCatalog, canonical_id: str) -> dict[str, Any] | None:
    """Lift the lab-spec reasoning control for a canonical id, or ``None``.

    For canonical id ``lab/X`` take the LAB provider ``lab`` (its models.dev id
    equals the lab name for the lifted cases) and read its model with wire-id
    ``X``; derive that model's reasoning control. NO union across providers — the
    canonical ladder is the lab's own spec only.

    Returns the derived control block (``control`` + ``levels``/``budget_max``),
    or ``None`` when there is no lab provider, the lab does not key the model by
    wire-id ``X``, or the lab model carries no usable ``reasoning_options`` (the
    ~31 hand-path cases the handoff describes — they get seeded by hand in
    ``models.overrides.json``, not fabricated here).
    """

    lab, _, wire_id = canonical_id.partition("/")
    if not wire_id:
        return None
    lab_model = catalog.provider_model(lab, wire_id)
    if lab_model is None:
        return None
    return derive_reasoning_control(_reasoning_options_of(lab_model))


def auto_canonical_pointer(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> str | None:
    """Return the auto ``canonical`` pointer for a provider model, or ``None``.

    A safe, deterministic exact-match only: when the provider's models.dev
    section (looked up by ``models_dev_id``) contains the exact ``wire_id``, the
    canonical id is ``"<models_dev_id>/<wire_id>"`` *if and only if* that id is a
    real key in the canonical layer. No fuzzy matching — a missed pointer is not
    damage (the at-load join also covers exact canonical-id wire-ids).

    Note: this is the lab-style pointer (``lab/model``). It is written into
    ``<provider>.json`` only for the provider that *is* the lab for that model;
    for resellers (OpenRouter, etc.) the wire-id usually already equals the
    canonical id, which the at-load exact-canonical-id match resolves without a
    stored pointer.
    """

    if catalog.provider_model(models_dev_id, wire_id) is None:
        return None
    candidate = f"{models_dev_id}/{wire_id}"
    if candidate in catalog.models:
        return candidate
    return None


def provider_reasoning_block(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> dict[str, Any] | None:
    """Return the per-provider reasoning block when the provider DEVIATES.

    Reads the provider's own ``reasoning_options`` (per-provider ``control``
    derivation runs here too) and compares against the lab spec. Returns the
    full typed reasoning block to stamp on the ``<provider>.json`` model ONLY
    when the provider reports a control that differs from the lab spec — so the
    canonical inheritance handles the common (non-deviating) case and the
    provider layer stays minimal. Returns ``None`` when the provider does not
    deviate (or carries no usable options), letting the model inherit the
    canonical ladder at load.
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None
    provider_control = derive_reasoning_control(_reasoning_options_of(provider_model))
    if provider_control is None:
        return None

    canonical_id = f"{models_dev_id}/{wire_id}"
    lab_control = (
        lift_canonical_ladder(catalog, canonical_id) if canonical_id in catalog.models else None
    )
    if provider_control == lab_control:
        return None
    # ``supported`` and the control derive from the SAME models.dev provider
    # section, so the stamped block is always internally consistent. Sourcing
    # ``supported`` from the adapter's normalization instead would let a thin
    # provider (whose bare ``/models`` endpoint reports no reasoning) produce an
    # invalid ``supported: false`` block carrying an effort ladder.
    return {"supported": bool(provider_model.get("reasoning")), **provider_control}


def reasoning_response_field(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> str | None:
    """Return the per-model reasoning RESPONSE field, or ``None`` (Phase 5).

    Projects the provider section's models.dev ``interleaved`` value into the
    field-name selector the adapter reads from ``metadata.<provider>.\
    reasoning_response_field``: ``{"field": "reasoning_content"}`` /
    ``{"field": "reasoning_details"}`` → that field name. Bare ``interleaved:
    true`` (no field-name override) and an absent ``interleaved`` both yield
    ``None`` so the adapter keeps its hardcoded default-key scan (graceful).
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None
    interleaved = provider_model.get("interleaved")
    if not isinstance(interleaved, Mapping):
        return None
    field_name = interleaved.get(_MODELS_DEV_INTERLEAVED_FIELD_KEY)
    return field_name if isinstance(field_name, str) and field_name else None


def provider_limits(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> tuple[int | None, int | None]:
    """Return ``(context_window, max_output_tokens)`` from the provider's section.

    A gateway/aggregator endpoint often returns bare ids with NO limits (e.g.
    opencode-go's ``/models`` reports neither a context window nor an output cap),
    but models.dev carries the per-provider section with the real limits THAT
    PROVIDER offers — which may legitimately differ from the canonical base, since
    a provider can cap the window or the output (e.g. opencode-go ``glm-5`` output
    32768 vs the canonical 131072). Refresh projects these into the provider layer
    so the on-disk ``<provider>.json`` carries the provider's own limits rather
    than a hand-guessed override. Returns ``(None, None)`` when the provider
    section has no entry for ``wire_id`` or no ``limit`` block.
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None, None
    raw_limit = provider_model.get("limit")
    limit: Mapping[str, Any] = raw_limit if isinstance(raw_limit, Mapping) else {}
    return _optional_int(limit.get("context")), _optional_int(limit.get("output"))


def provider_modalities(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> tuple[list[str], list[str]] | None:
    """Return ``(input_modalities, output_modalities)`` from the provider section.

    A bare gateway endpoint reports no modalities, so the adapter falls back to a
    text-only default; models.dev's per-provider section carries the real ones
    (e.g. opencode-go ``minimax-m3`` is ``text/image/video``). The caller applies
    these only when they are a strict SUPERSET of what the endpoint reported, so
    enrichment can only ADD modalities, never drop a real one the endpoint knew.
    Returns ``None`` when the provider section has no entry or no modalities.
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None
    modalities = provider_model.get("modalities")
    if not isinstance(modalities, Mapping):
        return None
    input_modalities = [m for m in _string_list(modalities.get("input")) if m]
    output_modalities = [m for m in _string_list(modalities.get("output")) if m]
    if not input_modalities and not output_modalities:
        return None
    return input_modalities, output_modalities


def provider_reasoning_supported(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> bool | None:
    """Return the bare ``reasoning`` capability flag from the provider's section.

    models.dev marks each provider model reasoning-capable with a top-level
    ``reasoning`` boolean (independent of whether it publishes a control ladder in
    ``reasoning_options``). A gateway model whose wire-id cannot reach the
    canonical layer would otherwise keep the endpoint's default ``supported:
    false`` even when the feed says it can reason; the caller projects this flag so
    the capability survives without a hand override. Returns ``None`` when the
    provider section has no entry for ``wire_id``.
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None
    return bool(provider_model.get("reasoning"))


def provider_family(
    catalog: ModelsDevCatalog,
    *,
    models_dev_id: str,
    wire_id: str,
) -> str | None:
    """Return the model ``family`` from the provider's models.dev section, or ``None``.

    A bare gateway endpoint reports no family; models.dev's per-provider section
    carries one. Projected into the provider layer so ``Model.family`` is populated
    without the canonical join (which a gateway wire-id usually cannot reach).
    """

    provider_model = catalog.provider_model(models_dev_id, wire_id)
    if provider_model is None:
        return None
    family = provider_model.get("family")
    return family if isinstance(family, str) and family else None


# ---------------------------------------------------------------------------
# Canonical projection internals
# ---------------------------------------------------------------------------

# Field projection (handoff "Feld-Projektion"): keep what plausibly helps —
# storage is cheap, a missing field later is expensive. These are carried
# verbatim from the canonical model onto the projected record's top level.
# ``id``/``name``/``family``/``modalities``/``limit``/``reasoning`` are handled
# explicitly; these are the remaining "keep" fields (status/interleaved live on
# the per-provider side, not the canonical base).
_CANONICAL_KEEP_FIELDS = (
    "temperature",
    "cost",
    "knowledge",
    "release_date",
    "last_updated",
    "experimental",
)

# The ``temperature`` boolean is a real ``supported_parameters`` signal (some
# models report ``false``). A ``true`` value contributes the ``temperature``
# parameter name to the capability's supported-parameters list.
_TEMPERATURE_PARAMETER_NAME = "temperature"


def _project_canonical_model(
    catalog: ModelsDevCatalog,
    canonical_id: str,
    model: Mapping[str, Any],
) -> dict[str, Any]:
    modalities = model.get("modalities")
    input_modalities = (
        _string_list(modalities.get("input")) if isinstance(modalities, Mapping) else []
    )
    output_modalities = (
        _string_list(modalities.get("output")) if isinstance(modalities, Mapping) else []
    )

    supported = bool(model.get("reasoning"))
    reasoning_block: dict[str, Any] = {"supported": supported}
    if supported:
        lifted = lift_canonical_ladder(catalog, canonical_id)
        if lifted is not None:
            reasoning_block.update(lifted)

    supported_parameters: list[str] = []
    if model.get(_TEMPERATURE_PARAMETER_NAME) is True:
        supported_parameters.append(_TEMPERATURE_PARAMETER_NAME)

    capabilities: dict[str, Any] = {
        "vision": "image" in input_modalities,
        "tools": bool(model.get("tool_call")),
        # structured_output is absent for many models → json_mode false.
        "json_mode": model.get("structured_output") is True,
        "reasoning": reasoning_block,
        "input_modalities": input_modalities,
        "output_modalities": output_modalities or ["text"],
    }
    if supported_parameters:
        capabilities["supported_parameters"] = supported_parameters

    raw_limit = model.get("limit")
    limit: Mapping[str, Any] = raw_limit if isinstance(raw_limit, Mapping) else {}
    record: dict[str, Any] = {
        "name": _string_or(model.get("name"), canonical_id),
        "capabilities": capabilities,
        "context_window": _optional_int(limit.get("context")),
        "max_output_tokens": _optional_int(limit.get("output")),
    }
    family = model.get("family")
    if isinstance(family, str) and family:
        record["family"] = family
    for field_name in _CANONICAL_KEEP_FIELDS:
        if field_name in model:
            record[field_name] = model[field_name]
    return record


def _reasoning_options_of(model: Mapping[str, Any]) -> Sequence[Mapping[str, Any]] | None:
    options = model.get("reasoning_options")
    if isinstance(options, Sequence) and not isinstance(options, str | bytes):
        return [opt for opt in options if isinstance(opt, Mapping)]
    return None


def _known_effort_levels(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return []
    return [value for value in values if isinstance(value, str) and value in THINKING_EFFORT_ORDER]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_or(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
