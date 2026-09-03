"""Repository-level invariants.

The privacy rules only hold if the working tree cannot carry an identifier
into a commit.  These tests check the repository itself, not the code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from uniden_r8.privacy import looks_like_identifier, looks_like_position

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


def test_docs_carry_no_coordinates():
    """The other half of the privacy invariant, and it had no control.

    `evidence.publish()` refuses a document containing a position, and
    `looks_like_identifier` keeps addresses out of every committable file -- but
    nothing checked the documentation for a coordinate, even though the docs are
    where a real one would most plausibly be pasted while writing up a POI or
    GNSS result. This project's own handoff lists "no coordinate in published
    output" as an invariant; a stated invariant with no test is a convention.

    Scoped to prose rather than to every file because a coordinate has no
    unambiguous shape: a decimal pair is also a version range, a byte histogram,
    or a pair of measurements. Prose is where the risk actually is.
    """
    offenders = []
    for path in [*sorted(REPO_ROOT.glob("docs/*.md")), REPO_ROOT / "README.md"]:
        if not path.exists():
            continue
        if looks_like_position(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"coordinate-shaped text in documentation: {offenders}"


def test_the_coordinate_scan_actually_catches_one():
    """A control that cannot fail proves nothing.

    Demonstrates that the check above would catch a real paste rather than
    passing because `looks_like_position` never returns true for prose.
    """
    # Assembled rather than written out, for the same reason `fixtures.py`
    # builds addresses from octets: the file that proves the control works
    # must not itself be the thing the control is looking for.
    latitude = f"{33.0 + 0.4484:.4f}"
    longitude = f"{-(112.0 + 0.0740):.4f}"
    assert looks_like_position(f"the fix came back {latitude}, {longitude} at 04:12Z")
    assert not looks_like_position("324 of 324 packets, 0 unparsed, 2.6 ms")


#: Shell scripts shipped in this repository.
SHELL_SCRIPTS = sorted((REPO_ROOT / "scripts").glob("*.sh"))

#: A pipeline whose reader exits early, under `set -o pipefail`.
#:
#: `grep -q` and `head` stop reading at the first match or line. The writer then
#: dies of SIGPIPE with status 141, and `pipefail` makes that the pipeline's
#: status -- so the pipeline reports *failure on success*. Every occurrence of
#: this pattern in this repository has been a real bug, twice in the same week:
#: an installer that reported a present systemd unit as missing, and an
#: uninstaller that refused to uninstall anything on exactly the nodes where the
#: unit existed. Both were found on hardware rather than by reading.
_EARLY_EXIT_READER = re.compile(r"\|\s*(grep\s+-[a-zA-Z]*q|head\b)")


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_no_script_pipes_into_an_early_exiting_reader_under_pipefail(path):
    """`cmd | grep -q` reports failure on a *match* when pipefail is set.

    The consequence is not a crash, which is why it survives review: the script
    runs to completion and quietly takes the wrong branch. Use `grep -c` (which
    reads to EOF), or ask the tool for the one thing directly, or drop the pipe.
    """
    text = path.read_text(encoding="utf-8")
    if "pipefail" not in text:
        return
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _EARLY_EXIT_READER.search(line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "a pipe into an early-exiting reader under `set -o pipefail` reports "
        "failure when it matches:\n  " + "\n  ".join(offenders)
    )


def test_the_pipefail_scan_actually_catches_one():
    """A control that cannot fail proves nothing.

    Demonstrates the pattern above is detected, so the test is not passing
    merely because the regex never matches anything.
    """
    assert _EARLY_EXIT_READER.search('systemctl list-unit-files | grep -q "^x"')
    assert _EARLY_EXIT_READER.search("ps -ef | head -1")
    assert not _EARLY_EXIT_READER.search('grep -q "x" somefile.txt')
    assert not _EARLY_EXIT_READER.search("systemctl list-unit-files | grep -c .")


def test_the_shipped_scripts_are_syntactically_valid():
    """`bash -n` every script, so a typo cannot reach a vehicle.

    These run on a node in a truck, usually with sudo, often when something is
    already wrong. A syntax error found there is found at the worst moment.
    """
    for path in SHELL_SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr.strip()}"
