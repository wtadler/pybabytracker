"""Fail unless a release tag matches the version in the package's _version.py.

Usage: check_version.py TAG [--version-file PATH]

TAG is the git tag name, with or without a leading "v" (v0.2.0 or 0.2.0). The comparison is
exact, so a pre-release tag must spell the version as _version.py does (v0.2.0rc1, not
v0.2.0-rc1), and _version.py must itself use the canonical PEP 440 spelling. Exits 0 on a
match and 1 on anything else.
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_VERSION_FILE = Path(__file__).resolve().parent.parent / "src/pybabytracker/_version.py"
# The canonical public version form from PEP 440 (Appendix B), so PyPI never normalizes a
# differently spelled version into something the tag doesn't match.
CANONICAL_PEP440 = re.compile(
    r"^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*((a|b|rc)(0|[1-9][0-9]*))?"
    r"(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?(\+[a-z0-9]+(\.[a-z0-9]+)*)?$"
)
VERSION_PATTERN = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']\s*$""", re.MULTILINE)


def read_version(path: Path) -> str:
    try:
        text = path.read_text()
    except OSError as err:
        raise SystemExit(f"error: cannot read {path}: {err}") from err
    found = VERSION_PATTERN.search(text)
    if found is None:
        raise SystemExit(f"error: no __version__ assignment found in {path}")
    return found.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="git tag name, e.g. v0.2.0")
    parser.add_argument("--version-file", type=Path, default=DEFAULT_VERSION_FILE)
    args = parser.parse_args(argv)

    tag = args.tag.strip()
    version = tag.removeprefix("v")
    if not version:
        print(f"error: tag {args.tag!r} is empty after removing the v prefix", file=sys.stderr)
        return 1

    expected = read_version(args.version_file)
    if CANONICAL_PEP440.match(expected) is None:
        print(
            f"error: __version__ {expected!r} in {args.version_file} is not a canonical "
            "PEP 440 version (for example 0.2.0 or 0.2.0rc1)",
            file=sys.stderr,
        )
        return 1
    if version != expected:
        print(
            f"error: tag {tag} does not match __version__ {expected} in {args.version_file}",
            file=sys.stderr,
        )
        return 1
    print(f"tag {tag} matches {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
