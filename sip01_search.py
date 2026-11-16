#!/usr/bin/env python3
"""sip01-search — search the decentralized SIP-01 web index from a terminal.

Reads kind 39697 Web Index Observations from the SIP-01 index relays and
ranks them locally. Designed to be the search half of an Omarchy desktop:
a human gets a keyboard-first result list, an agent gets `--json`.

Protocol logic is NOT reimplemented here. URL normalization, the `d`/`x`
hashing and the event validator come from the `sip-01` skill's `sip01.py`,
and the relay client comes from the `nostr` skill's `nostr_query.py`. Both
are loaded at runtime if present; this file only adds querying, ranking and
presentation.

Usage:
  sip01-search <query> [--limit N] [--relay URL] [--json] [--open N]
  sip01-search --url <exact-url>            # find observations of one page
  sip01-search --relays                    # list the index relays and their NIP-11 caps
  sip01-search --selftest                  # protocol self-test

Exit codes:
  0  results found (or the command succeeded)
  1  no results
  2  usage error
  3  the index could not be reached
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path

# --------------------------------------------------------------------------
# Index relays — the SIP-01 pool. Override with SIP01_RELAYS (comma separated).
# --------------------------------------------------------------------------

DEFAULT_RELAYS = [
    "wss://sip-relay-3.uncaged-sip.workers.dev",
    "wss://test-sip-relay.sip-01test.workers.dev",
    "wss://sip-relay-2.sip-booster-relay.workers.dev",
    "wss://sip-relay-4.sip-relay-4.workers.dev",
]

WEB_INDEX_KIND = 39697


def _relays() -> list[str]:
    env = os.environ.get("SIP01_RELAYS", "").strip()
    if not env:
        return list(DEFAULT_RELAYS)
    return [r.strip() for r in env.split(",") if r.strip()]


# --------------------------------------------------------------------------
# Skill script loading — one implementation of the protocol, not two.
# --------------------------------------------------------------------------

SKILL_ROOTS = [
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "skills",
    Path.home() / ".hermes" / "skills",
]

_HINT = (
    "install it with: hermes skills install SIP-01/sip-01-skill/ "
    "and hermes skills install NostrDanish/nostr-skill/"
)


def _find_skill_script(skill_dirs: list[str], filename: str) -> Path | None:
    for root in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for skill_dir in skill_dirs:
            candidate = root / skill_dir / "scripts" / filename
            if candidate.is_file():
                return candidate
        for category in root.iterdir():
            if not category.is_dir():
                continue
            for skill_dir in skill_dirs:
                candidate = category / skill_dir / "scripts" / filename
                if candidate.is_file():
                    return candidate
    return None


def _load(name: str, filename: str, skill_dirs: list[str]):
    path = _find_skill_script(skill_dirs, filename)
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sip01():
    return _load("sip01_search_sip01", "sip01.py", ["sip-01-skill", "sip-01"])


def _relay_client():
    return _load("sip01_search_query", "nostr_query.py", ["nostr-skill", "nostr"])


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------


async def _query(relay: str, filter_obj: dict, timeout: float) -> dict:
    client = _relay_client()
    if client is None:
        raise RuntimeError(f"the nostr skill's relay client is missing — {_HINT}")
    return await client.query(relay, json.dumps(filter_obj), timeout=timeout)


def _parse_observation(event: dict) -> dict | None:
    """Turn a kind 39697 event into a result row, or None if it is malformed."""
    if event.get("kind") != WEB_INDEX_KIND:
        return None
    tags: dict[str, str] = {}
    for tag in event.get("tags", []):
        if isinstance(tag, list) and len(tag) > 1 and isinstance(tag[0], str):
            tags.setdefault(tag[0], tag[1])
    url = tags.get("u")
    doc_id = tags.get("d")
    if not url or not doc_id:
        return None
    content: dict = {}
    try:
        parsed = json.loads(event.get("content") or "{}")
        if isinstance(parsed, dict):
            content = parsed
    except json.JSONDecodeError:
        pass
    title = _clean_text(content.get("title") or "")
    if not title:
        return None
    return {
        "id": event.get("id"),
        "pubkey": event.get("pubkey"),
        "url": url,
        "d": doc_id,
        "x": tags.get("x"),
        "title": title,
        "description": _clean_text(content.get("description") or ""),
        "lang": tags.get("l"),
        "topics": [t[1] for t in event.get("tags", []) if isinstance(t, list) and len(t) > 1 and t[0] == "t"],
        "created_at": event.get("created_at"),
        "published": tags.get("published"),
    }


_TAG_RE = None


def _clean_text(value: str) -> str:
    """Strip HTML tags and collapse whitespace.

    Indexers store the raw page excerpt, so descriptions routinely carry
    markup. It is noise in a terminal and noise in an agent's context.
    """
    global _TAG_RE
    if _TAG_RE is None:
        import re

        _TAG_RE = re.compile(r"<[^>]+>")
    text = _TAG_RE.sub(" ", value)
    return " ".join(text.split())


def _score(result: dict, terms: list[str]) -> float:
    """Local relevance. The relay's NIP-50 ranking is a hint; this is ours.

    Mirrors the principle in Dsearch's query engine: never rely on a relay's
    operator semantics — re-evaluate every candidate locally.
    """
    if not terms:
        return 0.0
    haystack = " ".join(
        filter(None, [result.get("title", ""), result.get("description", ""), " ".join(result.get("topics") or [])])
    ).lower()
    url = (result.get("url") or "").lower()
    score = 0.0
    for term in terms:
        if term in haystack:
            score += 2.0
        if term in url:
            score += 1.0
    # Title hits matter more than body hits.
    title = (result.get("title") or "").lower()
    for term in terms:
        if term in title:
            score += 1.0
    return score


def _terms(query: str) -> list[str]:
    """Split a query into terms, keeping quoted phrases together."""
    terms: list[str] = []
    current: list[str] = []
    in_quotes = False
    for chunk in query.split():
        if chunk.startswith('"') and chunk.endswith('"') and len(chunk) > 1:
            terms.append(chunk[1:-1].lower())
            continue
        if chunk.startswith('"'):
            in_quotes = True
            current.append(chunk[1:])
            continue
        if chunk.endswith('"') and in_quotes:
            current.append(chunk[:-1])
            terms.append(" ".join(current).lower())
            current = []
            in_quotes = False
            continue
        if in_quotes:
            current.append(chunk)
            continue
        terms.append(chunk.lower())
    if current:
        terms.append(" ".join(current).lower())
    return [t for t in terms if t]


async def search(
    query: str = "",
    *,
    url: str | None = None,
    relays: list[str] | None = None,
    limit: int = 10,
    timeout: float = 15.0,
    max_pages: int = 5,
) -> dict:
    """Search the SIP-01 index. Returns a result dict with hits and diagnostics."""
    relays = relays or _relays()
    terms = _terms(query)
    sip = _sip01()
    client = _relay_client()
    if client is None:
        raise RuntimeError(f"the nostr skill's relay client is missing — {_HINT}")

    # A NIP-01 filter: kind + tag filters. Free text goes in the NIP-50
    # `search` field, which SIP-01-aware relays answer with relevance-ranked
    # matches. Relays that don't support it ignore the field (SHOULD per
    # NIP-50) and we score locally anyway.
    filter_obj: dict = {"kinds": [WEB_INDEX_KIND], "limit": max(limit * 3, 30)}
    normalized_url = None
    if url:
        normalized_url = None
        if sip is not None:
            normalized_url = sip.normalize_index_url(url)
        filter_obj["#u"] = [normalized_url or url]
    elif terms:
        filter_obj["search"] = query

    hits: dict[str, dict] = {}
    relay_status: list[dict] = []
    complete = False

    for relay in relays:
        try:
            result = await _query(relay, filter_obj, timeout)
        except Exception as exc:
            relay_status.append({"relay": relay, "error": f"{type(exc).__name__}: {exc}"})
            continue
        relay_status.append({
            "relay": relay,
            "events": len(result.get("events", [])),
            "completeness": result.get("completeness"),
        })
        if result.get("completeness") == "complete":
            complete = True
        for event in result.get("events", []):
            parsed = _parse_observation(event)
            if parsed is None:
                continue
            # Group by document id (spec §2.1): independent indexers observing
            # the same page are agreement, and the distinct-author count is the
            # core ranking signal.
            key = parsed["d"]
            existing = hits.get(key)
            if existing is None:
                parsed["indexers"] = {parsed["pubkey"]} if parsed["pubkey"] else set()
                hits[key] = parsed
            else:
                if parsed["pubkey"]:
                    existing["indexers"].add(parsed["pubkey"])
                # Keep the richer metadata.
                if len(parsed.get("description") or "") > len(existing.get("description") or ""):
                    existing["description"] = parsed["description"]

    rows = []
    for hit in hits.values():
        indexers = hit.pop("indexers", set())
        hit["indexers"] = len(indexers)
        hit["score"] = _score(hit, terms)
        rows.append(hit)

    # Rank: relevance first, then independent indexers, then recency.
    rows.sort(key=lambda r: (r["score"], r["indexers"], r.get("created_at") or 0), reverse=True)

    if url:
        # The relay returns the normalized URL, so compare against the
        # normalized form of what the user asked for, not the raw string.
        target = normalized_url or url
        rows = [r for r in rows if r["url"] == target]
    elif terms:
        # Strict: a candidate that does not match the query locally is
        # dropped, even when the relay returned it. A relay that ignores the
        # NIP-50 `search` field hands back recent events instead of matches,
        # and re-evaluating every candidate locally is what stops the tool
        # from presenting those as answers. Zero local matches means no
        # results — never "here are some unrelated pages".
        rows = [r for r in rows if r["score"] > 0]

    return {
        "query": query,
        "url": url,
        "hits": rows[:limit],
        "hit_count": len(rows[:limit]),
        "total_documents": len(rows),
        "complete": complete,
        "relays": relay_status,
        "note": (
            "complete=true means at least one relay returned EOSE with a NIP-67 finish hint. "
            "'unknown' means the result set may be incomplete."
        ),
    }


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------


def _human(result: dict) -> str:
    hits = result.get("hits", [])
    if not hits:
        return (
            "No results.\n"
            f"  relays tried: {', '.join(r['relay'] for r in result.get('relays', [])) or 'none'}\n"
            "  The SIP-01 index is community-run and may not have this page yet."
        )
    lines = []
    for i, hit in enumerate(hits, 1):
        marker = f" [{hit['indexers']}x]" if hit.get("indexers", 0) > 1 else ""
        lines.append(f"{i:2}. {hit['title']}{marker}")
        lines.append(f"    {hit['url']}")
        if hit.get("description"):
            lines.append(f"    {hit['description'][:160]}")
        if hit.get("lang"):
            lines.append(f"    lang={hit['lang']}  d={hit['d']}")
        else:
            lines.append(f"    d={hit['d']}")
    return "\n".join(lines)


def _open_url(url: str) -> int:
    opener = None
    if os.environ.get("BROWSER"):
        opener = os.environ["BROWSER"]
    for candidate in (opener, "xdg-open", "gio", "sensible-browser"):
        if not candidate:
            continue
        try:
            if candidate == "xdg-open":
                import subprocess

                subprocess.run(["xdg-open", url], check=True, timeout=15)
            elif candidate == "gio":
                import subprocess

                subprocess.run(["gio", "open", url], check=True, timeout=15)
            else:
                import subprocess

                subprocess.run([candidate, url], check=True, timeout=15)
            return 0
        except Exception:
            continue
    print(f"could not open a browser; open manually: {url}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sip01-search",
        description="Search the decentralized SIP-01 web index.",
    )
    parser.add_argument("query", nargs="*", default=[], help="search terms")
    parser.add_argument("--url", help="find observations of this exact URL")
    parser.add_argument("--limit", type=int, default=10, help="max results (default 10)")
    parser.add_argument("--relay", action="append", help="use this relay (repeatable)")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds per relay (default 15)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--open", type=int, metavar="N", help="open result N in the browser")
    parser.add_argument("--relays", action="store_true", help="show the index relays and their capabilities")
    parser.add_argument("--selftest", action="store_true", help="run the SIP-01 protocol self-test")
    parser.add_argument("--dump", action="store_true",
                        help="fetch the whole index as JSON (no query), for the local search engine")
    args = parser.parse_args(argv)

    if args.dump:
        # The native search app keeps its own corpus and ranks locally, so it
        # needs every observation, not just the matches for one query.
        try:
            result = asyncio.run(search("", limit=5000, timeout=args.timeout))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"dump failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("hits") else 1

    if args.selftest:
        sip = _sip01()
        if sip is None:
            print(f"the sip-01 skill's toolkit is missing — {_HINT}", file=sys.stderr)
            return 3
        return sip.selftest() or 0

    if args.relays:
        client = _relay_client()
        if client is None:
            print(f"the nostr skill's relay client is missing — {_HINT}", file=sys.stderr)
            return 3
        rows = []
        for relay in (args.relay or _relays()):
            try:
                info = client.info(relay, timeout=args.timeout)
                rows.append({
                    "relay": relay,
                    "ok": True,
                    "nips": info.get("supported_nips", []),
                    "sip01": bool((info.get("uncaged_index") or {}).get("sip01")),
                })
            except Exception as exc:
                rows.append({"relay": relay, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for row in rows:
                if row.get("ok"):
                    print(f"OK    {row['relay']}  sip01={row['sip01']}  nips={row['nips']}")
                else:
                    print(f"FAIL  {row['relay']}  {row.get('error')}")
        return 0 if any(r.get("ok") for r in rows) else 3

    query = " ".join(args.query).strip()
    if not query and not args.url:
        parser.print_help()
        return 2

    try:
        result = asyncio.run(
            search(
                query,
                url=args.url,
                relays=args.relay or None,
                limit=args.limit,
                timeout=args.timeout,
            )
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_human(result))

    if args.open:
        index = args.open - 1
        hits = result.get("hits", [])
        if index < 0 or index >= len(hits):
            print(f"no result #{args.open} (have {len(hits)})", file=sys.stderr)
            return 1
        return _open_url(hits[index]["url"])

    return 0 if result.get("hits") else 1


if __name__ == "__main__":
    raise SystemExit(main())
