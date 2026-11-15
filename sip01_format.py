#!/usr/bin/env python3
"""Format sip01-search --json output for the terminal.

Used by sip01-search-tui. Kept as a file rather than an inline heredoc so
the quoting stays readable and the same code serves both the list view and
the URL lookup.

Usage:
  sip01-format list < results.json
  sip01-format choose < results.json
  sip01-format url <N> < results.json
"""

from __future__ import annotations

import json
import sys


def _hits(data: dict) -> list[dict]:
    return data.get("hits", []) or []


def _marker(hit: dict) -> str:
    count = hit.get("indexers", 0)
    return f" [{count}x]" if count > 1 else ""


def cmd_list(data: dict) -> int:
    for i, hit in enumerate(_hits(data), 1):
        print(f"{i:2}. {hit.get('title', '')[:80]}{_marker(hit)}")
        print(f"    {hit.get('url', '')[:100]}")
    return 0


def cmd_choose(data: dict) -> int:
    for i, hit in enumerate(_hits(data), 1):
        print(f"{i}  {hit.get('title', '')[:70]}{_marker(hit)}")
    return 0


def cmd_url(data: dict, index: int) -> int:
    hits = _hits(data)
    if not 1 <= index <= len(hits):
        return 1
    print(hits[index - 1].get("url", ""))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[0]
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON: {exc}", file=sys.stderr)
        return 1
    if command == "list":
        return cmd_list(data)
    if command == "choose":
        return cmd_choose(data)
    if command == "url":
        if len(argv) < 2:
            print("url needs an index", file=sys.stderr)
            return 2
        try:
            index = int(argv[1])
        except ValueError:
            return 2
        return cmd_url(data, index)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
