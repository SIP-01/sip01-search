# sip01-search

Search the decentralized SIP-01 web index from a terminal.

SIP-01 ([spec v1.3](https://github.com/SIP-01/SIP-01)) defines kind 39697
"Web Index Observation" — a signed statement by an indexer that it observed a
URL. Many independent indexers publish those observations to relays, and any
client can read them. There is no central index and no search company.

This tool queries those relays, groups observations by document, and ranks
the results locally.

```
$ sip01-search bitcoin
 1. Bitcoin Feature Matrix | Bitcoin Optech [3x]
    https://bitcoinops.org/en/matrix
    Helping Bitcoin-based businesses integrate scaling technology.
    lang=en  d=widx:e0eeb93a9a37e143672a024c11013d73
 2. Bitcoin Optech Podcast | Listen Free on Castbox. [3x]
    https://castbox.fm/channel/Bitcoin-Optech-Podcast-id5330863
```

The `[3x]` is the number of *independent indexers* that saw the same page.
Agreement between unrelated indexers is the strongest signal the protocol
offers — a signature proves who published, not that the page is good.

## Install

Requires Python 3.10+ and `websockets`.

```bash
git clone https://github.com/SIP-01/sip01-search
cd sip01-search
pip install --user websockets
install -Dm755 sip01_search.py ~/.local/bin/sip01-search
install -Dm755 sip01_format.py ~/.local/lib/sip01-search/sip01_format.py
```

The tool also needs the `sip-01` and `nostr` Agent Skills, which it loads at
runtime for URL normalization, hashing, validation and the relay client:

```bash
hermes skills install SIP-01/sip-01-skill/
hermes skills install NostrDanish/nostr-skill/
```

Protocol logic is not reimplemented here — those skills ship the audited
implementations, and this tool reuses them so there is one place to fix a bug.

## Usage

```bash
sip01-search <query>              # search
sip01-search --url <url>          # find observations of one page
sip01-search --limit 20           # more results
sip01-search --json               # machine-readable output
sip01-search --open 3             # open result 3 in the browser
sip01-search --relays             # show the index relays and their NIPs
sip01-search --selftest           # run the SIP-01 protocol self-test
```

Exit codes: `0` results found, `1` no results, `2` usage error, `3` the index
could not be reached.

## For agents

`--json` returns a stable shape:

```json
{
  "query": "bitcoin",
  "hits": [
    {
      "title": "Bitcoin Feature Matrix | Bitcoin Optech",
      "url": "https://bitcoinops.org/en/matrix",
      "d": "widx:e0eeb93a9a37e143672a024c11013d73",
      "description": "Helping Bitcoin-based businesses integrate scaling technology.",
      "lang": "en",
      "indexers": 3,
      "score": 4.0,
      "pubkey": "…",
      "created_at": 1750000000
    }
  ],
  "hit_count": 10,
  "total_documents": 412,
  "complete": true,
  "relays": [{"relay": "wss://…", "events": 30, "completeness": "complete"}]
}
```

`complete` is `true` only when a relay returned EOSE with a NIP-67 `finish`
hint. When it is `false` the result set may be incomplete — treat it as
"not proven complete".

## How ranking works

The relay's NIP-50 `search` field is treated as a *hint*, never as truth.

Relays that do not implement NIP-50 ignore the field and return recent events
instead of matches. So every candidate is re-scored locally against the
parsed query, and a candidate that does not match locally is **dropped** —
the tool will report "no results" rather than present unrelated pages as
answers. This mirrors the design in
[Dsearch](https://github.com/SIP-01/Dsearch)'s query engine.

Ranking order: local relevance, then independent indexer count, then
recency.

## Omarchy

`sip01-search-tui` is a keyboard-first wrapper: it prompts with `gum`, shows
the results, and opens the one you pick in the browser.

```bash
install -Dm755 sip01-search-tui ~/.local/lib/sip01-search/sip01-search-tui
```

To put it in the Omarchy menu, add a row to
`~/.config/omarchy/extensions/omarchy-menu.jsonc`:

```jsonc
"sip01": {"icon": "󰍉", "label": "SIP-01", "description": "Decentralized web index"},
"sip01.search": {
  "icon": "󰍉",
  "label": "Search the decentralized index",
  "description": "SIP-01 kind 39697 — no central index",
  "action": "omarchy-launch-floating-terminal-with-presentation sip01-search-tui"
}
```

The menu watches that file and hot-reloads, so the row appears without a
shell restart.

## Relays

The default pool is the four UNCAGED SIP-01 relays:

```
wss://sip-relay-3.uncaged-sip.workers.dev
wss://test-sip-relay.sip-01test.workers.dev
wss://sip-relay-2.sip-booster-relay.workers.dev
wss://sip-relay-4.sip-relay-4.workers.dev
```

Override with `SIP01_RELAYS` (comma separated) or `--relay`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite is offline — the relay client is stubbed, so nothing touches the
network.

## License

MIT
