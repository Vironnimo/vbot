from __future__ import annotations

from pathlib import Path

import pytest

from scripts import domain_maps

MAPS = ".vorch/domain-maps"


def _write(root: Path, path: str, content: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(content.encode("utf-8"))


def test_corpus_report_finds_routing_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(domain_maps, "ROOT_BUDGET", 55)
    _write(tmp_path, ".vorch/PROJECT.md", "| Map |\n|---|\n| alpha.md | a |\n| gone.md | g |\n")
    _write(tmp_path, f"{MAPS}/alpha.md", "# Alpha\n\n## References\n\n- Deep -> `alpha/deep.md`\n")
    # Reached through another supplementary file.
    _write(tmp_path, f"{MAPS}/alpha/deep.md", "# Deep\n\nSee `alpha/deeper.md`.\n")
    _write(tmp_path, f"{MAPS}/alpha/deeper.md", "# Deeper\n")
    _write(tmp_path, f"{MAPS}/alpha/orphan.md", "# Orphan\n")
    _write(tmp_path, f"{MAPS}/beta.md", "# Beta\n" + "x" * 50 + "\n")

    report = domain_maps.corpus_report(tmp_path)

    assert list(report.sizes) == [f"{MAPS}/beta.md", f"{MAPS}/alpha.md"]
    assert sorted(str(finding) for finding in report.findings) == [
        ".vorch/PROJECT.md: Domain Maps index lists `.vorch/domain-maps/gone.md`, "
        "which is no root map",
        f"{MAPS}/alpha/orphan.md: no map references this supplementary file",
        f"{MAPS}/beta.md: root map is missing from the Domain Maps index in .vorch/PROJECT.md",
    ]
    assert "1 over budget" in domain_maps.format_report(report)
