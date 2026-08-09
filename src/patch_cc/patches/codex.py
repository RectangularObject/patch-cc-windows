"""Codex models: register the chosen OpenAI models, and route them to the gateway.

Two things happen here, and only when at least one model has been chosen:

* **Registration** makes Claude Code *accept and show* each model -- in the
  subagent ``model`` enum, the known-model validator, the ``/model`` picker, the
  model resolver, the context-window table, and the binary's own **model
  registry** (which names the model in the status line and banners, declares
  its effort capabilities, and makes it advisor-eligible). This is the same
  surface clodex's binary patcher touches, re-anchored on 2.1.217's real shapes
  (its own context-window anchor had already drifted off this build). Alongside each
  id, a short **family shortcut** (``sol`` -> the newest chosen
  ``gpt-<ver>-sol``) is registered in the validator, *both* resolvers, and the
  picker, mirroring the binary's own ``opus`` -> ``claude-opus-4-8``: it resolves
  to the id before a request is built, so routing needs no separate knowledge of
  it (see :func:`patch_cc.codex.models.family_aliases`). The binary keeps two
  resolvers -- a general one every request uses and an override one for managed
  ``availableModels`` -- and a shortcut needs an arm in both; it is gated on the
  general one, so a drift there costs the shortcuts, never the bridge.

* **Routing** is the piece clodex does at the network layer with a TLS
  man-in-the-middle. patch-cc does it *in the bundle*: one injection in the
  Anthropic SDK's ``buildRequest`` swaps the request origin to the localhost
  gateway **only** when the body's model is one of the chosen ids. Every
  Anthropic-model request is left byte-identical -- same host, same plan token
  -- so nothing about the normal path changes, and if this step ever drifts,
  Claude models keep working and ``doctor`` flags the miss.

Everything this patch bakes comes from its ``Options``, exactly like the brand
and the subagent pins: the ids to register, and the port to route them to.
Nothing here reads a store of its own, which is what makes the binary's manifest
the one record of what a patched Claude Code answers to.

Required steps (``expect=True``): without any one, the feature is dead --
``enum`` (subagent use), ``validator`` and ``resolver`` (the model is accepted
and resolves -- an id to itself, a shortcut to the newest id in its family),
``redirect`` (requests actually reach the gateway). ``general-resolver`` is
required too, but only exists when a shortcut is being registered (it is gated
in, above). ``picker`` (the ``/model`` list), ``context`` (the real window) and
``registry`` (the model table behind the status line, effort gating and
`/advisor`) are refinements: absent, you can still type ``/model <id>`` and get
a 200k default under the model's raw id.
"""

from __future__ import annotations

import json
import re

from ..codex.models import family_aliases, family_of
from .agents import MODEL_ENUM
from .base import (
    GROUP_MODELS,
    IDENT,
    Options,
    Outcome,
    Patch,
    compile_js,
    js_string,
    splice,
)

# A JS double-quoted string literal (handles escapes), for scanning array bodies.
_STR = r'"(?:[^"\\]|\\.)*"'

# --- anchors, each locked against the installed bundle (see docs/PLAYBOOK.md) ---

# The known-model master list: ["sonnet",...,"opusplan",...]. Anchored on the
# built-in prefix and "opusplan" so new built-ins in the middle don't shift it.
_VALIDATOR = compile_js(
    r'\["sonnet","opus","haiku","fable"(?:,"[^"]+")*,"opusplan"(?:,"[^"]+")*\]'
)
# The binary has TWO model resolvers; a shortcut needs an arm in both.
# `_RESOLVER` (minified `J9n`) is the override/availability resolver, reached
# only when managed `availableModels` are active -- its `case"best"` is a block.
# New cases go right after it.
_RESOLVER = compile_js(r'case"best":\{[^{}]*\}')
# `_GENERAL_RESOLVER` (minified `Ei`) is the resolver *every ordinary request*
# uses -- the one that turns `opus` into `claude-opus-4-8`. Unlike `J9n` it passes
# an unknown-but-valid name straight through (`return e`), so a family shortcut
# resolves only if it gets an explicit arm here. Shape-anchored: `Ei`'s `case"best"`
# ends `return X();default:}` (vs `J9n`'s `case"best":{...}`), and the minified `X`
# is matched, never baked in. Group 1 is the arm; new cases splice at its end.
_GENERAL_RESOLVER = compile_js(r'(case"best":return [A-Za-z_$][\w$]*\(\);)default:\}')
# The exact shape of an arm we generate, for a *bounded* "already added?" check:
# matched contiguously from the insertion point, it spans only our own prior arms
# and stops at the first foreign token -- so it can never run into an unrelated
# later switch the way a `find("default:")` scan could on a default-less build.
_RESOLVER_ARMS = compile_js(rf"(?:case{_STR}:return {_STR};)*")
# The /model picker choke-point: `?[n,r]:[r];for(let i of o)push(e,i,t);`, where
# `e` (group 6) is the options array every model is pushed onto.
_PICKER = compile_js(
    rf"\?\[({IDENT}),({IDENT})\]:\[\2\];for\(let ({IDENT}) of ({IDENT})\)"
    rf"({IDENT})\(({IDENT}),\3,({IDENT})\);"
)
# The Anthropic SDK's buildRequest: options var (group 3) and the built URL
# (group 8) are both in scope, and `r.body.model` is still the parsed object
# here (buildBody runs later). The middle is brace-free, matched as one span.
_REDIRECT = compile_js(
    rf"buildRequest\(({IDENT}),\{{retryCount:({IDENT})=0\}}=\{{\}}\)\{{"
    rf"let ({IDENT})=\{{\.\.\.\1\}},"
    rf"\{{method:({IDENT}),path:({IDENT}),query:({IDENT}),defaultBaseURL:({IDENT})\}}=\3;"
    rf"[^{{}}]*?let ({IDENT})=this\.buildURL\(\5,\6,\7\);"
)
# The context-window resolver: a brace-free `(e,t)` body that reads the max-tokens
# env override. `e` (group 2) is the model string; group 4 is the body.
_CONTEXT = compile_js(
    rf"function ({IDENT})\(({IDENT}),({IDENT})\)\{{"
    rf"([^{{}}]*CLAUDE_CODE_MAX_CONTEXT_TOKENS[^{{}}]*)\}}"
)
# The embedded model registry -- the binary's own single table of what a model
# is called, what it can do, and whether it may advise. Its `models:[...]`
# array closes immediately before the `aliases` table, whose first entry
# defaults to a Claude id; that boundary occurs once in the bundle and cannot
# belong to anything else. Group 1 opens at the `]` new entries splice before.
_REGISTRY = compile_js(r'\}(\],aliases:\{[a-z][\w]*:\{default:"claude-)')

# The JS regex literal that strips a URL's scheme+host, leaving path+query. A raw
# string keeps the backslashes as the JS engine needs them.
_ORIGIN = r"/^https?:\/\/[^\/]+/"


def _extend_array_body(body: str, names: list[str]) -> tuple[str, bool]:
    """Append names not already present in a comma-joined quoted-string body."""
    present = set(re.findall(_STR, body))
    add = [n for n in names if js_string(n) not in present]
    if not add:
        return body, False
    return body + "," + ",".join(js_string(n) for n in add), True


def _register_enum(content: str, names: list[str], step: Outcome) -> str:
    """Extend the Task tool's ``model`` enum, the anchor `subagent-models` reads."""
    match = MODEL_ENUM.search(content)
    if not match:
        return content
    step.candidates += 1
    body, changed = _extend_array_body(match.group(1), names)
    if changed:
        content = splice(content, match.start(1), match.end(1), body)
        step.applied += 1
    return content


def _register_validator(content: str, names: list[str], step: Outcome) -> str:
    match = _VALIDATOR.search(content)
    if not match:
        return content
    step.candidates += 1
    body, changed = _extend_array_body(match.group(0)[1:-1], names)
    if changed:
        content = splice(content, match.start(), match.end(), "[" + body + "]")
        step.applied += 1
    return content


def _register_resolver(content: str, resolution: dict[str, str], step: Outcome) -> str:
    """Add arms to the override resolver (`J9n`) -- the managed-settings path.

    An id resolves to *itself* (its identity everywhere else); a family shortcut
    resolves to the newest id in its family. This resolver only fires when managed
    ``availableModels`` are active; the ordinary path goes through
    :func:`_register_general_resolver`, so both carry the same arms.
    """
    match = _RESOLVER.search(content)
    if not match:
        return content
    step.candidates += 1
    # Scope the "already present?" check to the arms already generated at THIS
    # insertion point, never the whole 21MB bundle: a short word like `auto`
    # legitimately occurs as `case"auto":return` in unrelated code, and a global
    # check would skip its arm here while the full-id arms still marked the step
    # applied -- shipping a short alias that never resolves on the managed path. The
    # contiguous-arm match is bounded to our own output (empty on a pristine arm, so
    # every arm is added), so it can't run into a later switch on a default-less build.
    arms = _RESOLVER_ARMS.match(content, match.end())
    region = arms.group() if arms else ""
    add = {
        name: target
        for name, target in resolution.items()
        if f"case{js_string(name)}:return" not in region
    }
    if add:
        cases = "".join(
            f"case{js_string(name)}:return {js_string(target)};"
            for name, target in add.items()
        )
        content = splice(content, match.end(), match.end(), cases)
        step.applied += 1
    return content


def _register_general_resolver(
    content: str, shorts: dict[str, str], step: Outcome
) -> str:
    """Add shortcut arms to the general resolver (`Ei`) -- the ordinary path.

    ``case"sol":return "gpt-5.6-sol";`` -- the short handle resolves to the newest
    id in its family right here, before the request is built, exactly as ``opus``
    does. Only shortcuts need this: `Ei` already passes an id through unchanged.
    """
    match = _GENERAL_RESOLVER.search(content)
    if not match:
        return content
    step.candidates += 1
    # The anchor requires `default:}` immediately after `case"best"`, so it only
    # matches the *pristine* arm; once arms are spliced in, re-application no longer
    # finds it -- that adjacency is the idempotency guard. A global "already
    # present?" check would instead false-match the identical arms the override
    # resolver was just given, and wrongly skip this one.
    cases = "".join(
        f"case{js_string(name)}:return {js_string(target)};"
        for name, target in shorts.items()
    )
    content = splice(content, match.end(1), match.end(1), cases)
    step.applied += 1
    return content


def _redirect(content: str, options: Options, step: Outcome) -> str:
    match = _REDIRECT.search(content)
    if not match:
        return content
    step.candidates += 1
    opts, url = match.group(3), match.group(8)
    routed = "[" + ",".join(js_string(m.id) for m in options.codex_models) + "]"
    # The URL is all this changes, and a diverted request still carries the
    # Anthropic credential: measured on the wire, `Authorization: Bearer
    # sk-ant-oat01-...` and the whole prompt arrive at whatever holds the gateway
    # port. Our gateway never reads it; a process that squatted the port would.
    #
    # Stripping it from here does not work, and the obvious attempt is worth
    # recording so it is not retried blind. `options.headers` is the last source
    # `buildHeaders` merges and its merge treats `null` as delete, so setting
    # either a null or an inert value there should win. Neither reaches the wire:
    # with the block proven to run (a marker spliced into the path arrived), a
    # probe header set on the options object *and* on the local copy was absent
    # from the request both times. Something between `buildRequest` and `fetch`
    # discards `options.headers` on this build. A real fix therefore needs its
    # own anchor further down -- `prepareRequest`, which receives the final
    # `Headers` object and the URL -- which is a new required step and new
    # matcher surface, not a line added here.
    inject = (
        f'if({opts}.body&&typeof {opts}.body=="object"&&'
        f"{routed}.includes({opts}.body.model))"
        f"{url}={url}.replace("
        + _ORIGIN
        + f',"http://127.0.0.1:{options.codex_port}");'
    )
    if inject not in content:
        content = splice(content, match.end(), match.end(), inject)
        step.applied += 1
    return content


def _display_name(handle: str) -> str:
    """A handle spelled as a name: ``gpt-5.7-sol`` -> ``GPT 5.7 Sol``, ``sol`` -> ``Sol``.

    Derived from the handle itself (or from the name discovery reported, which
    falls back to it), never from a catalogue of ours -- so a model released
    tomorrow reads correctly without patch-cc having heard of it. Any word that
    already carries case or digits is left exactly as it came, which makes this a
    no-op on a backend that starts sending real titles.
    """
    return " ".join(
        # `GPT` is OpenAI's own capitalization of the prefix. Every other
        # all-lowercase word title-cases; `5.7`, having no cased characters, is
        # not lowercase and falls through untouched.
        "GPT" if word == "gpt" else word.capitalize() if word.islower() else word
        for word in handle.replace("-", " ").split()
    )


def _describe(model) -> str:
    """One Codex model as the ``/model`` picker's second line."""
    window = f" with {model.context // 1000}k context" if model.context else ""
    return f"{_display_name(model.label)}{window} from codex"


def _register_picker(
    content: str, models, shorts: dict[str, str], step: Outcome
) -> str:
    match = _PICKER.search(content)
    if not match:
        return content
    step.candidates += 1
    array = match.group(6)
    by_id = {m.id: m for m in models}
    covered = set(shorts.values())
    # (value you pick, the model it stands for), shortcuts first. A shortcut *is*
    # its family's newest model, so listing that model's id underneath would offer
    # the same choice twice; only a model no shortcut speaks for gets its own row
    # -- an older version kept alongside a newer one, or a slug with no family at
    # all (`gpt-5.5`). Nothing is hidden by that: the description names the
    # concrete model, and the id stays typeable either way.
    picks = [(short, by_id[target]) for short, target in shorts.items()]
    picks += [(m.id, m) for m in models if m.id not in covered]
    # Shaped like the binary's own rows: the label is the handle *spelled as a
    # name* -- `Sol` for a shortcut, `GPT 5.6 Sol` for an id -- and the description
    # reads `<model> with <n> context`, as the native rows do. One rule for both
    # kinds, because a row is a row: labelling id rows with the raw slug made them
    # the only entries in the list wearing a different sort of name than every
    # neighbour, `gpt-5.6-sol` sitting under `Opus`. It ends in "from codex"
    # because this is the only place in the picker where a model leaves Anthropic,
    # and nothing else in the list would say so.
    entries = ",".join(
        f"{{value:{js_string(value)},label:{js_string(_display_name(value))},"
        f"description:{js_string(_describe(model))}}}"
        for value, model in picks
    )
    # Every row is emitted with no build-time "already present?" filter, and dedup
    # is left to the runtime `.some()` guard below. A per-value check would false-
    # skip (a short value like `auto` occurs as `value:"auto"` in the theme picker,
    # dropping its row while the id rows still mark the step applied); a whole-block
    # check can't be a stable key either, since the block depends on `shorts`, which
    # the drift gate zeroes on a second pass. Neither matters in practice: the
    # patcher always runs from a pristine source (never its own output), and even a
    # hypothetical double-append is dead bytes behind `.some()`, not a behaviour
    # change.
    inject = (
        f"[{entries}].forEach(function(__cc_row){{"
        f"if(!{array}.some(function(__cc_seen){{return __cc_seen.value===__cc_row.value}}))"
        f"{array}.push(__cc_row)}});"
    )
    content = splice(content, match.end(), match.end(), inject)
    step.applied += 1
    return content


def _register_context(content: str, models, outcome: Outcome) -> str:
    """Bake the real context window for each chosen model that reports one.

    Takes the parent outcome, not a step, so the step exists only when there is a
    window to bake. With none, no rewrite is owed and a step would have to report
    that as either an absent shape or a missed rewrite -- both untrue, and both
    reading as a build problem rather than as nothing to do (docs/CONDUCT.md).
    """
    windows = {m.id: m.context for m in models if m.context > 0}
    if not windows:
        outcome.note("no chosen model reports a context window; 200k default stands")
        return content
    match = _CONTEXT.search(content)
    if not match:
        return content
    step = outcome.step("context")
    step.candidates += 1
    model_var = match.group(2)
    table = json.dumps(windows, separators=(",", ":"))
    # At the top of the resolver, so it answers before the fallbacks below it --
    # the 200k default this function ends on, and its own
    # CLAUDE_CODE_MAX_CONTEXT_TOKENS read, which applies to exactly the non-Claude
    # models we are registering. It does not (and should not) outrank the caller's
    # own env override or long-context clamp, which are decided before this
    # function is reached. Only our ids are answered; an unknown key falls
    # through untouched.
    inject = (
        f'var __cc_window=({table})[String({model_var}||"").trim().toLowerCase()];'
        f"if(__cc_window!==void 0)return __cc_window;"
    )
    if inject not in content:
        content = splice(content, match.start(4), match.start(4), inject)
        step.applied += 1
    return content


#: Advisor rank claimed for every registered Codex model: Opus 5's own rank in
#: the registry (Fable is 5, Sonnet 5 is 3, Haiku 1). Rank *presence* is what
#: `/advisor <model>` checks for eligibility; the number only orders
#: "at least as capable" pairings. The chosen ids are frontier coding models,
#: so they sit with Opus -- able to advise anything below Fable, claiming
#: nothing above it. Per-model precision here would be a catalogue of ours:
#: the plan reports no rank, so one honest number beats four invented ones.
_ADVISOR_RANK = 4


def _effort_capabilities(model) -> list[str]:
    """The registry capability strings the plan's effort list vouches for.

    Positive declarations only. The binary reads an *absent* capability as "ask
    the provider fallback" (permissive on the first-party API), never as "no" --
    so leaving one out keeps today's behaviour, and a wrong *yes* is the only
    mistake this could bake. Which is why the list is built from the plan's own
    ``supported_reasoning_levels`` and stops at the effort trio: the thinking
    and adaptive flags change how the binary builds requests, and those belong
    to models Anthropic ships.
    """
    if not model.efforts:
        return []
    capabilities = ["effort"]
    if "xhigh" in model.efforts:
        capabilities.append("xhigh_effort")
    if "max" in model.efforts:
        capabilities.append("max_effort")
    return capabilities


def _registry_entry(model) -> str:
    """One schema-valid registry record for ``model``, as JS source.

    The registry's zod parse is all-or-nothing with an **empty** fallback: one
    malformed entry and every model -- Claude's included -- loses its metadata
    (names, capabilities, aliases). So this emits only fields the schema
    declares, shaped exactly as it demands: the required four (``id``,
    ``family``, ``display_name``, ``provider_ids.first_party``), and beyond
    them only values with something true to record. JSON is the emitter for
    the reason :func:`patch_cc.patches.base.js_string` gives: valid JSON is
    valid JS, escaping included.

    ``pricing`` and ``max_output_tokens`` are left out on purpose -- their
    readers all guard for absence, a subscription has no per-token price to
    state, and the output cap never reaches the wire (the backend refuses
    explicit caps; the translator drops them). ``default_effort`` is left out
    too, and that one is a decision, not a shrug: the binary resolves a missing
    default as ``high`` (``?.default_effort??"high"``) -- the very default its
    own flagships declare -- so omission makes ``/effort auto`` and an untouched
    session mean on these models exactly what they mean on Opus. Baking the
    plan's default made auto on sol mean *low*: Codex-the-product's UX imported
    into a harness that has its own convention. A model that some day lacks
    even ``high`` still cannot dead-turn -- the gateway clamps the refusal.
    """
    entry: dict = {
        "id": model.id,
        "family": family_of(model.id) or model.id,
        "display_name": _display_name(model.label),
        "provider_ids": {"first_party": model.id},
        "advisor_rank": _ADVISOR_RANK,
    }
    if capabilities := _effort_capabilities(model):
        entry["capabilities"] = capabilities
    return json.dumps(entry, separators=(",", ":"))


def _register_registry(content: str, models, step: Outcome) -> str:
    """Add each chosen model to the binary's own model table.

    This is what makes the models first-class rather than merely accepted:
    every consumer of the registry -- the status line's display name, the
    effort capability checks, `/advisor` eligibility, and any surface neither
    we nor upstream have enumerated -- handles them by default from here on.
    The picker and the context resolver are *not* registry-driven (rows are
    hand-built upstream; the window resolver ends on a flat 200k), which is why
    those two steps still exist alongside this one.
    """
    match = _REGISTRY.search(content)
    if not match:
        return content
    step.candidates += 1
    inject = "," + ",".join(_registry_entry(model) for model in models)
    if inject not in content:
        content = splice(content, match.start(1), match.start(1), inject)
        step.applied += 1
    return content


def _codex_models(content: str, options: Options, outcome: Outcome) -> str:
    """Register and route the chosen Codex models.

    A no-op with no models configured -- like ``subagent-models``, the patch is
    only meaningful once something has been chosen.
    """
    if not options.codex_models:
        outcome.note("no codex models chosen; pick some in `patch-cc` or with --codex")
        return content

    model_ids = [m.id for m in options.codex_models]
    # Family shortcuts (`sol` -> the newest `gpt-<ver>-sol`) resolve to an id
    # before the request is built, so they ride only the surfaces that accept,
    # resolve, and show a model -- validator, both resolvers, picker. They need no
    # `enum` (subagent pins stay explicit, pinned to a stable id, not a moving
    # "newest") and no `redirect`/`context` entry (resolution rewrites them to the
    # id first). They are gated as a unit on the general resolver: if that anchor
    # ever drifts, a `/model sol` would be accepted but never resolved -- a 404 --
    # so absent it, no shortcut is registered anywhere and the ids (which need none
    # of this) carry on untouched.
    shorts = family_aliases(options.codex_models)
    if shorts and _GENERAL_RESOLVER.search(content) is None:
        outcome.note("general model resolver anchor drifted; shortcuts skipped")
        shorts = {}

    content = _register_enum(content, model_ids, outcome.step("enum", expect=True))
    content = _register_validator(
        content, model_ids + list(shorts), outcome.step("validator", expect=True)
    )
    content = _register_resolver(
        content,
        {i: i for i in model_ids} | shorts,
        outcome.step("resolver", expect=True),
    )
    if shorts:
        content = _register_general_resolver(
            content, shorts, outcome.step("general-resolver", expect=True)
        )
    content = _redirect(content, options, outcome.step("redirect", expect=True))
    content = _register_picker(
        content, options.codex_models, shorts, outcome.step("picker")
    )
    content = _register_context(content, options.codex_models, outcome)
    return _register_registry(content, options.codex_models, outcome.step("registry"))


PATCHES = [
    Patch(
        id="codex-models",
        title="Codex models",
        summary="Register your OpenAI/Codex models and route them to the local gateway.",
        group=GROUP_MODELS,
        fn=_codex_models,
        default=False,
        option="--codex",
        anchors=(
            "Optional model override",
            '"opusplan"',
            'case"best":',
            "defaultBaseURL:",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            "],aliases:{",
        ),
    ),
]
