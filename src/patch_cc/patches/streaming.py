"""Live (streaming) thinking.

This is the single most fragile patch in the set: upstream has reshaped the
stream reducer at least three times, and most of their commit traffic lands
here. Two structural choices follow from that:

1. It is built from ~20 **named steps**, each recording its own outcome. A
   scalar hit count cannot tell "everything landed" from "half of it silently
   drifted", which is precisely how this patch hides its own regressions.
2. Steps share discovered identifiers through :class:`Discovery` rather than
   re-deriving them, and every step tolerates its own failure so one drifted
   shape does not take the rest down with it.

Tolerating failure is not the same as ignoring it: the steps the feature cannot
live without are marked *required*, and the two reducer shapes form a group of
which at least one must land. Everything else -- the legacy cleanups upstream has
since dropped -- stays optional, so `doctor` distinguishes "this build lacks that
shape" from "live thinking is dead".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .base import GROUP_OUTPUT, IDENT, Options, Outcome, Patch, compile_js, splice

#: The mutually-exclusive reducer variants. On any one build one of them lands;
#: none landing means upstream shipped a new head shape and live thinking is dead.
REDUCER = "reducer"

#: The two rewrites that *are* live thinking: one creates the virtual message
#: when a thinking block opens, the other appends each delta to it. Recognising
#: a reducer proves nothing on its own -- a variant whose incidental rewrites
#: (threading the setter, clearing state on message_stop) landed while these two
#: drifted reports plenty of hits and streams nothing. Each is checked by the
#: marker its own builder emits, so the test is "did the state update reach the
#: bundle", not "did some literal match".
_CORE_UPDATES = (
    ("block-start", "__cc_streamingThinkingMessage="),
    ("thinking-delta", "__cc_nextStreamingThinkingDelta"),
)


def _record_core_updates(segment: str, outcome: Outcome) -> None:
    """Credit the core state updates found in a rewritten reducer body."""
    for name, marker in _CORE_UPDATES:
        if marker in segment:
            step = outcome.step(name, expect=True)
            step.candidates += 1
            step.applied += 1


@dataclass(slots=True)
class Discovery:
    """Identifiers found in one step and consumed by later ones."""

    streaming_var: str | None = None
    create_message_helper: str | None = None
    transcript_var: str | None = None


# --------------------------------------------------------------- JS builders


def _reset(setter: str, ended_at: str) -> str:
    """The `mark the live block finished` state update, used by many cases."""
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>__cc_prevStreamingThinking?"
        f"{{...__cc_prevStreamingThinking,isStreaming:!1,streamingEndedAt:{ended_at},"
        f"currentIndex:null,currentMessage:null}}:__cc_prevStreamingThinking)"
    )


def _block_start(event: str, setter: str, helper: str) -> str:
    """Create a virtual message when a thinking content block starts.

    Keyed by content-block index, so a block-start handled twice replaces its
    entry rather than appending a duplicate live block.
    """
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>{{"
        f"let __cc_streamingThinkingMessage={helper}({{content:["
        f'{event}.event.content_block.type==="redacted_thinking"'
        f'?{{type:"redacted_thinking",data:{event}.event.content_block.data??""}}'
        f':{{type:"thinking",thinking:""}}],isVirtual:!0}}),'
        f"__cc_nextStreamingThinkingMessages=[...(__cc_prevStreamingThinking?.messages??[])"
        f".filter((__cc_entry)=>__cc_entry.index!=={event}.event.index),"
        f"{{index:{event}.event.index,message:__cc_streamingThinkingMessage}}];"
        f'return{{thinking:{event}.event.content_block.type==="redacted_thinking"'
        f'?{event}.event.content_block.data??"":"",isStreaming:!0,streamingEndedAt:void 0,'
        f"currentIndex:{event}.event.index,currentMessage:__cc_streamingThinkingMessage,"
        f"messages:__cc_nextStreamingThinkingMessages}}}})"
    )


def _delta(event: str, setter: str, helper: str) -> str:
    """Append a thinking delta to the live block."""
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>{{"
        f"let __cc_nextStreamingThinkingDelta=typeof {event}.event.delta.thinking==="
        f'"string"?{event}.event.delta.thinking:"",'
        f'__cc_nextStreamingThinkingText=(__cc_prevStreamingThinking?.thinking??"")'
        f"+__cc_nextStreamingThinkingDelta,"
        f"__cc_nextStreamingThinkingIndex=__cc_prevStreamingThinking?.currentIndex"
        f"??{event}.event.index,"
        f"__cc_nextStreamingThinkingMessage={helper}({{content:["
        f'{{type:"thinking",thinking:__cc_nextStreamingThinkingText}}],isVirtual:!0}}),'
        f"__cc_nextStreamingThinkingMessages=[...(__cc_prevStreamingThinking?.messages??[])"
        f".filter((__cc_entry)=>__cc_entry.index!==__cc_nextStreamingThinkingIndex),"
        f"{{index:__cc_nextStreamingThinkingIndex,message:__cc_nextStreamingThinkingMessage}}];"
        f"return __cc_prevStreamingThinking?{{...__cc_prevStreamingThinking,"
        f"thinking:__cc_nextStreamingThinkingText,isStreaming:!0,streamingEndedAt:void 0,"
        f"currentIndex:__cc_nextStreamingThinkingIndex,"
        f"currentMessage:__cc_nextStreamingThinkingMessage,"
        f"messages:__cc_nextStreamingThinkingMessages}}"
        f":{{thinking:__cc_nextStreamingThinkingText,isStreaming:!0,streamingEndedAt:void 0,"
        f"currentIndex:{event}.event.index,"
        f"currentMessage:__cc_nextStreamingThinkingMessage,"
        f"messages:[{{index:{event}.event.index,"
        f"message:__cc_nextStreamingThinkingMessage}}]}}}})"
    )


# ------------------------------------------------------------------- step 1

_MEMO_CACHE = compile_js(
    rf"if\(({IDENT})\[(\d+)\]!==({IDENT})\|\|\1\[(\d+)\]!==({IDENT})\|\|\1\[(\d+)\]!==({IDENT})\)"
    rf"([\s\S]{{0,700}}?thinking:\5\.thinking[\s\S]{{0,700}}?)"
    rf"\1\[\2\]=\3,\1\[\4\]=\5,\1\[\6\]=\7,(\1\[\d+\]={IDENT};)"
)


def _step_memo_cache(content: str, outcome: Outcome) -> str:
    """Key the memo cache on `thinking?.thinking`, not the wrapper object.

    Without this the comparator sees the same object identity across deltas and
    never re-renders while text is still streaming in.
    """
    step = outcome.step("memo-cache")

    def rewrite(match: re.Match[str]) -> str:
        cache, i1, v1, i2, v2, i3, v3, middle, tail = match.groups()
        step.candidates += 1
        if f"{v2}?.thinking" in match.group(0):
            return match.group(0)
        step.applied += 1
        return (
            f"if({cache}[{i1}]!=={v1}||{cache}[{i2}]!=={v2}?.thinking||{cache}[{i3}]!=={v3})"
            f"{middle}{cache}[{i1}]={v1},{cache}[{i2}]={v2}?.thinking,{cache}[{i3}]={v3},{tail}"
        )

    return _MEMO_CACHE.sub(rewrite, content)


# ------------------------------------------------------------------- step 2

#: How far back the state back-scan looks for the setter's ``useState(null)``.
#: Deliberately *not* widened past the worst observed reach (35k on 2.1.217) by
#: much: minified setter names repeat across scopes, so a wider window trades a
#: loud discovery failure -- `prop-threading` is required, so it stops the patch
#: -- for the chance of silently binding an unrelated same-named state. The step
#: notes the distance actually reached, which is the early warning instead.
_DISCOVER_WINDOW = 50_000

_HIDE_PAST = compile_js(rf"hidePastThinking:!0,streamingThinking:({IDENT})")
_ON_STREAMING = compile_js(rf"onStreamingThinking:({IDENT})")
_CREATE_ELEMENT_CALL = compile_js(rf"createElement\(({IDENT}),\{{([^{{}}]*?)\}}\)")
_PROMPT_RENDERER = compile_js(
    rf"createElement\(({IDENT}),\{{([\s\S]{{0,2000}}?placeholderElement:[\s\S]{{0,2000}}?"
    rf"agentDefinitions:[^}}]*?onOpenRateLimitOptions:[^}}]*?isLoading:)([^,}}]+)"
    rf"(,streamingText:[^}}]*?(?:showThinkingHint:[^}}]*?)?isBriefOnly:[^}}]*?)\}}\)"
)
_JSX_MAIN_PROPS = compile_js(
    r"(screen:[^,}]+,streamingToolUses:[^,}]+,)"
    r"(showAllInTranscript:[^,}]+,agentDefinitions:[^,}]+,onOpenRateLimitOptions:[^,}]+,isLoading:[^,}]+)"
)
_JSX_TRANSCRIPT_PROPS = compile_js(
    r"(screen:[^,}]+,agentDefinitions:[^,}]+,streamingToolUses:[^,}]+,)"
    r"(showAllInTranscript:[^,}]+,onOpenRateLimitOptions:[^,}]+,isLoading:[^,}]+)"
)


def _discover_streaming_var(content: str, found: Discovery, outcome: Outcome) -> None:
    """Find the state variable holding live thinking.

    2.1.216 no longer ships `hidePastThinking`, so the primary anchor is already
    dead and we rely on the `onStreamingThinking` -> `useState(null)` back-scan.
    Losing that fallback too would leave this patch with nothing, so the scan
    records how far back it had to reach: that distance against
    :data:`_DISCOVER_WINDOW` is the warning that the next hoist will break it.
    """
    primary = _HIDE_PAST.search(content)
    if primary:
        found.streaming_var = primary.group(1)
        return

    step = outcome.step("discover")
    step.note("hidePastThinking anchor gone; using useState back-scan")
    for match in _ON_STREAMING.finditer(content):
        setter = match.group(1)
        start = max(0, match.start() - _DISCOVER_WINDOW)
        window = content[start : match.start()]
        state = compile_js(
            rf"\[({IDENT}),{re.escape(setter)}\]={IDENT}\.useState\(null\)"
        )
        candidates = list(state.finditer(window))
        if candidates:
            found.streaming_var = candidates[-1].group(1)
            reach = len(window) - candidates[-1].start()
            step.note(f"resolved {reach:,} chars back of {_DISCOVER_WINDOW:,}")
            if len(candidates) > 1:
                # Nearest wins, which is a guess the moment there is more than
                # one. Both shipped builds have exactly one state destructured
                # to this setter in the *whole* bundle; a second would mean the
                # pairing by setter name has stopped being an identification.
                step.note(
                    f"{len(candidates)} states share this setter; took the nearest"
                )
            return


def _step_prop_threading(content: str, found: Discovery, outcome: Outcome) -> str:
    """Pass the live-thinking state into the renderers that need it."""
    step = outcome.step("prop-threading", expect=True)
    if found.streaming_var is None:
        step.note("no streaming state variable found; skipped")
        return content
    var = found.streaming_var

    def rewrite_create_element(match: re.Match[str]) -> str:
        component, props = match.group(1), match.group(2)
        required = (
            "streamingToolUses:",
            "toolJSX:",
            "agentDefinitions:",
            "onOpenRateLimitOptions:",
            "conversationId:",
            "isLoading:",
        )
        forbidden = ("streamingThinking:", "hidePastThinking:")
        if any(tok not in props for tok in required) or any(
            t in props for t in forbidden
        ):
            return match.group(0)
        step.candidates += 1
        step.applied += 1
        return f"createElement({component},{{{props},streamingThinking:{var}}})"

    output = _CREATE_ELEMENT_CALL.sub(rewrite_create_element, content)

    def rewrite_prompt(match: re.Match[str]) -> str:
        if "streamingThinking:" in match.group(0):
            return match.group(0)
        component, before, is_loading, after = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"createElement({component},{{{before}{is_loading},"
            f"streamingThinking:{var}{after}}})"
        )

    output = _PROMPT_RENDERER.sub(rewrite_prompt, output)

    def inject(match: re.Match[str]) -> str:
        if "streamingThinking:" in match.group(0):
            return match.group(0)
        step.candidates += 1
        step.applied += 1
        return f"{match.group(1)}streamingThinking:{var},{match.group(2)}"

    output = _JSX_MAIN_PROPS.sub(inject, output)
    return _JSX_TRANSCRIPT_PROPS.sub(inject, output)


# ------------------------------------------------------------------- step 3

_THINKING_DISPLAY = compile_js(
    rf"({IDENT})=({IDENT})\.type!==\"disabled\"&&!({IDENT})"
    rf"\(process\.env\.CLAUDE_CODE_DISABLE_THINKING\),({IDENT})=\1"
    rf"(?:&&{IDENT}\(\)&&{IDENT}\({IDENT}\))?\?\2\.display(?:\?\?void 0)?:void 0,({IDENT})=void 0;"
)

# 2.1.216 hoists the env check into its own variable and gates the display
# value behind extra feature/model helpers. The helper chain is kept verbatim;
# only the display expression gains the `??"summarized"` default.
_THINKING_DISPLAY_2 = compile_js(
    rf"({IDENT})=({IDENT})\(process\.env\.CLAUDE_CODE_DISABLE_THINKING\),"
    rf"({IDENT})=({IDENT})\.type!==\"disabled\"&&!\1,"
    rf"({IDENT})=\3((?:&&{IDENT}\((?:{IDENT})?\))*)\?\4\.display:void 0,"
)


def _step_display_mode(content: str, outcome: Outcome) -> str:
    """Default the thinking request to `summarized`.

    Without a display mode in the request the API streams signature-only (or
    late) thinking, so the live row starves -- worst on short thinks. Upstream
    only requests summaries when the `showThinkingSummaries` setting is on;
    default it on instead.
    """
    step = outcome.step("display-mode", expect=True)

    def rewrite(match: re.Match[str]) -> str:
        enabled, config, env_helper, display, request = match.groups()
        step.candidates += 1
        if 'display??"summarized"' in match.group(0):
            return match.group(0)
        step.applied += 1
        return (
            f'{enabled}={config}.type!=="disabled"&&!{env_helper}'
            f"(process.env.CLAUDE_CODE_DISABLE_THINKING),"
            f'{display}={enabled}?{config}.display??"summarized":void 0,{request}=void 0;'
        )

    output = _THINKING_DISPLAY.sub(rewrite, content)

    def rewrite_hoisted(match: re.Match[str]) -> str:
        env_var, env_helper, enabled, config, display, guards = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"{env_var}={env_helper}(process.env.CLAUDE_CODE_DISABLE_THINKING),"
            f'{enabled}={config}.type!=="disabled"&&!{env_var},'
            f'{display}={enabled}{guards}?{config}.display??"summarized":void 0,'
        )

    return _THINKING_DISPLAY_2.sub(rewrite_hoisted, output, count=1)


# ------------------------------------------------------------------- step 4

# The braces around the guarded call are optional *as a pair*: a bare `\}?` tail
# would happily eat the enclosing block's closing brace on the unbraced shape and
# the rewrite -- which emits its own -- would not put it back. Hence the
# conditional: consume the closing brace only if the opening one was there.
_ASSISTANT_THINKING = compile_js(
    rf"let ({IDENT})=({IDENT})\.message\.content\.find\(\(({IDENT})\)=>"
    rf'\3\.type==="thinking"\);if\(\1&&\1\.type==="thinking"\)(\{{)?({IDENT})'
    rf"\?\.\(\(\)=>\(\{{thinking:\1\.thinking,isStreaming:!1,"
    rf"streamingEndedAt:Date\.now\(\)\}}\)\)(?(4)\}})"
)


def _step_final_summary(content: str, outcome: Outcome) -> str:
    """Include redacted thinking in the final assistant-message summary."""
    step = outcome.step("final-summary")

    def rewrite(match: re.Match[str]) -> str:
        block, message, item, _brace, setter = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"let {block}={message}.message.content.find(({item})=>"
            f'{item}.type==="thinking"||{item}.type==="redacted_thinking");'
            f'if({block}&&({block}.type==="thinking"||{block}.type==="redacted_thinking"))'
            f'{setter}?.(()=>({{thinking:{block}.type==="thinking"'
            f'?{block}.thinking:{block}.data??"",isStreaming:!1,'
            f"streamingEndedAt:Date.now()}}))"
        )

    return _ASSISTANT_THINKING.sub(rewrite, content)


# ------------------------------------------------------------------- step 5

_MEMO_ASSIGN = compile_js(rf"({IDENT})=({IDENT})\.memo\(({IDENT}),({IDENT})\)")


def _step_memo_removal(content: str, outcome: Outcome) -> str:
    """Unwrap the message-row memo whose comparator suppresses live updates."""
    step = outcome.step("memo-removal")
    output = content
    pos = 0
    while True:
        match = _MEMO_ASSIGN.search(output, pos)
        if not match:
            break
        lhs, _ns, render_fn, comparator = match.groups()
        pos = match.end()

        start = output.find(f"function {comparator}(")
        if start == -1:
            continue
        body = output[start : start + 2200]
        if not all(
            tok in body
            for tok in (
                ".screen!==",
                ".columns!==",
                ".lastThinkingBlockId",
                ".streamingToolUseIDs",
            )
        ):
            continue

        step.candidates += 1
        replacement = f"{lhs}={render_fn}"
        if replacement != match.group(0):
            output = splice(output, match.start(), match.end(), replacement)
            step.applied += 1
            pos = match.start() + len(replacement)
    return output


# ------------------------------------------------------------------- step 6

_LINGER_LABEL = compile_js(
    rf"({IDENT}):\{{if\(!({IDENT})\)\{{({IDENT})=!1;break \1\}}"
    rf"if\(\2\.isStreaming\)\{{\3=!0;break \1\}}"
    rf"if\(\2\.streamingEndedAt\)\{{\3=Date\.now\(\)-\2\.streamingEndedAt<30000;break \1\}}"
    rf"\3=!1\}}let ({IDENT})=\3"
)
_LINGER_MEMO = compile_js(
    rf"({IDENT})=({IDENT})\.useMemo\(\(\)=>\{{if\(!({IDENT})\)return!1;"
    rf"if\(\3\.isStreaming\)return!0;"
    rf"if\(\3\.streamingEndedAt\)return Date\.now\(\)-\3\.streamingEndedAt<30000;"
    rf"return!1\}},\[\3\]\)"
)


def _step_linger(content: str, outcome: Outcome) -> str:
    """Drop the 30-second post-stream linger; show only while streaming."""
    step = outcome.step("linger")

    def rewrite_label(match: re.Match[str]) -> str:
        step.candidates += 1
        step.applied += 1
        return (
            f"let {match.group(4)}=!!({match.group(2)}&&{match.group(2)}.isStreaming)"
        )

    def rewrite_memo(match: re.Match[str]) -> str:
        visible, ns, stream = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"{visible}={ns}.useMemo(()=>!!({stream}&&{stream}.isStreaming),[{stream}])"
        )

    output = _LINGER_LABEL.sub(rewrite_label, content)
    return _LINGER_MEMO.sub(rewrite_memo, output)


# ------------------------------------------------------------------- step 7

_TOOLUSE_HELPERS = compile_js(
    rf"let {IDENT}=({IDENT})\(\{{content:\[{IDENT}\.contentBlock\]\}}\);"
    rf"return {IDENT}\.uuid=({IDENT})\({IDENT}\.contentBlock\.id,0\),({IDENT})\(\[{IDENT}\]\)"
)
_RENDERER_HAS_VAR = compile_js(
    rf"\(\{{messages:[^}}]*?streamingToolUses:{IDENT},streamingThinking:({IDENT}),showAllInTranscript:"
)
_RENDERER_SIGNATURE = compile_js(
    rf"(\(\{{messages:[^}}]*?streamingToolUses:{IDENT},)(showAllInTranscript:)"
)
_TRANSCRIPT_VAR = compile_js(
    rf"streamingToolUses:{IDENT},[^}}]*streamingThinking:({IDENT}),streamingText:"
)


def _step_transcript_signature(content: str, found: Discovery, outcome: Outcome) -> str:
    """Make sure the transcript renderer actually receives the live state."""
    step = outcome.step("transcript-signature", expect=True)

    helpers = _TOOLUSE_HELPERS.search(content)
    if helpers:
        found.create_message_helper = helpers.group(1)

    existing = _RENDERER_HAS_VAR.search(content)
    if existing:
        found.transcript_var = existing.group(1)
        step.candidates += 1
        step.applied += 1  # nothing to do; upstream already threads it
        return content

    output = content
    if found.streaming_var is not None:

        def inject(match: re.Match[str]) -> str:
            if "streamingThinking:" in match.group(0):
                return match.group(0)
            step.candidates += 1
            step.applied += 1
            found.transcript_var = "__cc_streamingThinking"
            return f"{match.group(1)}streamingThinking:__cc_streamingThinking,{match.group(2)}"

        output = _RENDERER_SIGNATURE.sub(inject, output, count=1)

    if found.transcript_var is None:
        fallback = _TRANSCRIPT_VAR.search(output)
        if fallback:
            found.transcript_var = fallback.group(1)
    return output


# ------------------------------------------------------------------- step 8

_INLINE_EXTRAS = compile_js(
    rf"({IDENT})=({IDENT})\.useMemo\(\(\)=>({IDENT})\.flatMap\(\(({IDENT})\)=>\{{"
    rf"let ({IDENT})=({IDENT})\(\{{content:\[\4\.contentBlock\]\}}\);"
    rf"return \5\.uuid=({IDENT})\(\4\.contentBlock\.id,0\),({IDENT})\(\[\5\]\)\}}\),\[\3\]\)"
)


def _step_inline_extras(content: str, found: Discovery, outcome: Outcome) -> str:
    """Render live thinking inline, ordered with streaming tool-use blocks."""
    step = outcome.step("inline-extras", expect=True)
    if not found.transcript_var:
        step.note("no transcript streaming variable; skipped")
        return content
    var = found.transcript_var

    def rewrite(match: re.Match[str]) -> str:
        extras, ns, tool_uses, entry, message, helper, uuid_helper, normalize = (
            match.groups()
        )
        step.candidates += 1
        step.applied += 1
        found.create_message_helper = helper
        return (
            f"{extras}={ns}.useMemo(()=>{{"
            f"let __cc_streamingToolUseExtras={tool_uses}.map(({entry})=>{{"
            f"let {message}={helper}({{content:[{entry}.contentBlock]}});"
            f"return {message}.uuid={uuid_helper}({entry}.contentBlock.id,0),"
            f"{{index:{entry}.index??9007199254740991,"
            f"messages:{normalize}([{message}])}}}}),"
            f"__cc_streamingThinkingExtras=({var}?.messages??[])"
            f".map((__cc_entry,__cc_index)=>({{"
            f"index:__cc_entry.index??9007199254740991+__cc_index,"
            f"messages:{normalize}([__cc_entry.message??__cc_entry])}}));"
            f"return[...__cc_streamingToolUseExtras,...__cc_streamingThinkingExtras]"
            f".sort((__cc_a,__cc_b)=>__cc_a.index===__cc_b.index?0:__cc_a.index-__cc_b.index)"
            f".flatMap((__cc_entry)=>__cc_entry.messages)}},[{tool_uses},{var}])"
        )

    return _INLINE_EXTRAS.sub(rewrite, content)


# ------------------------------------------------------------------- step 9

_LIVE_ROW = compile_js(
    rf"({IDENT})&{{2}}({IDENT})&{{2}}!({IDENT})&{{2}}({IDENT})\.createElement\(({IDENT}),"
    rf"\{{marginTop:1\}},\4\.createElement\(({IDENT}),\{{param:\{{type:\"thinking\","
    rf"thinking:\2\.thinking\}},addMargin:!1,isTranscriptMode:!0,verbose:({IDENT}),"
    rf"hideInTranscript:!1\}}\)\)"
)


def _step_bottom_row(content: str, outcome: Outcome) -> str:
    """Remove the separate bottom-pinned live row now that it renders inline."""
    step = outcome.step("bottom-row")

    def rewrite(_match: re.Match[str]) -> str:
        step.candidates += 1
        step.applied += 1
        return "null"

    return _LIVE_ROW.sub(rewrite, content)


# ------------------------------------------------------ steps 10-11: reducer

# The reducer rewrites are *insertions at dispatch points*, never edits of what
# an arm already does. Each state update is spliced in where the reducer
# dispatches on an event-type string -- after a run of ``case`` labels, or into
# an ``if`` condition -- and the arm's own body is left byte-untouched. The body
# is upstream's busiest surface (2.1.226 threaded a progress flag through five
# arms in one release) and modelling any of it means enumerating spellings that
# churn per build; the dispatch strings are the API's own vocabulary and do not.

_LEGACY_ANCHOR = 'type!=="stream_event"&&'
_FN_SIG = compile_js(rf"^function {IDENT}\(([^)]*)\)\{{")

#: The options-bag reducer head, 2.1.138 to date. Bounded at the grammar's
#: declarator edge: in this one-line minified bundle only ``;`` (statement end)
#: or ``,`` (next declarator) can follow ``}=<param>``, and which one is
#: minifier noise -- 2.1.226 broke the old matcher purely by appending
#: ``,d=...`` to the ``let``.
_OPTIONS_HANDLER = compile_js(
    rf"function {IDENT}\(({IDENT}),({IDENT})(?:,{IDENT})*\)"
    rf"\{{let\{{([^{{}}]*)\}}=\2(?=[;,])"
)


def _prop_var(props: str, name: str, *, shorthand: bool = False) -> str | None:
    alias = compile_js(rf"(?:^|,){re.escape(name)}:({IDENT})").search(props)
    if alias:
        return alias.group(1)
    if shorthand and compile_js(rf"(?:^|,){re.escape(name)}(?:,|$)").search(props):
        return name
    return None


def _handler_is_stream_reducer(segment: str) -> bool:
    return all(
        tok in segment
        for tok in (
            'type==="stream_request_start"',
            'case"thinking_delta"',
            "content_block_start",
        )
    )


def _insert_after_labels(
    segment: str, labels: tuple[str, ...], expr: str, step: Outcome
) -> str:
    """Run ``expr`` first in every arm dispatching on one of these ``case`` labels.

    The insertion lands after the whole consecutive label chain, so a fused
    ``case"thinking":case"redacted_thinking":`` arm gets one update and split
    *terminating* arms get one each -- both spellings of the same dispatch.
    Inserting a statement is valid whatever the arm holds: a following braced
    arm simply becomes a bare block, whose ``let``s stay scoped inside it.
    """
    chain = compile_js("(?:" + "|".join(rf'case"{label}":' for label in labels) + ")+")
    text = expr + ";"
    for match in reversed(list(chain.finditer(segment))):
        step.candidates += 1
        if segment[match.end() : match.end() + len(text)] == text:
            step.applied += 1  # already carries the update: the goal, achieved
            continue
        segment = splice(segment, match.end(), match.end(), text)
        step.applied += 1
    return segment


def _guard_dispatch(segment: str, needle: str, expr: str, step: Outcome) -> str:
    """Run ``expr`` first wherever the reducer evaluates this dispatch test.

    The test expression itself is wrapped -- ``(test&&((expr),!0))`` -- which
    preserves its value exactly: true stays true through the comma, false never
    reaches ``expr``. Value-equivalence is what makes the wrap survive any
    surrounding composition unread -- an ``if`` of its own, a compound
    condition (short-circuit keeps ``expr`` off the paths that did not
    dispatch), a ternary, an assignment -- where guarding the *enclosing*
    condition would run the update for the other side of a bare ``||``.
    """
    guard = f"&&(({expr}),!0)"
    for match in reversed(list(compile_js(re.escape(needle)).finditer(segment))):
        step.candidates += 1
        if segment[match.end() : match.end() + len(guard)] == guard:
            step.applied += 1  # already guarded: the goal, achieved
            continue
        segment = splice(segment, match.start(), match.end(), f"({needle}{guard})")
        step.applied += 1
    return segment


def _rewrite_reducer_segment(
    segment: str, event: str, setter: str, helper: str, outcome: Outcome
) -> str:
    """Splice the live-thinking state updates into one recognised reducer.

    Each dispatch point is its own named step, so a build that drops one --
    upstream folding an arm away -- reads as that point's absence by name,
    never as a bare count drop inside an aggregate. The two updates that *are*
    the feature stay credited by their markers (:data:`_CORE_UPDATES`), not by
    these insertion counts.
    """
    ended = _reset(setter, "Date.now()")
    cleared = _reset(setter, "void 0")

    segment = _guard_dispatch(
        segment,
        f'{event}.type==="stream_request_start"',
        f"{setter}?.(null)",
        outcome.step("request-start"),
    )
    segment = _guard_dispatch(
        segment,
        f'{event}.event.type==="message_stop"',
        ended,
        outcome.step("message-stop"),
    )
    segment = _insert_after_labels(
        segment, ("text",), cleared, outcome.step("text-clear")
    )
    segment = _insert_after_labels(
        segment, ("message_delta",), cleared, outcome.step("message-delta-clear")
    )
    segment = _insert_after_labels(
        segment,
        ("thinking", "redacted_thinking"),
        _block_start(event, setter, helper),
        outcome.step("thinking-start"),
    )
    return _insert_after_labels(
        segment,
        ("thinking_delta",),
        _delta(event, setter, helper),
        outcome.step("thinking-append"),
    )


def _step_reducer_options(content: str, found: Discovery, outcome: Outcome) -> str:
    """2.1.138+ shape: an options bag destructured at the head.

    One step for every options-bag build: whether the bag already destructures
    ``onStreamingThinking`` (2.1.138-183) or dropped it (2.1.183+) is the same
    shape with the prop present or absent, so the setter is reused when found
    and threaded into the destructuring when not -- one code path, no variant.
    """
    step = outcome.step("reducer-options", expect=REDUCER)
    helper = found.create_message_helper
    if helper is None:
        step.note("no virtual-message helper discovered; skipped")
        return content

    output, pos = content, 0
    while True:
        match = _OPTIONS_HANDLER.search(output, pos)
        if not match:
            break
        event, props = match.group(1), match.group(3)
        pos = match.end()
        if _prop_var(props, "onSetStreamMode", shorthand=True) is None:
            continue

        end = output.find("function ", match.end())
        if end == -1:
            continue
        original = output[match.start() : end]
        if not _handler_is_stream_reducer(original):
            continue
        step.candidates += 1

        segment = original
        setter = _prop_var(props, "onStreamingThinking", shorthand=True)
        if setter is None:
            # Threaded at the *front* of the pattern, which is valid whatever
            # the pattern ends with -- a rest element or trailing comma there
            # would make an append a SyntaxError the write verifier cannot see.
            setter = "__cc_onStreamingThinking"
            props_start = match.start(3) - match.start()
            segment = splice(
                segment, props_start, props_start, f"onStreamingThinking:{setter},"
            )

        updated = _rewrite_reducer_segment(segment, event, setter, helper, outcome)
        _record_core_updates(updated, outcome)
        if updated != original:
            step.applied += 1
            output = splice(output, match.start(), end, updated)
            pos = match.start() + len(updated)
        elif all(marker in original for _, marker in _CORE_UPDATES):
            step.applied += 1  # a previous run already did the work: achieved
    return output


def _step_reducer_legacy(content: str, found: Discovery, outcome: Outcome) -> str:
    """Pre-2.1.138 shape: positional parameters, no options bag."""
    step = outcome.step("reducer-legacy", expect=REDUCER)
    anchor = content.find(_LEGACY_ANCHOR)
    if anchor == -1:
        return content
    if content.find('type==="stream_request_start"', anchor) == -1:
        return content
    if content.find('case"thinking_delta"', anchor) == -1:
        return content

    start = content.rfind("function ", 0, anchor)
    end = content.find("function ", anchor + len(_LEGACY_ANCHOR))
    if start == -1 or end == -1:
        return content

    segment = content[start:end]
    signature = _FN_SIG.search(segment)
    if not signature:
        return content
    params = [p.strip() for p in signature.group(1).split(",")]
    if len(params) < 7:
        return content

    helper = found.create_message_helper
    if helper is None:
        # Without the helper the two marker steps can never land, so the
        # fixpoint always drops the patch: a resets-only rewrite ships nothing.
        step.note("no virtual-message helper discovered; skipped")
        return content
    step.candidates += 1

    updated = _rewrite_reducer_segment(segment, params[0], params[6], helper, outcome)
    _record_core_updates(updated, outcome)
    if updated == segment:
        if all(marker in segment for _, marker in _CORE_UPDATES):
            step.applied += 1  # a previous run already did the work: achieved
        return content
    step.applied += 1
    return splice(content, start, end, updated)


# ------------------------------------------------------------------ assembly


def _live_thinking(content: str, _options: Options, outcome: Outcome) -> str:
    found = Discovery()
    # Declared before anything runs: an expectation that only comes into
    # existence once its own rewrite succeeds can never report that rewrite
    # missing -- which is exactly the silence being designed out here.
    for name, _marker in _CORE_UPDATES:
        outcome.step(name, expect=True)
    output = _step_memo_cache(content, outcome)
    _discover_streaming_var(output, found, outcome)
    output = _step_prop_threading(output, found, outcome)
    output = _step_display_mode(output, outcome)
    output = _step_final_summary(output, outcome)
    output = _step_memo_removal(output, outcome)
    output = _step_linger(output, outcome)
    output = _step_transcript_signature(output, found, outcome)
    output = _step_inline_extras(output, found, outcome)
    output = _step_bottom_row(output, outcome)
    output = _step_reducer_options(output, found, outcome)
    output = _step_reducer_legacy(output, found, outcome)

    if found.streaming_var is None:
        outcome.note("live-thinking state variable was never found")
    return output


PATCHES = [
    Patch(
        id="live-thinking",
        title="Stream thinking live",
        summary="Show thinking as it is generated, inline and in order, instead of "
        "only after the turn finishes.",
        group=GROUP_OUTPUT,
        fn=_live_thinking,
        anchors=(
            "onStreamingThinking:",
            'case"thinking_delta"',
            'type==="stream_request_start"',
            "content_block_start",
        ),
    ),
]
