from __future__ import annotations

import pytest

from tests.conftest import _selected_shard, in_shard

NODE_IDS = [f"tests/core/test_module_{n}.py::test_case[{n % 7}]" for n in range(500)]


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_shards_partition_every_test_exactly_once(count: int) -> None:
    shards = [
        {node for node in NODE_IDS if in_shard(node, index, count)} for index in range(1, count + 1)
    ]

    assert sum(len(shard) for shard in shards) == len(NODE_IDS)
    assert set().union(*shards) == set(NODE_IDS)
    assert all(shards)


@pytest.mark.parametrize("value", ["2", "0/3", "4/3", "a/b"])
def test_invalid_shard_setting_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("VBOT_TEST_SHARD", value)

    with pytest.raises(pytest.UsageError):
        _selected_shard()
