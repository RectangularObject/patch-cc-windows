"""Live (streaming) thinking.

This is the patch upstream disturbs most: the stream reducer has been reshaped
at least three times and most of their commit traffic lands in it. Two
structural choices follow.

1. It is built from **named steps**, each recording its own outcome. A scalar
   hit count cannot tell "everything landed" from "half of it silently
   drifted", which is precisely how this patch used to hide its own regressions.
2. Every rewrite is either an *insertion at a dispatch point* or a replacement
   of one grammar node. The reducer's arm bodies -- upstream's busiest surface,
   where 2.1.226 threaded a progress flag through five arms in a single release
   -- are never matched at all. The dispatch strings (``"thinking_delta"``,
   ``"message_stop"``) are the API's own vocabulary and do not churn.

Tolerating failure is not the same as ignoring it: the steps the feature cannot
live without are marked *required*, so `doctor` distinguishes "this build lacks
that shape" from "live thinking is dead".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .. import js
from ..js import Edit, Source
from .base import GROUP_OUTPUT, Options, Outcome, Patch

#: The two rewrites that *are* live thinking: one creates the virtual message
#: when a thinking block opens, the other appends each delta to it. Recognising
#: a reducer proves nothing on its own -- a build whose incidental rewrites
#: (threading the setter, clearing state on message_stop) landed while these two
#: drifted reports plenty of hits and streams nothing. Each is credited only
#: when its marker is found in the bundle the run produced, so the test is "did
#: the state update reach the bundle", not "did some matcher match".
_CORE_UPDATES = (
    ("block-start", "__cc_streamingThinkingMessage="),
    ("thinking-delta", "__cc_nextStreamingThinkingDelta"),
)


@dataclass(slots=True)
class Discovery:
    """Identifiers found in one step and consumed by later ones."""

    create_message_helper: str | None = None


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


# ------------------------------------------------------------ prop threading

#: What a conversation render is *made of*, whatever this build calls the
#: component or lists the props in. Membership, never order: upstream reorders
#: props freely and inserts new ones between them, and neither is a fact about a
#: set. Two regexes once differed only in the order two call sites listed the
#: same props, and 2.1.229 killed both at once by inserting
#: `onRateLimitAutoQueueContinue:` between two of them. The pair is also the
#: *whole* identity: it once had `agentDefinitions` beside it as witness and
#: insertion point, and 2.1.235 retired that prop from the bag -- an identity
#: resting on a neighbour reported no conversation renders on a build that
#: plainly drew four. The conversation is what the render is *for*; the props
#: that name it are the weakest claim that still proves it.
_CONVERSATION = ("conversationId", "messages")
_STREAMING = "streamingThinking"
_SETTER = "onStreamingThinking"
#: What the transcript renderer's signature *is*: the conversation it draws and
#: the streaming tool-uses the extras memo computes over. A third conjunct
#: (`showAllInTranscript`) stood here as find-anchor, identity and insertion
#: point in one -- the exact triple role `agentDefinitions` held in
#: `prop-threading` when 2.1.235 retired it -- and discriminates nothing on any
#: build in the corpus: the pair already names the same one renderer.
_TRANSCRIPT_SIGNATURE = ("messages", "streamingToolUses")
_INJECTED = "__cc_streamingThinking"


def _outermost(scope: js.Node) -> bool:
    """Is this the module wrapper -- the one scope not worth searching?

    The bundle is one enormous top-level function. Everything below it is a
    real lexical scope worth asking a question of; it is not, and walking its
    twelve million nodes to find out is the difference between a search that
    takes microseconds and one that does not finish.
    """
    return js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS) is None


def _conversation_renders(source: Source) -> list[js.Node]:
    """Every props bag that is a conversation render, as the object it is.

    The identity is one of the identity's own props -- a new property
    immediately before an existing one is valid in any object literal -- so
    nothing here computes where the bag *ends*, and the insertion point cannot
    be absent from a bag the identity admitted. Ending the literal used to be
    the whole difficulty: delimiting one in minified JS means telling a regex
    literal from division, and a hand-rolled scanner that guesses returns a
    `}` that is merely wrong.

    Being an ``object`` and not an ``object_pattern`` is what separates a prop
    being *passed* from one being *received*; inserting into the latter would
    rebind a local. The regex spelled that as a call-opening it had to match.
    A render bag is also *handed to* a component -- an argument -- which is
    what keeps a module-level literal, a return-value payload, or a config
    object that happens to carry the pair from being read as a render: those
    are the decoys the pair alone admits, and being an argument is a fact
    about what a render *is*, not about how this build spells one.
    """
    found = []
    for node in source.find(_CONVERSATION[0]):
        pair = js.named(node)
        if pair is None:
            continue
        bag = js.owner(pair)
        if bag is None or bag.type != "object":
            continue
        if bag.parent is None or bag.parent.type != "arguments":
            continue
        if js.carries(bag, *_CONVERSATION[1:]):
            found.append(bag)
    return found


def _bound_in_scope(site: js.Node, prop: str) -> str | None:
    """The local a scope binds this prop to, resolved from the site outwards.

    Which is how a name spliced into a site is a name that site can see, rather
    than the last one some other scope happened to bind under the same prop:
    the transcript renderer's own signature is what its own memo reads, and a
    second renderer carrying the same four props donated its local to the first
    one's memo when this was one variable discovered once and used everywhere.
    """
    scope = js.climb(site, lambda n: n.type in js.FUNCTIONS)
    while scope is not None:
        bound = js.parameters(scope).get(prop)
        if bound is not None:
            return js.text(bound)
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    return None


def _state_in_scope(site: js.Node) -> str | None:
    """The live-thinking state variable visible at this render, if any.

    Resolved from the site outwards, so what is threaded is a variable actually
    in scope where it is threaded -- the one thing the old matcher named as
    owed and could not discharge without a parse. It mattered: on 2.1.232 the
    two conversation renders sit in *different* top-level components, only one
    of which declares the state, and threading a single bundle-wide answer into
    both put an out-of-scope identifier into a shipped binary. It parses, so no
    gate could see it; it throws when that component renders.

    The state is identified by its setter: the scope that holds live thinking
    is the scope that hands ``onStreamingThinking`` to the reducer.

    Outwards is only half of lexical, and the half a subtree walk gets wrong:
    the declaration has to be one this render can *see* (:func:`js.visible`),
    which a nested function's is not and a block's own is not either. Threading
    one anyway produced a binary that parsed and threw -- the same failure as
    the bundle-wide answer this replaced, one door along -- and took the note
    that says a render was skipped with it. The setter is the other question and
    keeps the whole subtree: a scope may hand its setter to the reducer from
    inside a callback, and that says nothing about where the state lives.

    What the state is initialised to is not asked. ``useState(null)`` was the
    spelling on every build in the corpus and ``useState(void 0)`` is the same
    state, while the line below is the identity that matters: the pair whose
    second name is the setter this scope hands the reducer *is* the live
    thinking state, whatever produced it.
    """
    scope = js.climb(site, lambda n: n.type in js.FUNCTIONS)
    while scope is not None and not _outermost(scope):
        setters = {
            js.text(js.binding(value))
            for node in js.every(
                scope,
                lambda n: n.type == "property_identifier" and js.text(n) == _SETTER,
            )
            if (pair := js.named(node)) is not None
            and (value := pair.child_by_field_name("value")) is not None
        }
        for declarator in js.every(scope, js.of_type("variable_declarator")):
            name = declarator.child_by_field_name("name")
            if name is None or name.type != "array_pattern":
                continue
            if not js.visible(declarator, site):
                continue
            bound = [js.text(child) for child in name.named_children]
            if len(bound) == 2 and bound[1] in setters:
                return bound[0]
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    return None


def _step_prop_threading(source: Source, outcome: Outcome) -> Source:
    """Pass the live-thinking state into the renderers that need it."""
    step = outcome.step("prop-threading")
    edits = []
    renders = _conversation_renders(source)
    # Printed every run, green ones included: an early warning held back until
    # something breaks arrives too late to be one.
    step.note(f"{len(renders)} conversation render(s)")

    for bag in renders:
        state = _state_in_scope(bag)
        if state is None:
            step.note("a conversation render has no live-thinking state in scope")
            continue
        step.candidates += 1
        step.applied += 1
        edits.append(
            Edit.before(
                js.entry(js.props(bag)[_CONVERSATION[0]]), f"{_STREAMING}:{state},"
            )
        )
    return source.apply(edits)


# -------------------------------------------------------------- display mode

#: The env var *name* is the witness; whatever expression reads it is
#: upstream's to spell, and is never described here. Spelling such a path is
#: what killed `org-label` on 2.1.228, and this is the same variable one
#: migration behind.
_DISABLE_THINKING = "CLAUDE_CODE_DISABLE_THINKING"
_DISPLAY = "display"
_SUMMARIZED = '"summarized"'


def _defaulting(read: js.Node) -> js.Node:
    """The value a `display` read stands for -- itself, or the ``??`` around it.

    Two spellings of one value: a bare read, or a read upstream has already
    given a fallback of its own. The coalesce is a node the grammar names,
    where splitting the text on ``??`` was a claim about an operator free to
    appear anywhere else in the expression -- and the same split then rebuilt
    the replacement, so one stray ``??`` in front would have been silently
    dropped from what we wrote back.
    """
    parent = read.parent
    if parent is not None and parent.type == "binary_expression":
        operator = parent.child_by_field_name("operator")
        if (
            operator is not None
            and js.text(operator) == "??"
            and parent.child_by_field_name("left") == read
        ):
            return parent
    return read


def _chooses(node: js.Node) -> bool:
    """Is this the ``display`` read the request's thinking value is chosen from?

    The read is the identity and :func:`_defaulting` is what the arm is, so the
    property is a field of a member read rather than the tail of a spelling --
    ``.display`` at the end of some text says nothing about what precedes it,
    which is the half upstream regenerates every build.
    """
    if not js.reads(node, _DISPLAY):
        return False
    value = _defaulting(node)
    chosen = value.parent
    return (
        chosen is not None
        and chosen.type == "ternary_expression"
        and chosen.child_by_field_name("consequence") == value
        and (alternative := chosen.child_by_field_name("alternative")) is not None
        and js.text(alternative) == "void 0"
    )


def _step_display_mode(source: Source, outcome: Outcome) -> Source:
    """Default the thinking request to `summarized`.

    Without a display mode in the request the API streams signature-only (or
    late) thinking, so the live row starves -- worst on short thinks. Upstream
    only asks for summaries when the `showThinkingSummaries` setting is on;
    default it on instead.

    Two shapes used to be spelled out here -- an inline env check, and the
    2.1.216 form that hoists it into its own variable and gates the display
    behind extra feature-helper calls. They are one edit: the display value
    gains a default. Whatever guards reach it, and in whatever order the
    declaration lists them, is untouched because it is never matched.
    """
    step = outcome.step("display-mode")
    edits = []
    seen: set[int] = set()

    for node in source.find(_DISABLE_THINKING):
        declaration = js.up(node, "lexical_declaration", "variable_declaration")
        if declaration is None or declaration.start_byte in seen:
            continue
        # The value itself, not the ternary that chooses it: what is edited is
        # what identified it, so there is no second reach for the same child.
        display = js.first(declaration, _chooses)
        if display is None:
            continue
        seen.add(declaration.start_byte)
        value = _defaulting(display)
        step.candidates += 1
        step.applied += 1
        # Built from the read, so the default we write is a default *of that
        # read* whichever of the two shapes this build ships. An upstream that
        # already asks for summaries is the goal achieved, not a rewrite owed.
        summarized = f"{js.text(display)}??{_SUMMARIZED}"
        if js.text(value) == summarized:
            continue
        edits.append(Edit.replace(value, summarized))
    return source.apply(edits)


# ------------------------------------------------------------- final summary

_THINKING = '"thinking"'
_REDACTED = '"redacted_thinking"'
_TYPE = "type"
_SUMMARY_PROPS = ("thinking", "isStreaming", "streamingEndedAt")


def _typed(test: js.Node) -> js.Node | None:
    """The ``.type`` read a comparison asks about, whichever side it sits on.

    Both operands are nodes and the comparison is one, so which side upstream
    writes the label on says nothing about what is being tested -- the same
    rule `subagent-models` states for its bypass guard.
    """
    for side in (test.child_by_field_name("left"), test.child_by_field_name("right")):
        if js.reads(side, _TYPE):
            return side
    return None


def _is_thinking_test(node: js.Node) -> bool:
    """Is this the ``<block>.type==="thinking"`` comparison?

    Asked as text it was a claim about how the arrow *around* it closes
    (``.type==="thinking")``, closing paren included) -- the same mistake
    :func:`_wraps_block` records paying for one shape along, and one an added
    conjunct or a trailing argument would have been enough to break. The
    operator, the read and the label are each nodes; the code between them is
    upstream's.
    """
    if node.type != "binary_expression":
        return False
    operator = node.child_by_field_name("operator")
    if operator is None or js.text(operator) != "===":
        return False
    sides = (node.child_by_field_name("left"), node.child_by_field_name("right"))
    return _typed(node) is not None and any(
        side is not None and js.text(side) == _THINKING for side in sides
    )


def _widen(test: js.Node) -> str:
    """A thinking-block test, widened to accept redacted blocks.

    The second half is built from the *read* the first half compares, copied as
    the node it is. Splitting the test's text on its operator took whatever sat
    to the left of the last ``===``, which is the read only while upstream
    writes the label second: on ``"thinking"===b.type`` it produced
    ``"thinking"==="redacted_thinking"``, a widening that is always false.
    """
    return f"({js.text(test)}||{js.text(_typed(test))}==={_REDACTED})"


def _selects_thinking(scope: js.Node | None, block: str) -> js.Node | None:
    """The thinking test inside the `.find` that produced ``block``.

    One search where there were two: the declarator used to be identified by
    carrying this very test and then reached into again for it. What identifies
    a node and what gets edited are the same node, so there is no second reach
    to land somewhere else.
    """
    for declared in js.every(
        scope,
        lambda n: (
            n.type == "variable_declarator"
            and (name := n.child_by_field_name("name")) is not None
            and js.text(name) == block
        ),
    ):
        test = js.first(declared.child_by_field_name("value"), _is_thinking_test)
        if test is not None:
            return test
    return None


def _tests_thinking(scope: js.Node | None, block: str) -> js.Node | None:
    """The test that rejects a redacted block, asked of the block in hand."""
    return js.first(
        scope,
        lambda n: _is_thinking_test(n) and js.text(js.receiver(_typed(n))) == block,
    )


def _step_final_summary(source: Source, outcome: Outcome) -> Source:
    """Include redacted thinking in the final assistant-message summary."""
    step = outcome.step("final-summary")
    edits = []

    for node in source.find(_SUMMARY_PROPS[2]):
        summary = js.owner(node)
        if summary is None or not js.carries(summary, *_SUMMARY_PROPS):
            continue
        carried = js.props(summary)
        if js.text(carried["isStreaming"]) != "!1":
            continue
        # What the summary reads the text off, as the receiver of that read --
        # not the property sliced off the end of its own spelling.
        thinking = carried[_SUMMARY_PROPS[0]]
        if not js.reads(thinking, _SUMMARY_PROPS[0]):
            continue
        block = js.text(js.receiver(thinking))
        guard = js.up(summary, "if_statement")
        if guard is None:
            continue
        condition = guard.child_by_field_name("condition")
        scope = js.climb(guard, lambda n: n.type in js.FUNCTIONS)
        if condition is None or scope is None:
            continue
        # The two tests that reject a redacted block: the one inside the `.find`
        # that produced it, and the one this summary is guarded by. Each is a
        # node; none of the code between them is.
        predicate = _selects_thinking(scope, block)
        test = _tests_thinking(condition, block)
        if predicate is None or test is None:
            continue
        step.candidates += 1
        step.applied += 1
        edits += [
            Edit.replace(predicate, _widen(predicate)),
            Edit.replace(test, _widen(test)),
            Edit.replace(
                thinking,
                f'{block}.type==={_THINKING}?{block}.thinking:{block}.data??""',
            ),
        ]
    return source.apply(edits)


# -------------------------------------------------------- transcript signature


def _step_transcript_signature(source: Source, outcome: Outcome) -> Source:
    """Make sure the transcript renderer actually receives the live state.

    The insertion point is one of the identity's own names, so it cannot be
    absent from a pattern the identity admitted -- the guarantee
    ``prop-threading`` moved to after 2.1.235 retired its third conjunct.

    Deriving the renderer from its consumer instead -- the scope that binds
    what the streaming-extras memo computes over -- was measured and rejected:
    on every current build the memo's receiver is a react-compiler memoized
    *local* (``Te=useMemo(...)``), with real dataflow between it and the
    signature, and a dataflow pass is a tool this project deliberately does
    not build. Membership of the pair is the weakest claim the grammar can
    still prove.
    """
    step = outcome.step("transcript-signature")
    edits = []

    for node in source.find(_TRANSCRIPT_SIGNATURE[1]):
        pattern = js.owner(node)
        # A destructured parameter list, not a props bag being passed: this is
        # the renderer's own signature, and the same property names appear on
        # both sides of every call.
        if pattern is None or pattern.type != "object_pattern":
            continue
        if not js.carries(pattern, *_TRANSCRIPT_SIGNATURE):
            continue
        carried = js.props(pattern)
        step.candidates += 1
        step.applied += 1
        if _STREAMING in carried:
            # Upstream already threads it; nothing owed. What it bound the prop
            # to is that renderer's own business and is read back there.
            continue
        edits.append(
            Edit.before(
                js.entry(carried[_TRANSCRIPT_SIGNATURE[1]]),
                f"{_STREAMING}:{_INJECTED},",
            )
        )
    return source.apply(edits)


# ------------------------------------------------------------- inline extras

_CONTENT_BLOCK = "contentBlock"
_CONTENT = "content"
_UUID = "uuid"
_USE_MEMO = "useMemo"
_FLAT_MAP = "flatMap"


def _handed_only(call: js.Node, name: str) -> bool:
    """Is this call handed exactly ``[<name>]`` -- a one-element array of it?"""
    args = js.arguments(call)
    if len(args) != 1 or args[0].type != "array":
        return False
    elements = js.elements(args[0])
    return (
        len(elements) == 1
        and elements[0].type == "identifier"
        and js.text(elements[0]) == name
    )


def _wraps_block(call: js.Node | None) -> bool:
    """Is this a call wrapping one streaming content block as a message?

    The bag it is handed is the identity -- ``{content:[<entry>.contentBlock]}``
    -- asked as the object and the read it is. Asked as the text
    ``.contentBlock]}`` it was a claim about how the array *closes*, so one
    sibling property beside `content` read as the whole computation being gone
    and took live thinking with it.
    """
    arguments = js.arguments(call)
    if call is None or len(arguments) != 1 or arguments[0].type != "object":
        return False
    content = js.props(arguments[0]).get(_CONTENT)
    return any(js.reads(element, _CONTENT_BLOCK) for element in js.elements(content))


def _flatmap_extras(source: Source) -> tuple[js.Node, js.Node] | None:
    """The memo that turns streaming tool-uses into renderable messages.

    Identified by what it computes: a ``useMemo`` over a ``flatMap`` that
    builds one virtual message per streaming content block. Everything the
    replacement needs -- the helper that builds a message, the one that stamps
    its uuid, the one that normalises a list -- is read out of that computation
    rather than matched by name, because every one of those names is minified.

    Both nodes come back, the memo to replace and the ``flatMap`` that proved
    it was the right one, so the caller reads what was identified rather than
    reaching down for it a second time.
    """
    found: dict[int, tuple[js.Node, js.Node]] = {}
    for node in source.find(_CONTENT_BLOCK):
        memo = js.climb(
            node,
            lambda n: (
                n.type == "call_expression"
                and js.reads(n.child_by_field_name("function"), _USE_MEMO)
            ),
        )
        arrow = next(iter(js.arguments(memo)), None)
        if arrow is None or arrow.type != "arrow_function":
            continue
        flat = js.body(arrow)
        if (
            memo is not None
            and flat is not None
            and flat.type == "call_expression"
            and js.reads(flat.child_by_field_name("function"), _FLAT_MAP)
            and js.first(flat, _wraps_block) is not None
        ):
            found[memo.start_byte] = (memo, flat)
    return js.only(list(found.values()), "streaming-extras memos")


def _step_inline_extras(source: Source, found: Discovery, outcome: Outcome) -> Source:
    """Render live thinking inline, ordered with streaming tool-use blocks."""
    step = outcome.step("inline-extras")
    computed = _flatmap_extras(source)
    if computed is None:
        step.note("no streaming-extras memo in this build")
        return source

    memo, flat = computed
    # The state this memo can actually read: the prop *its own* scope was handed
    # -- which `transcript-signature` has just made sure of -- rather than a
    # variable some other renderer bound under the same name.
    var = _bound_in_scope(memo, _STREAMING)
    ns = js.text(js.receiver(memo.child_by_field_name("function")))
    tool_uses = js.text(js.receiver(flat.child_by_field_name("function")))
    callback = js.arguments(flat)[0]
    taken = js.positional(callback)
    if var is None or not taken:
        step.note("the streaming-extras memo is out of reach of the live state")
        return source
    entry = js.text(js.binding(taken[0]))
    block = js.body(callback)

    # What the callback builds, not what it declares first: an unrelated `let`
    # ahead of this one answered for it and the step raised on a number where a
    # call was expected.
    built = js.only(
        js.every(
            block,
            lambda n: (
                n.type == "variable_declarator"
                and _wraps_block(n.child_by_field_name("value"))
            ),
            scoped=True,
        ),
        "virtual-message builders in this callback",
    )
    if built is None:
        step.note("no virtual message built per streaming block")
        return source
    message = js.text(built.child_by_field_name("name"))
    helper = js.text(
        (built.child_by_field_name("value") or built).child_by_field_name("function")
    )
    stamp = js.first(
        block,
        lambda n: (
            n.type == "assignment_expression"
            and js.reads(n.child_by_field_name("left"), _UUID)
            and js.text(js.receiver(n.child_by_field_name("left"))) == message
        ),
        scoped=True,
    )
    # The call that turns the built message into a renderable list -- named by
    # what it is handed, since every helper name here is minified.
    # The call handed exactly `[<message>]` -- asked as the array it is (one
    # element, the built-message local), not as the text `[X]`, which asserted
    # the minifier put no space inside the brackets.
    normalize = js.first(
        block,
        lambda n: n.type == "call_expression" and _handed_only(n, message),
        scoped=True,
    )
    if stamp is None or normalize is None:
        step.note("the built message is not stamped and normalised as it was")
        return source
    uuid_helper = js.text(
        (stamp.child_by_field_name("right") or stamp).child_by_field_name("function")
    )
    normalize_fn = js.text(normalize.child_by_field_name("function"))

    found.create_message_helper = helper
    step.candidates += 1
    step.applied += 1
    return source.apply(
        [
            Edit.replace(
                memo,
                f"{ns}.useMemo(()=>{{"
                f"let __cc_streamingToolUseExtras={tool_uses}.map(({entry})=>{{"
                f"let {message}={helper}({{content:[{entry}.{_CONTENT_BLOCK}]}});"
                f"return {message}.{_UUID}={uuid_helper}({entry}.{_CONTENT_BLOCK}.id,0),"
                f"{{index:{entry}.index??9007199254740991,"
                f"messages:{normalize_fn}([{message}])}}}}),"
                f"__cc_streamingThinkingExtras=({var}?.messages??[])"
                f".map((__cc_entry,__cc_index)=>({{"
                f"index:__cc_entry.index??9007199254740991+__cc_index,"
                f"messages:{normalize_fn}([__cc_entry.message??__cc_entry])}}));"
                f"return[...__cc_streamingToolUseExtras,...__cc_streamingThinkingExtras]"
                f".sort((__cc_a,__cc_b)=>__cc_a.index===__cc_b.index?0:__cc_a.index-__cc_b.index)"
                f".flatMap((__cc_entry)=>__cc_entry.messages)}},[{tool_uses},{var}])",
            )
        ]
    )


# ------------------------------------------------------------------ reducer

_REQUEST_START = "stream_request_start"
_MESSAGE_STOP = "message_stop"
_CONTENT_BLOCK_START = "content_block_start"
_THINKING_DELTA = "thinking_delta"
_SET_STREAM_MODE = "onSetStreamMode"


def _reducer(source: Source) -> js.Node | None:
    """The stream reducer: the function that dispatches on all three events and
    is handed the stream callbacks to answer them with.

    Identified by the dispatch points it *has*, which is the same question its
    own arms answer and the one this patch goes on to edit. Asking it of the
    function's text instead -- ``'case"thinking_delta"' in spelled`` -- was the
    last place here that described syntax rather than reaching a node, and it
    described the busiest kind: which of the two spellings routes an event is
    upstream's to change, and :func:`_dispatch` already declines to care.

    Dispatching is asked of the scope itself, and the callbacks are asked for
    too, because *containing* three dispatches is not performing them: on
    2.1.210 the engine loop and `submitMessage` both hold the whole reducer
    somewhere inside them, and the right one was picked by nothing better than
    sitting earlier in the bundle. The options bag is what the reducer is *for*
    -- it is also where the setter gets threaded -- so it belongs to the
    identity rather than to a second search afterwards.
    """
    found = {}
    for node in source.find(f'"{_REQUEST_START}"'):
        handler = js.climb(node, lambda n: n.type in js.FUNCTIONS)
        if (
            handler is not None
            and _options_bag(handler) is not None
            and all(
                js.first(handler, _routing(label), scoped=True) is not None
                for label in (_REQUEST_START, _THINKING_DELTA, _CONTENT_BLOCK_START)
            )
        ):
            found[handler.start_byte] = handler
    return js.only(list(found.values()), "stream reducers")


def _options_bag(handler: js.Node) -> js.Node | None:
    """The reducer's options bag, destructured from one of its parameters.

    Visible to the reducer's whole body, because that is what a binding every
    arm reads has to be: a pattern inside a nested function -- or inside a
    block of its own -- takes the same parameter apart under a name the arms
    cannot see.
    """
    block = js.body(handler)
    if block is None:
        return None
    taken = {js.text(js.binding(p)) for p in js.positional(handler)}
    for declarator in js.every(block, js.of_type("variable_declarator")):
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or name.type != "object_pattern" or value is None:
            continue
        if not js.visible(declarator, block):
            continue
        if js.text(value) in taken and _SET_STREAM_MODE in js.props(name):
            return name
    return None


def _dispatches(node: js.Node, label: str) -> bool:
    """Is this the test by which the reducer recognises one stream event?

    The dispatch string is the API's own vocabulary and does not churn. The read
    that reaches it is upstream's to spell -- ``e.type``, ``e.event.type``, and
    ``e.event?.type`` the day someone adds a defensive ``?`` -- so the literal
    is what is matched and the path between is never described. Spelling the
    whole test out cost `message-stop` on exactly that one character: the live
    block was never marked finished and shimmered on after every turn, with the
    step reporting *absent* and the patch green.

    The operator is read, because the wrap runs the update on the side that
    dispatched: against a ``!==`` it would run on every event but this one.
    """
    if node.type != "binary_expression":
        return False
    operator = node.child_by_field_name("operator")
    if operator is None or js.text(operator) != "===":
        return False
    return any(
        side is not None and js.text(side) == f'"{label}"'
        for side in (
            node.child_by_field_name("left"),
            node.child_by_field_name("right"),
        )
    )


def _routing(label: str) -> Callable[[js.Node], bool]:
    """Predicate: this node routes that stream event.

    A factory rather than a closure spelled per call site, for the reason
    :func:`js.returns` is one: the question is asked of three events in one
    breath and of one event in another.
    """
    return lambda node: _routes(node, label)


def _routes(node: js.Node, label: str) -> bool:
    """Does this node route one stream event, whichever way this build routes it?

    Two spellings of one act: a ``switch`` arm labelled with the event, and the
    ``===`` test a build writes where it uses an ``if`` instead. Upstream uses
    both at once -- ``content_block_start`` is an arm and ``stream_request_start``
    a test, on every build we hold -- so which one carries a given event is not
    a fact about that event. :func:`_dispatch` inserts at the first and wraps the
    second; this is that choice asked as a question rather than performed as an
    edit, so a build that flips a spelling routes through the same code.
    """
    if node.type == "switch_case":
        value = node.child_by_field_name("value")
        return value is not None and js.text(value) == f'"{label}"'
    return _dispatches(node, label)


def _switch_on(case: js.Node, field: str) -> bool:
    """Does the ``switch`` holding this arm discriminate on ``.<field>.type``?

    The reducer switches over three stream unions -- the event type, the content
    block type, the delta type -- and a `case` label alone does not say which of
    them an arm belongs to: ``"thinking"`` is a content-block type, but a decoy
    arm of the same name in the *delta*-type switch drew the block-start update
    to itself, at 14/14 with `thinking-start` merely 1/1 -> 2/2. They are told
    apart by the authored name the discriminant reads ``.type`` off
    (``event`` / ``content_block`` / ``delta``), never by the minified receiver
    in front of it. Upstream folds side effects into the discriminant as a
    parenthesised comma sequence, whose *value* is its last element -- so
    unwrapping to the last child of each, once, reaches the read either way.
    """
    switch = js.up(case, "switch_statement")
    node = switch.child_by_field_name("value") if switch is not None else None
    while node is not None and node.type in (
        "parenthesized_expression",
        "sequence_expression",
    ):
        kids = js.children(node)
        node = kids[-1] if kids else None
    return (
        node is not None
        and js.reads(node, "type")
        and js.reads(js.receiver(node), field)
    )


def _dispatch(
    handler: js.Node,
    labels: tuple[str, ...],
    update: str,
    step: Outcome,
    *,
    on: str | None = None,
) -> list[Edit]:
    """Run ``update`` wherever the reducer dispatches one of these events -- in
    whichever spelling this build ships.

    A build routes a given event *either* as a ``case`` arm in a ``switch`` *or*
    as an ``===`` test written into an ``if``; upstream uses both across the
    reducer at once, and which one carries a given event is not a fact about that
    event. Binding each event to one of two functions made a spelling flip cost
    swapping them -- on 2.1.233 the ``message_stop`` ``if`` sits one line above
    the ``switch`` it would fold into, and folding it would send a required step
    to 0/0. Matching both spellings (:func:`_routes`) makes that flip cost
    nothing, which is the promise CONDUCT makes about a new spelling.

    The two spellings take the two edits they always did. A ``case`` arm gets the
    update inserted at its dispatch point, after the whole consecutive label
    chain (a fused ``case"a":case"b":`` arm once, split terminating arms once
    each), and only when its ``switch`` discriminates the ``on`` union -- so a
    decoy label in a sibling switch is not a dispatch point (:func:`_switch_on`).
    ``on=None`` is an event whose union is not a ``.<field>.type`` read the
    discriminant check can express (``stream_request_start`` routes on the
    reducer's own ``e.type``), so it is matched only as a test. An ``===`` test
    is wrapped ``(test&&((update),!0))``, value-preserving so it survives any
    surrounding composition and needs no union check (the label *is* the union).
    """
    points: set[int] = set()
    tests: list[js.Node] = []
    for node in js.every(handler, lambda n: any(_routes(n, label) for label in labels)):
        if node.type == "switch_case":
            if on is not None and _switch_on(node, on):
                points.add(js.dispatch(node))
        else:
            tests.append(node)
    step.candidates += len(points) + len(tests)
    step.applied += len(points) + len(tests)
    return [Edit.at(at, f"{update};") for at in sorted(points)] + [
        Edit.replace(test, f"({js.text(test)}&&(({update}),!0))") for test in tests
    ]


def _step_reducer(source: Source, found: Discovery, outcome: Outcome) -> Source:
    """Splice the live-thinking state updates into the stream reducer.

    Each dispatch point is its own named step, so a build that folds an arm
    away reads as that point's absence by name rather than as a bare count drop
    inside an aggregate.
    """
    step = outcome.step("reducer")
    helper = found.create_message_helper
    handler = _reducer(source)
    if handler is None or helper is None:
        if helper is None:
            # Without the helper the two marker steps can never land, so the
            # fixpoint always drops the patch: a resets-only rewrite ships
            # nothing.
            step.note("no virtual-message helper discovered; skipped")
        return source

    bag = _options_bag(handler)
    taken = js.positional(handler)
    if bag is None or not taken:
        return source
    step.candidates += 1
    step.applied += 1

    event = js.text(js.binding(taken[0]))
    setter = "__cc_onStreamingThinking"
    # Taken out of the very object the reducer already destructures, under
    # upstream's own name and into a name of ours. Whether upstream *also* binds
    # it is not a case to handle: two patterns naming one property both read
    # that property, so reusing their local name would buy a branch and nothing
    # else. Threaded at the *front* of the pattern, which is valid whatever the
    # pattern ends with -- a rest element or trailing comma there would make an
    # append a SyntaxError no write verifier could see.
    edits: list[Edit] = [Edit.before(bag.named_children[0], f"{_SETTER}:{setter},")]

    ended, cleared = _reset(setter, "Date.now()"), _reset(setter, "void 0")
    # The four resets are what *end* a live block, and every one of them is
    # required. Their absence is not a shape some builds lack -- it is a
    # shimmer that never stops, on a run that reports green off `reducer`
    # alone. That is `branding`'s undeclared badge again, in the one patch
    # whose failure mode is a UI that never settles rather than one that never
    # appears. The dispatch strings they anchor on are the API's own
    # vocabulary, so an arm going missing is news worth being told.
    #
    # `thinking-start` and `thinking-append` are deliberately not marked here:
    # `_CORE_UPDATES` already declares their witnesses, and credits those off
    # the bundle this run produced rather than off what a matcher reports about
    # itself -- the stronger of the two checks, and not worth stating twice.
    edits += _dispatch(
        handler,
        (_REQUEST_START,),
        f"{setter}?.(null)",
        outcome.step("request-start"),
    )
    edits += _dispatch(
        handler,
        (_MESSAGE_STOP,),
        ended,
        outcome.step("message-stop"),
        on="event",
    )
    edits += _dispatch(
        handler,
        ("text",),
        cleared,
        outcome.step("text-clear"),
        on="content_block",
    )
    edits += _dispatch(
        handler,
        ("message_delta",),
        cleared,
        outcome.step("message-delta-clear"),
        on="event",
    )
    edits += _dispatch(
        handler,
        ("thinking", "redacted_thinking"),
        _block_start(event, setter, helper),
        outcome.step("thinking-start"),
        on="content_block",
    )
    edits += _dispatch(
        handler,
        (_THINKING_DELTA,),
        _delta(event, setter, helper),
        outcome.step("thinking-append"),
        on="delta",
    )
    return source.apply(edits)


# ------------------------------------------------------------------ assembly


def _live_thinking(source: Source, _options: Options, outcome: Outcome) -> Source:
    found = Discovery()
    # Every step this patch can take, declared before anything runs: an
    # expectation that only comes into existence once its own rewrite succeeds
    # can never report that rewrite missing. The dispatch points are declared
    # here too, so a build whose reducer is gone fails *them* by name as well,
    # instead of them never having existed.
    outcome.declare(
        required=(
            "prop-threading",
            "display-mode",
            "transcript-signature",
            "inline-extras",
            "reducer",
            "request-start",
            "message-stop",
            "text-clear",
            "message-delta-clear",
            *(name for name, _marker in _CORE_UPDATES),
        ),
        optional=("final-summary", "thinking-start", "thinking-append"),
    )

    source = _step_prop_threading(source, outcome)
    source = _step_display_mode(source, outcome)
    source = _step_final_summary(source, outcome)
    source = _step_transcript_signature(source, outcome)
    source = _step_inline_extras(source, found, outcome)
    source = _step_reducer(source, found, outcome)

    # Credited off the bundle this run produced, never off a matcher: these two
    # updates *are* the feature, and every other step can land without them.
    for name, marker in _CORE_UPDATES:
        if source.count(marker):
            step = outcome.step(name)
            step.candidates += 1
            step.applied += 1
    return source


PATCHES = [
    Patch(
        id="live-thinking",
        title="Stream thinking live",
        summary="Show thinking as it is generated, inline and in order, instead of "
        "only after the turn finishes.",
        group=GROUP_OUTPUT,
        fn=_live_thinking,
        anchors=(
            f"{_SETTER}:",
            f'case"{_THINKING_DELTA}"',
            f'"{_REQUEST_START}"',
            _CONTENT_BLOCK_START,
            _CONVERSATION[0],
        ),
    ),
]
