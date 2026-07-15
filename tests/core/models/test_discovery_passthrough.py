"""Model discovery passthrough-filter tests."""

from __future__ import annotations

from .discovery_test_support import (
    Capabilities,
    Model,
    PassthroughModelFilter,
    PassthroughRawFilter,
    ReasoningCapabilities,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache


class TestPassthroughFilters:
    def test_raw_filter_accepts_everything(self):
        assert PassthroughRawFilter().accepts({"anything": object()}) is True

    def test_model_filter_accepts_everything(self):
        model = Model(
            model_id="model-a",
            name="Model A",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=1000,
            max_output_tokens=100,
        )

        assert PassthroughModelFilter().accepts(model) is True
