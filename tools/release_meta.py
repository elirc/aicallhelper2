"""Release metadata: one version, checked everywhere, plus a build record.

pyproject.toml's ``[project] version`` is the single source of truth. Every other
place that states a version must agree with it, and this module is the one
place that knows where those are:

* ``frontend/package.json`` and both root entries of ``frontend/package-lock.json``
* ``installer.iss`` (``#define AppVersion``; build.ps1 and CI also pass it via /D)
* any literal ``vX.Y[.Z]`` chip rendered by a frontend component
* the exe's Windows version resource, which ``aica.spec`` builds from
  :func:`read_version`, so it cannot drift

It also checks that every dependency declared in pyproject.toml is pinned in
constraints.txt, so the tested set and the declared set cannot quietly diverge.

CLI (stdlib only, runs before any dependency is installed)::

    python tools/release_meta.py check              # exit 1 on any drift
    python tools/release_meta.py version            # print the version
    python tools/release_meta.py build-info OUT     # write OUT as JSON
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

_ISS_DEFINE = re.compile(r'^#define\s+AppVersion\s+"([^"]+)"', re.MULTILINE)
_UI_CHIP = re.compile(r">\s*v(\d+(?:\.\d+){1,2})\s*<")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject(root: Path) -> dict[str, Any]:
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def read_version(root: Path = ROOT) -> str:
    version = _pyproject(root)["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"pyproject version must be MAJOR.MINOR.PATCH, got {version!r}")
    return version


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """Four-part numeric form required by the Windows VS_FIXEDFILEINFO block."""
    major, minor, patch = (int(p) for p in version.split("."))
    return (major, minor, patch, 0)


def declared_versions(root: Path = ROOT) -> dict[str, str]:
    """Every version string stated outside pyproject, keyed by where it lives."""
    found: dict[str, str] = {}
    pkg = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    found["frontend/package.json"] = str(pkg.get("version"))
    lock_path = root / "frontend" / "package-lock.json"
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        found["frontend/package-lock.json (root)"] = str(lock.get("version"))
        found['frontend/package-lock.json (packages[""])'] = str(
            lock.get("packages", {}).get("", {}).get("version")
        )
    iss = (root / "installer.iss").read_text(encoding="utf-8")
    m = _ISS_DEFINE.search(iss)
    found["installer.iss #define AppVersion"] = m.group(1) if m else "<missing>"
    src = root / "frontend" / "src"
    for path in sorted(src.rglob("*.tsx")) if src.exists() else []:
        if "__tests__" in path.parts:
            continue
        for chip in _UI_CHIP.findall(path.read_text(encoding="utf-8")):
            found[f"{path.relative_to(root).as_posix()} (UI chip v{chip})"] = chip
    return found


def _declared_dependencies(root: Path) -> list[str]:
    project = _pyproject(root)["project"]
    reqs: list[str] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    names = []
    for req in reqs:
        m = _REQ_NAME.match(req)
        if m:
            names.append(m.group(1))
    return names


def pinned(root: Path = ROOT) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (root / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[_norm(name)] = ver.strip()
    return pins


def check(root: Path = ROOT) -> list[str]:
    """Human-readable problems; empty means the release metadata is consistent."""
    problems: list[str] = []
    version = read_version(root)
    for where, stated in declared_versions(root).items():
        # A UI chip may abbreviate (v3.1 for 3.1.0); everything else must match exactly.
        ok = stated == version
        if "(UI chip" in where:
            ok = ok or version.startswith(stated + ".")
        if not ok:
            problems.append(f"{where} says {stated!r}; pyproject.toml says {version!r}")
    pins = pinned(root)
    for name in _declared_dependencies(root):
        if _norm(name) not in pins:
            problems.append(f"{name} is declared in pyproject.toml but not pinned in constraints")
    return problems


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def build_info(root: Path = ROOT) -> dict[str, Any]:
    """What was built from what. No secrets, no user data, no absolute paths."""
    revision = _git(root, "rev-parse", "HEAD") or os.environ.get("GITHUB_SHA") or "unknown"
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    return {
        "version": read_version(root),
        "revision": revision,
        # True means the exe contains uncommitted changes: not a reproducible release.
        "dirty": None if status is None else bool(status),
        "builtAtUtc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "pins": pinned(root),
    }


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "version":
        print(read_version())
        return 0
    if cmd == "check":
        problems = check()
        for p in problems:
            print(f"version/pin drift: {p}", file=sys.stderr)
        if not problems:
            print(f"release metadata consistent: {read_version()}")
        return 1 if problems else 0
    if cmd == "build-info" and len(argv) == 3:
        out = Path(argv[2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(build_info(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
