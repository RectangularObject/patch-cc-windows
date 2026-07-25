# patch-cc

Interactive patcher for the Claude Code native binary. What it does and how to
use it: [README.md](README.md).

Before changing anything, read [docs/CONDUCT.md](docs/CONDUCT.md) — how we
build here: the mindset, the guardrails, and who commits (the user does). Then:

- [docs/PLAYBOOK.md](docs/PLAYBOOK.md) — matcher rules, the patch reference,
  how to repair a patch after a Claude update, and the map of the bundle's
  native surfaces (the model registry and its consumers).
- [docs/INTERNALS.md](docs/INTERNALS.md) — the Bun container format and how
  the binary is rewritten in place.

Verify with `uv run patch-cc doctor` (every patch against a clean bundle;
point it at the pristine copies in `~/.local/share/patch-cc/backups/` to sweep
older builds). There is no test suite by design — doctor against real bundles
is the check.
