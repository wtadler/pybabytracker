"""Fail unless a release tag matches the version in the package's _version.py.

Usage: check_version.py TAG [--version-file PATH]

TAG is the git tag name, with or without a leading "v" (v0.2.0 or 0.2.0). The comparison is
exact, so a pre-release tag must spell the version as _version.py does (v0.2.0rc1, not
v0.2.0-rc1). Exits 0 on a match and 1 on anything else.
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_VERSION_FILE = Path(__file__).resolve().parent.parent / "src/pybabytracker/_version.py"
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
