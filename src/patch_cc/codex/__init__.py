"""Codex bridge: use OpenAI ChatGPT/Codex-plan models inside Claude Code.

The bridge is two halves, mirroring patch-cc's split of "what the binary shows"
vs "what actually happens at runtime":

* the **patches** (:mod:`patch_cc.patches.codex`) register the chosen Codex
  models so Claude Code accepts them -- in the subagent ``model`` enum, the
  ``/model`` picker, the known-model validator, the context-window table -- and
  divert *only* those models' requests to a localhost gateway. Every
  Anthropic-model request is left byte-identical, so your Claude plan is
  untouched.
* the **gateway** (:mod:`patch_cc.codex.gateway`) is a stateless localhost
  translator: Claude Code's Anthropic ``/v1/messages`` in, OpenAI Responses out
  (plain HTTPS POST + SSE, no WebSocket), and back. It holds *your* OpenAI token
  and never sees an Anthropic-model request.

Which models to register, and on which port, is not this package's business: it
is ordinary patch configuration, chosen in the menu or on ``apply``'s command
line and baked into the binary's manifest, which is the one place either half
reads it back from. The gateway needs only the port -- a diverted request already
names the OpenAI model to run, so there is no table here to keep in step with the
bundle.

:mod:`patch_cc.codex.translate` is the pure heart of the gateway: dict in, dict
out, no sockets and no auth, so every awkward shape it handles can be reasoned
about on its own.
"""

from __future__ import annotations

from typing import TypeGuard

#: The ChatGPT backend both halves reach: discovery reads its model list, the
#: gateway posts turns to it. One home, so a move cannot leave half the bridge
#: talking to an address the other has left behind.
BACKEND_HOST = "chatgpt.com"

#: A browser User-Agent: the ChatGPT backend refuses unrecognized clients on
#: these routes. Shared for the same reason -- it is exactly the kind of string a
#: backend starts age-gating, and bumping it in one place while the other kept
#: the old one would leave discovery working and every turn refused, or the
#: reverse. Neither half is more authoritative, so it belongs to neither.
BACKEND_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: The localhost port the gateway listens on and the redirect patch targets.
#: Here for the same reason as the two above: it belongs to neither half, and a
#: default that lived in one of them would be the other's remembered copy.
DEFAULT_PORT = 8817

#: Claude Code's reasoning-effort levels, lowest to highest -- the one spelling
#: of the ladder. Three surfaces read it, at three layers, so it lives at the
#: lowest they share: the gateway clamps a Codex request one rung at a time
#: against it (:func:`patch_cc.codex.translate.clamp_effort`), the `max-effort`
#: patch persists its top rung past the binary's whitelist
#: (:mod:`patch_cc.patches.thinking`), and the dry run's synthetic model carries
#: the whole ladder so the registry step bakes every capability. Spelled once,
#: an upstream level moves one place rather than four -- the "must stay in step"
#: the doctor comment used to ask of the reader.
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")


def is_valid_port(port: object) -> TypeGuard[int]:
    """A usable TCP port. One home for the rule; each surface owns its wording.

    ``bool`` is an ``int``, so it is excluded explicitly -- a hand-edited
    ``"port": true`` must not bind port 1. Stated once because four surfaces ask
    (argparse, the menu's field, the cache, and the baked manifest), and a rule
    spelled four times is one accepting what another refuses.
    """
    return isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536
