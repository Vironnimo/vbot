# Token Pricing

Read this reference when you change token pricing, repricing, Context tiers or cost computation. The `pricing_for` contract consumers rely on stays in `models.md` -> Interfaces; historical aggregation and coverage belong to `statistics.md`.

## Price sources

`Model.pricing` is optional immutable `TokenPricing` owned by `core/models/pricing.py`. The refresh projection retains USD-per-million input/output/cache-read/cache-write/Reasoning rates with an exact `models.dev:<provider>/<wire-id>` source. Provider rates override the canonical lab price through existing layer assembly; there is no fuzzy name matching. The public-catalog refresh also reprices existing Provider entries without requiring their discovery credentials. If the exact Provider entry is absent, its previous exact price remains; if that entry is present but no longer reports cost, the old price is cleared. A Provider Adapter may instead project a price from its own catalog with a `<provider>:<wire-id>` source - today only OpenRouter embedding Models, from `pricing.prompt`, because models.dev's OpenRouter section lists no embedding Models. Such a price wins: discovery does not replace it with the models.dev price, and the public-catalog repricing skips it. Overrides remain the final hand layer. `ModelRegistry.pricing_for(bare_or_qualified_reference)` returns the effective exact price or None.

A canonical refresh's repricing of a catalog Load takes from the system root is not kept (`models/refresh.md` -> Staging and publication).

## Cost computation

Pricing selects Context tiers from the complete input of each request (including cache); explicit `tiers` take precedence over legacy `context_over_200k`. Cache reads/writes are removed from ordinary input before applying their rates. Reasoning is already in output and is split only when a distinct rate exists. Zero is a valid price, absent used bucket rates are unknown, and unsupported tier shapes are not silently treated as flat pricing. A finite Provider-reported amount wins over estimation. Prices value API usage; they do not establish actual subscription charges. Chat and Compaction snapshot this result in canonical Usage; historical aggregation and coverage belong to `statistics.md`.

## Source and tests

- `core/models/pricing.py`; `tests/core/models/test_pricing.py`
