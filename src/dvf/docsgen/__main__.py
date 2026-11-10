"""``python -m dvf.docsgen [--check] [--root PATH]``: regenerate or check the reference pages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dvf.docsgen import stale_pages, write_pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dvf.docsgen", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(), help="Repository root.")
    parser.add_argument(
        "--check", action="store_true", help="Exit 1 if a page is stale, without writing."
    )
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_pages(args.root)
        for rel in stale:
            sys.stderr.write(f"stale: {rel}\n")
        if stale:
            sys.stderr.write("Regenerate with: uv run nox -s docs_reference\n")
        return 1 if stale else 0
    for path in write_pages(args.root):
        sys.stderr.write(f"wrote {path.as_posix()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
