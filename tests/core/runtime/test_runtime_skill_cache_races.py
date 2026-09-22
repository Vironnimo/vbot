"""Scoped Skill scans cannot republish snapshots invalidated during worker I/O."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.runtime.runtime import Runtime
from tests.core.runtime.runtime_test_support import config as config


@pytest.mark.parametrize("scope", ["project", "agent", "rooted_agent"])
@pytest.mark.parametrize("change", ["package", "policy", "environment"])
@pytest.mark.parametrize("rebuild_before_release", [False, True])
def test_scoped_scan_rechecks_changes_before_cache_publication(
    config, tmp_path, monkeypatch, scope, change, rebuild_before_release
):
    key = "VBOT_SCOPED_SKILL_CACHE_TEST"
    monkeypatch.delenv(key, raising=False)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    release = threading.Event()
    entered = threading.Event()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        project = runtime.projects.create("cache", "Cache", repo)
        project_id = project.project_id if scope != "agent" else None
        agent_id = None if scope == "project" else "main"
        root = (
            repo / ".opencode" / "skills"
            if scope == "project"
            else runtime.agent_skills_dir("main")
        )
        package = root / "changing"
        package.mkdir(parents=True)
        document = (
            "---\nname: changing\ndescription: Before change.\n"
            f"metadata:\n  vbot:\n    requirements:\n      env: {key}\n---\nBody\n"
        )
        path = package / "SKILL.md"
        path.write_text(document, encoding="utf-8")
        owner = runtime._skill_operations()
        method = (
            "_build_project_skill_bundle" if scope == "project" else "_build_agent_skill_registry"
        )
        original_build = getattr(owner, method)

        def controlled_build(*args):
            snapshot = original_build(*args)
            if not entered.is_set():
                entered.set()
                assert release.wait(10)
            return snapshot

        monkeypatch.setattr(owner, method, controlled_build)
        with ThreadPoolExecutor(max_workers=1) as workers:
            older = workers.submit(runtime.skills_for, project_id, agent_id)
            try:
                assert entered.wait(10)
                if change == "package":
                    path.write_text(
                        document.replace("Before change", "After change"), encoding="utf-8"
                    )
                    if scope == "project":
                        runtime.invalidate_project_skills(project_id)
                    else:
                        runtime.invalidate_agent_skills("main")
                elif change == "policy":
                    runtime.skill_policy.set_disabled("changing", disabled=True)
                    runtime.reload_skills()
                else:
                    runtime.storage.set_data_dir_credential(key, "test-value")
                    runtime.reload_environment_credentials()
                current = (
                    runtime.skills_for(project_id, agent_id) if rebuild_before_release else None
                )
                release.set()
                result = older.result(timeout=10)
                if current is not None:
                    assert result is current
                assert runtime.skills_for(project_id, agent_id) is result
                if change == "package":
                    assert result.get("changing").description == "After change."
                elif change == "policy":
                    with pytest.raises(KeyError):
                        result.get("changing")
                else:
                    assert result.availability_for("changing", ["*"]).state == "available"
            finally:
                release.set()
    finally:
        release.set()
        runtime.stop()
