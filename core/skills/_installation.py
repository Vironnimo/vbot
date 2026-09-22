"""Package import inside the Skill authoring owner: prepare completely, then publish."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.skills._packages import (
    INSTALL_RECEIPT,
    MAX_SKILL_DOCUMENT_BYTES,
    PackageError,
    PackageFile,
    package_path,
    package_roots,
    read_archive,
    read_directory,
    unwrap_archive,
)
from core.skills._sources import SkillSource, load_source
from core.skills.requirements import RequirementParseError, parse_vbot_requirements
from core.skills.skill_validator import (
    normalize_and_validate_skill_metadata,
    parse_skill_front_matter,
    split_skill_document,
)
from core.utils.atomic import atomic_write_bytes

if TYPE_CHECKING:
    from core.skills.authoring import SkillAuthoringService


@dataclass(frozen=True)
class SkillInstallResult:
    operation: str
    source: str
    name: str | None = None
    package_path: str | None = None
    files: int = 0
    sha256: str | None = None
    warnings: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate(files: dict[str, PackageFile], path: str, fallback: str) -> dict[str, Any]:
    entry = files[f"{path}/SKILL.md" if path else "SKILL.md"].content
    if len(entry) > MAX_SKILL_DOCUMENT_BYTES:
        raise PackageError("SKILL.md exceeds the 1 MiB document limit.")
    try:
        content = entry.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PackageError("SKILL.md must be UTF-8 text.") from error
    front, body, warnings = split_skill_document(content)
    fields, parse_warnings = parse_skill_front_matter(front)
    # The installed directory takes the declared name, not an archive/download basename.
    declared_name = fields.get("name") if isinstance(fields, dict) else None
    directory_name = (
        declared_name.strip()
        if isinstance(declared_name, str) and declared_name.strip()
        else Path(path).name
        if path
        else fallback
    )
    fields, validation = normalize_and_validate_skill_metadata(
        fields,
        directory_name=directory_name,
        skill_file=Path(path) / "SKILL.md",
        body=body,
        parse_warnings=[*warnings, *parse_warnings],
    )
    if not validation.valid:
        raise PackageError("Invalid Skill metadata: " + "; ".join(validation.warnings))
    try:
        metadata = fields.get("metadata")
        parse_vbot_requirements(metadata if isinstance(metadata, dict) else {})
    except RequirementParseError as error:
        raise PackageError(str(error)) from error
    return {
        "name": fields["name"],
        "path": path or ".",
        "description": fields["description"],
        "warnings": validation.warnings,
    }


def _digest(files: dict[str, PackageFile]) -> str:
    digest = hashlib.sha256()
    for path, file in sorted(files.items()):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(len(file.content).to_bytes(8, "big"))
        digest.update(file.content)
        # Windows cannot persist POSIX executable bits; content identity is still stable.
        if os.name != "nt":
            digest.update(bytes([file.executable]))
    return digest.hexdigest()


def install_package(
    authoring: SkillAuthoringService,
    target_root: Path,
    source: str,
    *,
    path: str | None = None,
    ref: str | None = None,
    replace: bool = False,
    dry_run: bool = False,
    archive: bytes | None = None,
    expected_sha256: str | None = None,
) -> SkillInstallResult:
    if archive is None:
        bundle = load_source(source, ref=ref)
    else:
        if ref is not None:
            raise PackageError("A Git revision cannot be used with an uploaded archive.")
        filename = source.replace("\\", "/").rsplit("/", 1)[-1]
        package_path(filename)
        bundle = SkillSource(
            unwrap_archive(read_archive(archive)), f"upload:{filename}", Path(filename).stem
        )
    if path is not None and bundle.path is not None and path.strip("/") != bundle.path.strip("/"):
        raise PackageError("--path conflicts with the directory in the supplied source link.")
    selection = path if path is not None else bundle.path
    if selection == ".":
        selection = ""
    elif selection is not None:
        selection = package_path(selection)
    roots = [selection] if selection is not None else package_roots(bundle.files)
    if not roots:
        raise PackageError(
            "Source contains no SKILL.md. Use a Skill package or a repository containing Skills."
        )
    candidates = []
    for root in roots:
        document_path = f"{root}/SKILL.md" if root else "SKILL.md"
        if document_path not in bundle.files:
            raise PackageError(
                "Selected directory has no SKILL.md. Use --dry-run without --path to list "
                "packages; for a GitHub branch containing '/', pass its complete --ref."
            )
        candidate = _candidate(bundle.files, root, bundle.fallback_name)
        if bundle.name is None or candidate["name"] == bundle.name:
            candidates.append(candidate)
    if len(candidates) != 1:
        if not candidates:
            raise PackageError("The linked Skill name was not found in its source repository.")
        if dry_run:
            return SkillInstallResult("candidates", bundle.source, candidates=candidates)
        paths = ", ".join(f"{item['path']} ({item['name']})" for item in candidates)
        raise PackageError(f"Source contains multiple Skills. Select one with --path: {paths}")
    selected = candidates[0]
    name = selected["name"]
    package_path(name)  # Reserved Windows basenames must also be rejected on Linux.
    selected_path = selected["path"]
    prefix = "" if selected_path == "." else selected_path + "/"
    files = {
        file_path[len(prefix) :]: value
        for file_path, value in bundle.files.items()
        if file_path.startswith(prefix)
    }
    sha256 = _digest(files)
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise PackageError(
            "The source changed since the preview. Check the source again before installing."
        )
    with authoring._write_lock:
        destination = authoring._skill_dir(target_root, name)
        exists = destination.exists()
        same = exists and destination.is_dir() and _digest(read_directory(destination)) == sha256
        operation = "unchanged" if same else "replaced" if exists else "installed"
        result = SkillInstallResult(
            operation,
            bundle.source,
            name,
            selected_path,
            len(files),
            sha256,
            list(selected["warnings"]),
        )
        if dry_run:
            return SkillInstallResult(
                "preview",
                bundle.source,
                name,
                selected_path,
                len(files),
                sha256,
                result.warnings,
                [{**selected, "exists": exists, "unchanged": same}],
            )
        if same:
            return result
        if exists and not replace:
            raise PackageError(
                f"Skill '{name}' already exists with different files. Inspect it first; "
                "use --replace --yes to replace the complete package."
            )
        if exists and (not destination.is_dir() or not (destination / "SKILL.md").is_file()):
            raise PackageError(
                "Existing destination is not a Skill package; it was left unchanged."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The temporary directory is a sibling, guaranteeing publication on one filesystem.
        transaction = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=destination.parent))
        preserve_backup = False
        published = False
        try:
            staged = transaction / "package"
            staged.mkdir()
            for relative, file in files.items():
                target = staged / relative
                atomic_write_bytes(target, file.content, mode=0o755 if file.executable else 0o644)
            receipt = {
                "version": 1,
                "source": bundle.source,
                "path": selected_path,
                "sha256": sha256,
                "installed_at": datetime.now(UTC).isoformat(),
            }
            atomic_write_bytes(
                staged / INSTALL_RECEIPT,
                (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            backup = transaction / "previous"
            if exists:
                destination.rename(backup)
            try:
                staged.rename(destination)
            except OSError:
                if backup.exists():
                    try:
                        backup.rename(destination)
                    except OSError as restore_error:
                        preserve_backup = True
                        raise PackageError(
                            "Installation failed and the old package could not be restored. "
                            f"Recover it from {backup.as_posix()} before retrying."
                        ) from restore_error
                raise
            published = True
        finally:
            if not preserve_backup:
                try:
                    shutil.rmtree(transaction)
                except OSError:
                    if published:
                        result.warnings.append(
                            "Installed successfully; some temporary files remain at "
                            f"{transaction.as_posix()}."
                        )
                    # On a failed write, preserve the original error and leave residue inert.
        return result
