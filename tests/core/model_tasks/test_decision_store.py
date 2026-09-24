import pytest

from core.model_tasks.decision_store import DecisionStore
from core.model_tasks.decision_types import DecisionError


def test_revision_conflict_snapshot_retention_and_restart(tmp_path):
    store = DecisionStore(tmp_path / "decisions.db")
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
    restarted = DecisionStore(store.path)
    retained = restarted.evaluation(record["id"])
    assert retained["status"] == "interrupted"
    assert retained["snapshot"]["state"] == "original"
    assert retained["result"] == {"step": 1}
    with pytest.raises(DecisionError):
        restarted.begin(first["id"], 2, "request", {})
    restarted.delete(first["id"], 2)
    assert restarted.list() == []
    with pytest.raises(DecisionError):
        restarted.evaluation(record["id"])


def test_pagination_and_completed_records_are_immutable(tmp_path):
    store = DecisionStore(tmp_path / "decisions.db")
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


def test_reads_return_every_stored_column_by_name(tmp_path):
    store = DecisionStore(tmp_path / "decisions.db")
    saved = store.save({"title": "Columns", "state": "draft", "questions": []})
    record = store.begin(saved["id"], 1, "request", {"state": "draft"})
    with store.connection() as db:
        experiment_columns = [row[1] for row in db.execute("PRAGMA table_info(experiments)")]
        evaluation_columns = [row[1] for row in db.execute("PRAGMA table_info(evaluations)")]

    assert list(store.get(saved["id"])) == experiment_columns
    assert list(store.evaluation(record["id"])) == evaluation_columns
