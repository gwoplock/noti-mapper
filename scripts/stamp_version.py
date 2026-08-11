#!/usr/bin/env python3
"""Copy ``noti_mapper.VERSION`` into the files that cannot read it themselves.

The PKGBUILD is shell and the man pages are roff. Neither can import a Python
attribute, so both hold a copy of the version, and copies drift. This is what
keeps them honest: one source, one command, and a test that fails if someone
edits a copy by hand.

    python scripts/stamp_version.py            # write the copies
    python scripts/stamp_version.py --check    # report drift, change nothing

``--check`` is what the test suite runs, so the failure a developer sees names
the files and the command rather than making them work it out.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from noti_mapper import VERSION  # noqa: E402 - needs the path set up first


@dataclass(frozen=True)
class Stamp:
    """One place a copy of the version lives.

    ``pattern`` must have exactly one group, around the version itself, so that
    the same expression both finds the current value and locates what to
    replace.
    """

    path: Path
    pattern: re.Pattern[str]
    description: str

    def current(self, text: str) -> str | None:
        match = self.pattern.search(text)
        if match is None:
            return None
        return match.group(1)

    def applied(self, text: str, version: str) -> str:
        """The text with the version replaced, or unchanged if it is not found."""
        match = self.pattern.search(text)
        if match is None:
            return text
        start, end = match.span(1)
        return text[:start] + version + text[end:]


STAMPS: tuple[Stamp, ...] = (
    Stamp(
        path=REPOSITORY_ROOT / "packaging" / "aur" / "noti-mapper" / "PKGBUILD",
        pattern=re.compile(r"^pkgver=(\S+)$", re.MULTILINE),
        description="the AUR package version",
    ),
    Stamp(
        path=REPOSITORY_ROOT / "dist" / "man" / "noti-mapper.1",
        pattern=re.compile(r'"noti-mapper (\S+)"'),
        description="the man page header",
    ),
    Stamp(
        path=REPOSITORY_ROOT / "dist" / "man" / "noti-mapper.d.5",
        pattern=re.compile(r'"noti-mapper (\S+)"'),
        description="the man page header",
    ),
)


def drift(version: str = VERSION) -> list[str]:
    """Return a line per copy that disagrees, empty when they all agree."""
    problems: list[str] = []
    for stamp in STAMPS:
        text = stamp.path.read_text(encoding="utf-8")
        found = stamp.current(text)
        relative = stamp.path.relative_to(REPOSITORY_ROOT)
        if found is None:
            problems.append(f"{relative}: could not find {stamp.description}")
        elif found != version:
            problems.append(f"{relative}: {stamp.description} says {found}, expected {version}")
    return problems


def stamp(version: str = VERSION) -> list[Path]:
    """Write the version into every copy. Returns the files actually changed."""
    changed: list[Path] = []
    for entry in STAMPS:
        text = entry.path.read_text(encoding="utf-8")
        updated = entry.applied(text, version)
        if updated != text:
            entry.path.write_text(updated, encoding="utf-8")
            changed.append(entry.path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report drift without writing anything"
    )
    arguments = parser.parse_args(argv)

    if arguments.check:
        problems = drift()
        if not problems:
            print(f"every copy of the version says {VERSION}")
            return 0
        for problem in problems:
            print(problem, file=sys.stderr)
        print(
            "\nThe version lives in src/noti_mapper/__init__.py. Set it there, "
            "then run: python scripts/stamp_version.py",
            file=sys.stderr,
        )
        return 1

    changed = stamp()
    if not changed:
        print(f"every copy already says {VERSION}")
        return 0
    for path in changed:
        print(f"stamped {VERSION} into {path.relative_to(REPOSITORY_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
