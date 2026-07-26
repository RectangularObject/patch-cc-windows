"""UI chrome: spinner tips, the --version marker, and branding."""

from __future__ import annotations

import re

from .base import (
    DEFAULT_SUFFIX,
    GROUP_CHROME,
    IDENT,
    Options,
    Outcome,
    Patch,
    compile_js,
    js_string,
)

# ---------------------------------------------------------- spinner tips

_SPINNER_GUARD = compile_js(
    rf"if\({IDENT}\(\)\.spinnerTipsEnabled===!1\)(?:\{{return;?\}}|return;)"
)
_SPINNER_EXPR = compile_js(rf"{IDENT}\.spinnerTipsEnabled!==!1")


def _disable_spinner_tips(content: str, _options: Options, outcome: Outcome) -> str:
    """Force spinner tips off through both code paths that can enable them.

    Each path counts its candidates from the *setting name*, not from its own
    regex. Counting matches would make "upstream reshaped this path" identical
    to "this build hasn't got it" -- both zero, both silently absent, while the
    other path lands and carries the patch to green with tips still showing.
    Off the setting, a drifted path is `candidates > 0, applied == 0`: a miss.
    """
    guard = outcome.step("guard")
    guard.candidates = content.count("spinnerTipsEnabled===!1")

    def kill_guard(_match: re.Match[str]) -> str:
        guard.applied += 1
        return "if(!0)return;"

    output = _SPINNER_GUARD.sub(kill_guard, content)

    expr = outcome.step("expr")
    expr.candidates = output.count("spinnerTipsEnabled!==!1")

    def kill_expr(_match: re.Match[str]) -> str:
        expr.applied += 1
        return "!1"

    return _SPINNER_EXPR.sub(kill_expr, output)


# ---------------------------------------------------------- version marker

_VERSION_NEEDLE = "}.VERSION} (Claude Code)"


def _js_template_escape(text: str) -> str:
    """Make arbitrary text safe inside a JS template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _version_output(content: str, options: Options, outcome: Outcome) -> str:
    """Append a marker line to plain ``--version`` output.

    The marker text is the user's ``--suffix`` (default ``(patched)``). The
    needle sits inside a template literal, so the suffix is escaped for that
    context.
    """
    marker = "\\n" + _js_template_escape(options.version_suffix or DEFAULT_SUFFIX)
    output = content
    index = output.find(_VERSION_NEEDLE)
    while index != -1:
        outcome.candidates += 1
        marker_at = index + len(_VERSION_NEEDLE)
        if output[marker_at : marker_at + len(marker)] == marker:
            index = output.find(_VERSION_NEEDLE, marker_at + len(marker))
            continue
        output = output[:marker_at] + marker + output[marker_at:]
        outcome.applied += 1
        index = output.find(_VERSION_NEEDLE, marker_at + len(marker))
    return output


# ---------------------------------------------------------- branding


def _branding(content: str, options: Options, outcome: Outcome) -> str:
    """Rename visible ``Claude Code`` branding to the user's chosen name.

    Only the small set of visible startup/help strings are touched, each as its
    own step so partial upstream drift is visible.
    """
    if not options.rebrands:
        outcome.note("brand unchanged; nothing to do")
        return content

    # Every site below quotes through `js_string`, never by interpolating into a
    # literal written here: the brand is arbitrary text, and hand-escaping only
    # the quote and the backslash let a newline through into a double-quoted
    # literal, producing a bundle that parsed nowhere but verified fine.
    brand = options.brand
    output = content

    def swap(step_name: str, pattern: re.Pattern[str], replace) -> None:
        nonlocal output
        step = outcome.step(step_name)

        def rewrite(match: re.Match[str]) -> str:
            step.candidates += 1
            result = replace(match)
            if result != match.group(0):
                step.applied += 1
            return result

        output = pattern.sub(rewrite, output)

    swap(
        "bold-text",
        compile_js(
            rf'({IDENT})\.createElement\(({IDENT}),\{{bold:!0\}},"Claude Code"\)'
        ),
        lambda m: (
            f"{m.group(1)}.createElement({m.group(2)},{{bold:!0}},{js_string(brand)})"
        ),
    )
    swap(
        "bold-jsx",
        compile_js(
            rf'({IDENT})\.(jsx|jsxs)\(({IDENT}),\{{bold:!0,children:"Claude Code"\}}\)'
        ),
        lambda m: (
            f"{m.group(1)}.{m.group(2)}({m.group(3)},"
            f"{{bold:!0,children:{js_string(brand)}}})"
        ),
    )
    swap(
        "help-title",
        compile_js(
            r"title:(`Claude Code v\$\{[\s\S]*?\.VERSION\}`),"
            r'color:"professionalBlue",defaultTab:"general"'
        ),
        lambda m: (
            f'title:{m.group(1)}.replace("Claude Code",{js_string(brand)}),'
            f'color:"professionalBlue",defaultTab:"general"'
        ),
    )
    swap(
        "welcome-for",
        compile_js(r'"Welcome to Claude Code for "'),
        lambda _m: js_string(f"Welcome to {brand} for "),
    )
    swap(
        "welcome",
        compile_js(r'"Welcome to Claude Code"'),
        lambda _m: js_string(f"Welcome to {brand}"),
    )
    swap(
        "children-array",
        compile_js(r'(color:"claude",bold:!0,children:\[)"Claude Code"(," "\])'),
        lambda m: f"{m.group(1)}{js_string(brand)}{m.group(2)}",
    )
    swap(
        "styled-title",
        compile_js(rf'({IDENT})\("claude",({IDENT})\)\("Claude Code"\)'),
        lambda m: f'{m.group(1)}("claude",{m.group(2)})({js_string(brand)})',
    )
    swap(
        "styled-title-padded",
        compile_js(rf'({IDENT})\("claude",({IDENT})\)\(" Claude Code "\)'),
        lambda m: f'{m.group(1)}("claude",{m.group(2)})({js_string(f" {brand} ")})',
    )

    return output


# ---------------------------------------------------------- startup org label

# The separator as the bundle may spell it: today's bundler writes the `·` as
# the escape text `\xB7`, and a charset flip would emit the raw character. Both
# spell the same string, so both are accepted -- a representation variant like
# the minifier's brace flips, not a second shape.
_SEP = r"(?:\\xB7|\xB7)"

# The welcome screen's info line: `model · plan` -- and, when the account has an
# organizationName (for personal claude.ai accounts, that is the account email),
# ` · <organizationName>` after it. One conditional template, and upstream ships
# the no-org variant as its own false branch, so hiding the segment is selecting
# a shape the bundle already carries -- separator and all -- not inventing one.
_ORG_LINE = compile_js(
    rf"!process\.env\.IS_DEMO&&({IDENT})\?\.organizationName\?"
    rf"`\$\{{({IDENT})\}} {_SEP} \$\{{({IDENT})\}} {_SEP} \$\{{\1\.organizationName\}}`"
    rf":`\$\{{\2\}} {_SEP} \$\{{\3\}}`"
)

#: The composition itself -- a separator joining the account's organizationName
#: into a template -- is the durable fingerprint; candidates count off it so a
#: reshaped ternary reads as "found it, rewrote nothing" (a matcher to repair)
#: instead of the zero that would equally mean upstream retired the segment.
_ORG_NEEDLE = compile_js(rf"{_SEP} \$\{{{IDENT}\.organizationName\}}")


def _org_label(content: str, options: Options, outcome: Outcome) -> str:
    """Replace -- or, with an empty label, drop -- the welcome line's org segment.

    One rule builds the replacement either way: the line is `model · plan`,
    plus ` · <label>` exactly when there is a label. Empty therefore removes
    the separator with the segment, and a custom label takes the org text's
    place. The `IS_DEMO`/has-org gate goes with the ternary: a baked label is
    unconditional by construction, and the hidden form needs no gate at all.
    """
    outcome.candidates += len(_ORG_NEEDLE.findall(content))
    label = options.org_label
    tail = f" \\xB7 {_js_template_escape(label)}" if label else ""

    def rewrite(match: re.Match[str]) -> str:
        outcome.applied += 1
        return f"`${{{match.group(2)}}} \\xB7 ${{{match.group(3)}}}{tail}`"

    return _ORG_LINE.sub(rewrite, content)


PATCHES = [
    Patch(
        id="spinner-tips",
        title="Disable spinner tips",
        summary="Stop the loading spinner from showing rotating tips.",
        group=GROUP_CHROME,
        fn=_disable_spinner_tips,
        default=False,
        anchors=("spinnerTipsEnabled",),
    ),
    Patch(
        id="version-marker",
        title="Mark --version as patched",
        summary="Append a marker line to `claude --version` (custom text via --suffix).",
        group=GROUP_CHROME,
        fn=_version_output,
        option="--suffix",
        anchors=("}.VERSION} (Claude Code)",),
    ),
    Patch(
        id="branding",
        title="Custom startup name",
        summary="Rename the startup/help branding (default: your username's Code).",
        group=GROUP_CHROME,
        fn=_branding,
        option="--brand",
        anchors=('"Welcome to Claude Code"', '{bold:!0},"Claude Code"'),
    ),
    Patch(
        id="org-label",
        title="Startup org/email label",
        summary="Replace or hide the org/email shown after the plan name on the welcome screen.",
        group=GROUP_CHROME,
        fn=_org_label,
        default=False,
        option="--org-label",
        anchors=("organizationName", "IS_DEMO"),
    ),
]
