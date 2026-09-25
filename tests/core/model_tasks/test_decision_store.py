import re

import pytest

from core.database import APPLICATION_IDS, read_marker, write_bootstrap_marker
from core.model_tasks.decision_store import FORMAT_GENERATION, DecisionStore
from core.model_tasks.decision_types import DecisionError
from core.runtime.databases import canonical_database_specs

_CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.fixture
def data_dir(tmp_path):
    write_bootstrap_marker(tmp_path)
    return tmp_path


@pytest.fixture
def store(data_dir):
    store = DecisionStore(data_dir / "decisions.db")
    yield store
    store.close()


def test_revision_conflict_snapshot_retention_and_restart(store):
    first = store.save({"title": "First", "state": "original", "questions": []})
    record = store.begin(first["id"], 1, "request", {"state": "original", "revision": 1})
    changed = store.save({**first["draft"], "state": "changed"}, first["id"], 1)
    assert changed["revision"] == 2
    with pytest.raises(DecisionError) as error:
        store.save(first["draft"], first["id"], 1)
    assert error.value.code == "conflict"
    with pytest.raises(DecisionError):
        store.delete(first["id"], 2)
    store.progress(record["id"], {"step": 1})
    store.close()
    restarted = DecisionStore(store.path)
    try:
        retained = restarted.evaluation(record["id"])
        assert retained["status"] == "interrupted"
        assert _CANONICAL_TIMESTAMP.match(retained["completed_at"])
        assert retained["snapshot"]["state"] == "original"
        assert retained["result"] == {"step": 1}
        with pytest.raises(DecisionError):
            restarted.begin(first["id"], 2, "request", {})
        restarted.delete(first["id"], 2)
        assert restarted.list() == []
        with pytest.raises(DecisionError):
            restarted.evaluation(record["id"])
    finally:
        restarted.close()


def test_pagination_and_completed_records_are_immutable(store):
    exp = store.save({"title": "Paging", "state": {}, "questions": []})
    records = []
    for n in range(52):
        record = store.begin(exp["id"], 1, str(n), {})
        store.finish(record["id"], "completed", result={"n": n})
        records.append(record["id"])
    page = store.history(exp["id"])
    tail = store.history(exp["id"], page["next_before"])
    assert [row["id"] for row in page["evaluations"] + tail["evaluations"]] == list(
        reversed(records)
    )
    assert tail["next_before"] is None
    store.finish(records[-1], "cancelled")
    assert store.evaluation(records[-1])["status"] == "completed"


def test_finish_rejects_an_unknown_status_before_writing(store):
    exp = store.save({"title": "Status", "state": {}, "questions": []})
    record = store.begin(exp["id"], 1, "request", {})

    with pytest.raises(ValueError, match="unknown terminal evaluation status"):
        store.finish(record["id"], "done")

    assert store.evaluation(record["id"])["status"] == "running"


def test_reads_return_every_stored_column_by_name(store):
    saved = store.save({"title": "Columns", "state": "draft", "questions": []})
    record = store.begin(saved["id"], 1, "request", {"state": "draft"})
    with store.database.read() as db:
        experiment_columns = [row[1] for row in db.execute("PRAGMA table_info(experiments)")]
        evaluation_columns = [row[1] for row in db.execute("PRAGMA table_info(evaluations)")]

    assert list(store.get(saved["id"])) == experiment_columns
    assert list(store.evaluation(record["id"])) == evaluation_columns
    assert _CANONICAL_TIMESTAMP.match(saved["created_at"])
    assert _CANONICAL_TIMESTAMP.match(record["created_at"])


def test_decisions_database_is_a_registered_canonical_member(store, data_dir):
    marker = read_marker(data_dir)

    assert marker is not None
    assert marker.databases["decisions"].format_generation == FORMAT_GENERATION
    assert marker.databases["decisions"].database_id == store.database.database_id
    with store.database.read() as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_IDS["decisions"]
    specs = {spec.name: spec for spec in canonical_database_specs(data_dir)}
    assert specs["decisions"].path == data_dir / "decisions.db"
    assert specs["decisions"].format_generation == FORMAT_GENERATION
