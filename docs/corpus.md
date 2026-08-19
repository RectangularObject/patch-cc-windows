# The corpus

The measured claims in [PLAYBOOK.md](PLAYBOOK.md) — "every build in the corpus",
a counter that "moved 7 → 9", a matcher that "hit zero on all of them" — are
statements about a set of real Claude binaries. This file is that set, so a
claim is one command from re-verification instead of a number you have to trust.

## What it is

Before the first patch of a version, patch-cc copies the pristine binary to
`~/.local/share/patch-cc/backups/<version>.orig` ([INTERNALS.md](INTERNALS.md#safety)).
The corpus is exactly those copies: not a fixture checked into the repo (each is
~300 MB), but a set that **accretes on its own** as Claude auto-updates and you
re-patch. `doctor <path>` reads one; the sweep below reads all of them.

The binaries are the artifact, not this file — `doctor` recomputes every count
from them, so nothing here can drift from what a matcher actually does. This
file is a fixed point to check the binaries *against*: a hash that no longer
matches means the file changed under you, not that a number moved.

## On disk now

The reproducible set on the machine this was written on — 13 distinct binaries
by content (the SHA-256 is the whole file, `sha256sum <version>.orig`):

| version | size | sha256 |
|---|---|---|
| `2.1.216` | 267 MB | `74deca45220b8080ec75ab099bd5a5980e41a2b5879846a008fb115d436de085` |
| `2.1.217` | 269 MB | `2630fc5dc6db61bc03f86b95daf47766e5ed5b61873f7bb7cfea764c5ac5a9ba` |
| `2.1.218` | 273 MB | `e12071751a9336b8af1012c103358ff04ac18f9aaff4a738cff7ba5cdfaf63f2` |
| `2.1.219` | 275 MB | `22cfd6f5b3061c0391ba84e9cf8c9deaa37783aac18b004d42ec061e98f00691` |
| `2.1.220` | 275 MB | `674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863` |
| `2.1.221` | 289 MB | `60db8e88d42c24b5199c92cfd56ec88370c510c3789c6f364af748354f087ada` |
| `2.1.223` | 291 MB | `98226474f802e3094d6a86c5ade8883c16206d0fcb5c400b7401c800063e99d7` |
| `2.1.226` | 298 MB | `4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555` |
| `2.1.227` | 304 MB | `6832dc3f1797b890b71116e5f2dbbf9a83fd3d0498c235b4b0f9cd0e6e499ad6` |
| `2.1.228` | 309 MB | `d535985e6941a3eb00179ccd7f52ceb0c6623a0305a518ebc4e6514f84a94c99` |
| `2.1.233` | 325 MB | `55d281096f57d411ebbdd94dbf5e9ff3accb7c05713e37348c2c11d4b83bf9d9` |
| `2.1.234` | 328 MB | `3473601ea695d5bf769c5b202844d4cb4fbf723ae995450fcb6973204775c84a` |
| `2.1.235` | 331 MB | `bfcf0ae2dbf94b2b6a106074aabf3938b9a10889c3b678e4cb5a00c03274d5d5` |

The backup directory holds a few more files than rows here, and that is not a
discrepancy: a pre-0.2.0 backup doubled the version into its name
(`2.1.216.2.1.216.orig`), a binary installed under a non-version name is
saved as `claude.unknown-<hash>.orig`, and a binary patched under a
non-canonical filename keeps that filename (`2.1.235` entered the corpus as a
downloaded `claude-2.1.235`, so its backup is `claude-2.1.235.orig`). The two
extra 2.1.216-era files hash-match the `2.1.216` row above — same bytes, a
different filename — which is why the count of *files* (15) and the count of
*distinct binaries* (13) differ.

## Rebuild or extend it

There is nothing to download and no fixture to restore: the corpus is whatever
pristine backups you have accumulated, and it grows every time patch-cc touches
a new build. To widen it, run patch-cc across Claude updates — each first patch
of a version leaves its `.orig`. To read the JS a given binary carries without
patching anything:

```bash
patch-cc extract ~/.local/share/patch-cc/backups/2.1.233.orig > 2.1.233.js
```

## The sweep

The whole point of the set — every patch against every build, the exit codes and
the `diff` between two revisions ([PLAYBOOK.md](PLAYBOOK.md#repairing-a-broken-patch)):

```bash
sweep() { for b in ~/.local/share/patch-cc/backups/*.orig; do
  echo "== $(basename "$b")"; patch-cc doctor "$b" || true
done; }
sweep
```

`doctor` is read-only — it runs the matchers and parses their output, and never
writes a binary — so the sweep is safe to run against every backup at any time.

## Provenance of the wider span

Some measurements in the playbook were taken during "the move to the tree"
across a wider set than is durably on disk now — the range `2.1.210` → `2.1.233`
(2.1.230 was never published), whose earliest builds predate this backup set.
Those numbers were real when taken; the set above is what reproduces today, and
a claim that names a build not listed here is a historical one this file does
not stand behind byte-for-byte.
