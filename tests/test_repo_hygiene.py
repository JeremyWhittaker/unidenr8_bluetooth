"""Repository-level invariants.

The privacy rules only hold if the working tree cannot carry an identifier
into a commit.  These tests check the repository itself, not the code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uniden_r8.privacy import looks_like_identifier

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Paths that must never be committed, whatever else is in the tree.
MUST_BE_IGNORED = (
    ".private/pi-host.txt",
    ".private/redaction.salt",
    ".private/scan-01.json",
    ".foreman/SESSION.md",
    "captures/btmon.log",
    "reports/scan.report.json",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_z(*args: str) -> list[str]:
    """Run a git command with -z and split on NUL.

    NUL-separated because a path may contain a newline, a quote, or a non-UTF-8
    byte, and git's default output quotes those in a way that silently changes
    the path.  A hygiene check that mis-parses a filename is a hygiene check
    with a hole in it.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        chunk.decode("utf-8", "surrogateescape")
        for chunk in result.stdout.split(b"\x00")
        if chunk
    ]


def committable_paths() -> list[str]:
    """Every file git would include in a commit: tracked plus non-ignored new.

    ``git status`` is the wrong tool here and this used to use it.  Status only
    reports paths that have *changed*, so once the tree is committed and clean
    it reports nothing, and a check that scans nothing passes for free.  A file
    committed today with an address in it would never be looked at again.

    ``ls-files`` reports the whole index regardless of modification, which is
    what "every file git would commit" actually means.
    """
    tracked = _git_z("ls-files", "-z")
    untracked = _git_z("ls-files", "-z", "--others", "--exclude-standard")
    return sorted(set(tracked) | set(untracked))


def _is_git_repo() -> bool:
    return (REPO_ROOT / ".git").exists()


@pytest.mark.skipif(not _is_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_private_paths_are_git_ignored(path):
    result = _git("check-ignore", "-q", path)
    assert result.returncode == 0, f"{path} is NOT ignored by .gitignore"


@pytest.mark.skipif(not _is_git_repo(), reason="not a git checkout")
def test_no_tracked_or_untracked_file_leaks_an_identifier():
    """Every file git would include in a commit, checked for an address.

    Ignored files are excluded on purpose: .private/ exists precisely to hold
    the raw material, and the previous test proves it stays ignored.
    """
    paths = committable_paths()
    assert paths, "enumeration returned nothing; the check would pass vacuously"

    offenders: list[str] = []
    for name in paths:
        candidate = REPO_ROOT / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if looks_like_identifier(text):
            offenders.append(name)

    assert not offenders, f"identifier found in files git would commit: {offenders}"


@pytest.mark.skipif(not _is_git_repo(), reason="not a git checkout")
def test_the_enumeration_includes_unchanged_tracked_files():
    """The regression this file was rewritten for.

    ``git status`` lists only *changed* paths, so on a clean tree it lists
    nothing and the scan above becomes a no-op.  This proves the enumeration
    sees a tracked file that has not been touched -- and that ``git status``
    would not have.
    """
    tracked = _git_z("ls-files", "-z")
    if not tracked:
        pytest.skip("nothing committed yet, so there is no unchanged tracked file")

    changed = {
        line[3:].strip().strip('"')
        for line in _git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    }
    unchanged = [path for path in tracked if path not in changed]
    if not unchanged:
        pytest.skip("every tracked file is currently modified")

    enumerated = set(committable_paths())
    assert unchanged[0] in enumerated
    assert unchanged[0] not in changed, "fixture is not actually unchanged"


@pytest.mark.skipif(not _is_git_repo(), reason="not a git checkout")
def test_the_enumeration_is_not_vacuous():
    """A scan of zero files passes for free; make that a failure instead."""
    assert committable_paths(), "no committable paths found"


def test_the_enumeration_survives_an_awkward_filename(tmp_path):
    """Paths with quotes or newlines must not be silently mis-parsed.

    Exercised against a scratch repository so the real tree is untouched.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    awkward = tmp_path / 'we"ird\nname.txt'
    awkward.write_text("harmless")
    listed = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "-z", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    )
    names = [c.decode("utf-8", "surrogateescape")
             for c in listed.stdout.split(b"\x00") if c]
    assert 'we"ird\nname.txt' in names


def test_the_gitignore_covers_the_private_directory():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".private/", ".foreman/", "captures/", "reports/"):
        assert pattern in ignore, f"{pattern} missing from .gitignore"


def test_docs_carry_no_identifiers():
    """Documentation is the most likely place for an address to be pasted."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(REPO_ROOT.glob("docs/*.md"))
        if looks_like_identifier(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"identifier in documentation: {offenders}"


def test_the_readme_carries_no_identifiers():
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        assert not looks_like_identifier(readme.read_text(encoding="utf-8"))
