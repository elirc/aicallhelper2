"""Release metadata: one version everywhere, every declared dependency pinned (R13)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import release_meta

ROOT = Path(__file__).resolve().parent.parent


def _copy_tree(tmp_path: Path) -> Path:
    """The files release_meta reads, copied so a test can introduce drift safely."""
    for rel in (
        "pyproject.toml",
        "constraints.txt",
        "installer.iss",
        "frontend/package.json",
        "frontend/package-lock.json",
    ):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst)
    (tmp_path / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_repository_release_metadata_is_consistent() -> None:
    # The gate CI runs. Before R13 the lockfile root still said 3.0.0 and
    # pyproject declared no dependencies at all.
    assert release_meta.check(ROOT) == []


def test_version_is_semver_and_four_part_for_the_exe_resource() -> None:
    version = release_meta.read_version(ROOT)
    assert release_meta.version_tuple(version)[:3] == tuple(int(p) for p in version.split("."))
    assert release_meta.version_tuple(version)[3] == 0


@pytest.mark.parametrize(
    ("rel", "mutate"),
    [
        ("frontend/package.json", lambda t: t.replace('"version": "', '"version": "9', 1)),
        ("frontend/package-lock.json", lambda t: t.replace('"version": "', '"version": "9', 1)),
        ("installer.iss", lambda t: t.replace('#define AppVersion "', '#define AppVersion "9', 1)),
    ],
    ids=["package.json", "lockfile", "installer"],
)
def test_check_reports_version_drift(tmp_path: Path, rel: str, mutate: object) -> None:
    root = _copy_tree(tmp_path)
    path = root / rel
    path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")  # type: ignore[operator]
    problems = release_meta.check(root)
    assert len(problems) == 1
    assert rel.split("/")[-1] in problems[0]


def test_ui_chip_may_abbreviate_but_not_disagree(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    major, minor, _ = release_meta.read_version(root).split(".")
    chip = root / "frontend" / "src" / "Chip.tsx"
    chip.write_text(f'<span className="header-chip">v{major}.{minor}</span>', encoding="utf-8")
    assert release_meta.check(root) == []
    bumped = f'<span className="header-chip">v{major}.{int(minor) + 1}</span>'
    chip.write_text(bumped, encoding="utf-8")
    assert any("Chip.tsx" in p for p in release_meta.check(root))


def test_check_reports_an_unpinned_dependency(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    pins = root / "constraints.txt"
    pins.write_text(
        "\n".join(
            line for line in pins.read_text(encoding="utf-8").splitlines()
            if not line.lower().startswith("websockets==")
        ),
        encoding="utf-8",
    )
    assert any("websockets" in p for p in release_meta.check(root))


def test_build_info_records_revision_and_pins_without_secrets() -> None:
    info = release_meta.build_info(ROOT)
    assert info["version"] == release_meta.read_version(ROOT)
    assert isinstance(info["revision"], str) and info["revision"]
    assert info["pins"]["pywebview"]
    json.dumps(info)  # the spec writes it verbatim
    assert "key" not in json.dumps(info).lower()


def test_spec_and_installer_take_the_version_from_one_source() -> None:
    spec = (ROOT / "aica.spec").read_text(encoding="utf-8")
    assert "release_meta.read_version()" in spec
    assert "version=version_resource" in spec
    assert "build_info.json" in spec
    iss = (ROOT / "installer.iss").read_text(encoding="utf-8")
    assert "AppVersion={#AppVersion}" in iss
    assert "OutputBaseFilename=AICallAssistant-Setup-{#AppVersion}" in iss
    assert "PrivilegesRequired=lowest" in iss
