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

    #: The virtual-message builder's local name in the *memo's* module -- what the
    #: memo rewrite uses, and the fallback the reducer uses when the two share a
    #: module (the monolith).
    create_message_helper: str | None = None
    #: The builder's external (exported) name when the memo imports it, so the
    #: reducer -- which may sit in a different module that names the same function
    #: differently -- can resolve *its own* local for it. ``None`` when the memo's
    #: helper is not an import (one module, one name).
    helper_export: str | None = None
    #: The blob index of the memo's module. When the helper is *not* imported
    #: (``helper_export`` is None) its local name only carries to a reducer in the
    #: *same* module; this is what lets the reducer confirm that rather than
    #: assume it, closing the mirror of the imported-name hazard.
    helper_module: int | None = None


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
#: The stream store's own name for the tool-use half of its snapshot -- what
#: its `_publish` calls write and what every consumer destructures. It is the
#: one name two identities rest on: the transcript renderer's signature, and
#: the snapshot pattern `_state_in_scope` proves the store's read by.
_TOOL_USES = "streamingToolUses"
#: What the transcript renderer's signature *is*: the conversation it draws and
#: the streaming tool-uses the extras memo computes over. A third conjunct
#: (`showAllInTranscript`) stood here as find-anchor, identity and insertion
#: point in one -- the exact triple role `agentDefinitions` held in
#: `prop-threading` when 2.1.235 retired it -- and discriminates nothing on any
#: build in the corpus: the pair already names the same one renderer.
_TRANSCRIPT_SIGNATURE = ("messages", _TOOL_USES)
#: Our local for the threaded state, wherever one has to be minted: the
#: renderer signatures `transcript-signature` extends, and the live wrapper's
#: own subscription on builds that keep the state in a store.
_INJECTED = "__cc_streamingThinking"
#: The store handle the live wrapper reads, one prop of ours beside upstream's.
_STORE_PROP = "__cc_stream"
#: The component a store-era render is rerouted through: the subscription the
#: build makes for tool uses, made once more for thinking.
_WRAPPER = "__cc_LiveConversation"


def _outermost(scope: js.Node) -> bool:
    """Is this the module wrapper -- the one scope not worth searching?

    A scope handing the reducer its callbacks, or a memo over the streaming
    tool-uses, lives inside a component; the module's whole-body wrapper never
    does, and walking its twelve million nodes to find that out is the
    difference between a search that takes microseconds and one that does not
    finish. So the climb stops at the wrapper -- but *which* scope is the wrapper
    is a fact about the build, not a fixed position.

    Pre-2.1.242 the bundle is one enormous top-level function: the module's
    ``program`` is a single ``(function(){...})()`` statement wrapping every
    line, and a top-level scope there *is* that wrapper. Since the code split,
    a module's ``program`` is dozens of statements -- imports, several
    components, exports -- and its top-level functions are real components, each
    a small part of a small module and exactly what the state resolution has to
    search. The signal that separates the two is structural and needs no size to
    guess at: a wrapper module is one (or, allowing a trailing statement, two)
    top-level statements; a component module is many. A scope nested inside a
    function is never the wrapper, whichever era it is.
    """
    if js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS) is not None:
        return False
    program = scope
    while program.parent is not None:
        program = program.parent
    statements = [child for child in program.children if child.type != "comment"]
    return len(statements) <= 2


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


def _bound_names(pattern: js.Node | None) -> set[str]:
    """Every name a binding pattern binds -- and none it merely references.

    A pattern's *keys* rename, its *defaults* are read, and only what lands on
    the binding side is a name the scope owns; walking every identifier in the
    subtree would claim both halves and refuse wraps over names nobody binds.
    """
    if pattern is None:
        return set()
    if pattern.type in ("identifier", "shorthand_property_identifier_pattern"):
        return {js.text(pattern)}
    if pattern.type == "pair_pattern":
        return _bound_names(pattern.child_by_field_name("value"))
    if pattern.type in ("assignment_pattern", "object_assignment_pattern"):
        return _bound_names(pattern.child_by_field_name("left"))
    if pattern.type in (
        "object_pattern",
        "array_pattern",
        "formal_parameters",
        "rest_pattern",
    ):
        found: set[str] = set()
        for child in js.children(pattern):
            found |= _bound_names(child)
        return found
    return set()


def _binds(scope: js.Node, name: str) -> bool:
    """Does this scope bind that spelling, by any binding form it has?

    One home for the question two callers ask -- :func:`_unshadowed` to refuse
    a wrap, :func:`_module_function` to stop a resolve -- because a binding
    form the list misses is a hole in both at once. The forms are the
    grammar's own: the scope's parameters, its declarators, and the
    declarations that bind by existing -- a function's name, a class's, a
    catch clause's parameter. The first version listed parameters, declarators
    and function declarations, and independent review demonstrated the gap the
    same day: a catch parameter and a class name are bindings too, and a
    wrapper re-spelling either would parse, verify, and throw at first render.
    """
    for holder in (
        scope.child_by_field_name("parameters"),
        scope.child_by_field_name("parameter"),
    ):
        if name in _bound_names(holder):
            return True
    for declarator in js.every(scope, js.of_type("variable_declarator"), scoped=True):
        if name in _bound_names(declarator.child_by_field_name("name")):
            return True
    for declaration in js.every(
        scope,
        js.of_type(
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
        ),
        scoped=True,
    ):
        bound = declaration.child_by_field_name("name")
        if bound is not None and js.text(bound) == name:
            return True
    for clause in js.every(scope, js.of_type("catch_clause"), scoped=True):
        if name in _bound_names(clause.child_by_field_name("parameter")):
            return True
    return False


def _module_function(source: Source, local: js.Node) -> js.Node | None:
    """The function this spelling means *at this use*, resolved lexically.

    A selector is usually hoisted (``function wv(sL){return
    sL.streamingToolUses}``) and its call site carries only the minified name,
    which is a spelling until its scope is said: in the monolith the same two
    letters bind functions in a hundred unrelated scopes. So the walk out from
    the use asks each scope in turn, and the innermost binding decides: a
    function bound there is the answer, and any *other* binding of the name
    blocks the resolve -- the engine would resolve the spelling to that
    binding, not to a function further out, and the first version skipped
    non-function bindings while claiming the engine's answer. Only when no
    enclosing scope binds the name at all does the single module-scope
    function answer from afar; anything still ambiguous answers ``None``.
    This is a predicate's resolver, not a locator: an unresolvable name here
    just means "not a provable selector", and the loudness belongs to the
    step whose production then fails to resolve.
    """
    name = js.text(local)
    owned: dict[int, js.Node] = {}
    module_scope: dict[int, js.Node] = {}
    for node in source.find_local(local, name):
        parent = node.parent
        if parent is None:
            continue
        fn = None
        if (
            parent.type == "function_declaration"
            and parent.child_by_field_name("name") == node
        ):
            fn = parent
        elif (
            parent.type == "variable_declarator"
            and parent.child_by_field_name("name") == node
            and (value := parent.child_by_field_name("value")) is not None
            and value.type in js.FUNCTIONS
        ):
            fn = value
        if fn is None:
            continue
        owner = js.climb(fn.parent, lambda n: n.type in js.FUNCTIONS)
        if owner is None or _outermost(owner):
            module_scope[fn.id] = fn
        else:
            owned[owner.id] = fn
    scope = js.climb(local, lambda n: n.type in js.FUNCTIONS)
    while scope is not None and not _outermost(scope):
        if scope.id in owned:
            return owned[scope.id]
        if _binds(scope, name):
            return None
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    found = list(module_scope.values())
    return found[0] if len(found) == 1 else None


def _selects(source: Source, argument: js.Node, field: str) -> bool:
    """Is this argument a *selector* for that snapshot field?

    A selector is a function answering the field read off its own parameter --
    2.1.247's ``function wv(sL){return sL.streamingToolUses}``, handed to the
    store hook beside the store. The field name is the identity, in the one
    grammar position a per-field read gives it; whether the function is inline
    or hoisted behind a name is spelling (:func:`_module_function`), and the
    answer routes through :func:`js.values` like every other answer here.
    """
    fn = argument
    if argument.type == "identifier":
        resolved = _module_function(source, argument)
        if resolved is None:
            return False
        fn = resolved
    if fn.type not in js.FUNCTIONS:
        return False
    taken = [js.binding(parameter) for parameter in js.positional(fn)]
    if len(taken) != 1:
        return False
    param = js.text(taken[0])
    block = js.body(fn)
    if block is None:
        return False
    answers = (
        [block]
        if block.type != "statement_block"
        else [
            expression
            for statement in js.every(fn, js.of_type("return_statement"), scoped=True)
            for expression in js.children(statement)
        ]
    )
    return any(
        js.reads(value, field) and js.text(js.receiver(value)) == param
        for answer in answers
        for value in js.values(answer)
    )


@dataclass(slots=True)
class _PairState:
    """The component-owned state (through 2.1.235): thread its value."""

    state: str


@dataclass(slots=True)
class _StoreRead:
    """A store-era production: wrap the render with a subscription through it.

    ``selected`` records whether the spelling itself demonstrated the hook
    taking a selector -- what licenses :func:`_wrap` to skip its arity check.
    """

    production: js.Node
    store: js.Node
    selected: bool


def _state_in_scope(source: Source, site: js.Node) -> _PairState | _StoreRead | None:
    """The live-thinking state at this render: how this build's own scope
    reaches the live stream data, resolved from the site outwards.

    Outwards, so what is spliced is valid where it is spliced: on 2.1.232 the
    two conversation renders sit in *different* top-level components, only one
    of which declares the state, and a single bundle-wide answer put an
    out-of-scope identifier into a shipped binary -- it parses, so no gate
    could see it, and it throws when that component renders. A binding used
    here has to be one this render can *see* (:func:`js.visible`).

    The state has two semantic homes, and each is identified by the only name
    it has:

    - **The component's own pair** -- every build through 2.1.235. Nothing
      names the pair itself (``useState(null)`` and ``useState(void 0)`` are
      the same state, so the initialiser is never asked); what names it is its
      *setter*, handed to the reducer as ``onStreamingThinking:`` in this very
      scope. The state is the array pattern whose second binding is a handed
      setter, and the handing is asked of the whole subtree on purpose -- a
      scope may hand it from inside a callback, and that says nothing about
      where the state lives. Answered as a :class:`_PairState`: the state is
      a binding of this scope, and threading its value is the whole rewrite.
    - **The stream store** -- 2.1.236 on. Here the question this function used
      to ask -- *which idiom holds the state* -- is retired, because it broke
      on every answer it ever gave: the whole-snapshot destructure it knew
      (``{streamingToolUses:…}=useX(<store>)``, 2.1.236-2.1.246) was retired
      by 2.1.247's per-field selector reads (``et(<store>,wv)``), the handing
      it once rested on moved into the engine on 2.1.246, and each repair
      bought exactly one build. Those are spellings of React's data-access
      fashion, the busiest surface upstream owns, and a matcher enumerating
      them is a whitelist against a fashion. What survives every spelling is
      the **twin**: live tool-uses ride the same store, and that feature ships
      working, so somewhere in this scope upstream reads ``streamingToolUses``
      off the store -- as a snapshot pattern's own key, or through a selector
      handed to the hook beside the store. That read is the **production**,
      answered as a :class:`_StoreRead`: the hook and the store, upstream's
      own, read out of whatever spelling this build uses and reused verbatim
      by :func:`_wrap` -- the same
      reuse-their-expression rule `org-label` and the extras memo follow,
      because what is copied cannot drift from what it was copied from.

    A production is a declarator this site can see whose value is one call
    among its possible values (:func:`js.values` -- 2.1.247 arrived with a
    ``??`` fallback on the read, and the exact-node question would have read
    it as no call at all), taking the store alone (the destructure spelling:
    the pattern names the field as its own key, sole argument since a second
    would make the pattern something other than the snapshot) or the store
    beside exactly one selector for the field (the selector spelling). One
    answer or none per scope (:func:`js.only`): two reads of the store is a
    cardinality change to be told about, not a first to win.
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
        productions: dict[int, _StoreRead] = {}
        for declarator in js.every(scope, js.of_type("variable_declarator")):
            name = declarator.child_by_field_name("name")
            if name is None or not js.visible(declarator, site):
                continue
            if name.type == "array_pattern":
                bound = [js.text(child) for child in name.named_children]
                if len(bound) == 2 and bound[1] in setters:
                    return _PairState(bound[0])
                continue
            calls = [
                v
                for v in js.values(declarator.child_by_field_name("value"))
                if v.type == "call_expression"
            ]
            if len(calls) != 1:
                continue
            production = calls[0]
            args = js.arguments(production)
            if name.type == "object_pattern" and js.children(name):
                if _TOOL_USES not in js.props(name) or len(args) != 1:
                    continue
                productions[declarator.id] = _StoreRead(production, args[0], False)
            elif name.type == "identifier" and len(args) == 2:
                chosen = [a for a in args if _selects(source, a, _TOOL_USES)]
                if len(chosen) == 1:
                    store = args[0] if chosen[0] == args[1] else args[1]
                    productions[declarator.id] = _StoreRead(production, store, True)
        found = js.only(list(productions.values()), "reads of the live stream store")
        if found is not None:
            return found
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    return None


def _module_statement(node: js.Node) -> js.Node:
    """The module-scope statement this node is part of -- where a declaration
    meant for the whole module goes.

    Module scope is a fact about the build, not a fixed depth: a split
    module's ``program`` is the scope, and the monolith's is the wrapper
    IIFE's own body (:func:`_outermost` -- inserting before the IIFE itself
    would put the declaration outside every binding it needs).
    """
    statement = node
    while statement.parent is not None:
        parent = statement.parent
        if parent.type == "program":
            return statement
        if parent.type == "statement_block":
            owner = js.climb(parent, lambda n: n.type in js.FUNCTIONS)
            if owner is not None and _outermost(owner):
                return statement
        statement = parent
    return statement


def _root(expression: js.Node) -> js.Node | None:
    """The identifier an expression resolves through -- ``X`` in
    ``X.createElement`` -- or nothing when there is no lone name to check."""
    node: js.Node | None = expression
    while node is not None and node.type in (
        "member_expression",
        "subscript_expression",
        "parenthesized_expression",
    ):
        node = node.child_by_field_name("object") or next(iter(js.children(node)), None)
    return node if node is not None and node.type == "identifier" else None


def _unshadowed(name: str, site: js.Node, *, above: js.Node | None = None) -> bool:
    """Does this spelling keep its meaning from the site up to ``above``?

    The live wrapper is a module-scope function that re-spells names it read
    off the render -- the JSX callee, the component, the hook -- and a
    component-scope binding of the same spelling between the two would make
    the wrapper name something else entirely: it parses, verifies, and throws
    when the wrapper first renders, the 2.1.232 class of damage. So the name
    is walked from the site outwards, and any scope that binds it
    (:func:`_binds`, every binding form at once) refuses the wrap loudly
    rather than shipping it. With no ``above`` the walk runs to module scope,
    for a name the wrapper re-spells there; the store expression's spellings
    name ``above`` instead -- the scope upstream wrote them in -- because a
    spelling copied *down* into the bag only has to mean at the bag what it
    meant where it was copied from.
    """
    scope = js.climb(site, lambda n: n.type in js.FUNCTIONS)
    while scope is not None and not _outermost(scope):
        if above is not None and scope.id == above.id:
            return True
        if _binds(scope, name):
            return False
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    return True


def _wrap(
    source: Source, bag: js.Node, resolved: _StoreRead, serial: int
) -> list[Edit] | str:
    """Reroute this render through a component of ours that subscribes itself.

    Threading the state's *value* into the props bag was sound while the
    resolved render was live-computed, and 2.1.247 retired that render: the
    surviving one is react-compiler cached behind a fixed slot-test chain
    (``if(Yr[15]!==ph||…)Du=r(Ih,{…})``), and a prop the compiler never saw is
    a prop no slot tests -- the element is reused, the child bails out on
    identity, and a perfectly threaded value renders exactly once. The
    compiler's cache is a representation this patch refuses to model; what
    needs no modelling is React's own contract that a component re-renders
    from its own subscription. So the render is rerouted through a wrapper
    that makes the subscription this scope stopped making: it reads the store
    through the *production's own hook* -- whose optional selector parameter
    is upstream's since the store era began (2.1.236's hook and 2.1.247's are
    the same two-parameter function, measured) -- with a selector of ours in
    upstream's own per-field shape, and hands the component the state under
    the prop every other step already speaks. The transcript renderer's own
    memo comparator compares unknown props by identity (and has since
    2.1.210), so a fresh value re-renders it and a quiet one does not, cached
    parent or not.

    Everything the wrapper spells is read off the site -- the JSX callee, the
    component, the hook -- so the one thing owed is that those names mean at
    module scope what they mean at the render (:func:`_unshadowed`); a
    shadowed name refuses the wrap loudly, and the answer is the *reason*, so
    the step's note names what refused rather than one sentence covering
    three facts. The store expression is not module-scope and never has to
    be: it rides into the props bag (``__cc_stream:``), evaluated exactly
    where the old threading evaluated its state, its spellings walked the
    same way but bounded at the scope upstream wrote them in, and the bag
    being cached is harmless for it -- the store's identity is as fresh as
    the conversation the cache already tests. A bag that is not a JSX props
    argument (the render call's second argument) is refused the same way:
    rerouting a call this step misread would be rubble, not a wrap.

    Where the destructure spelling leaves the selector undemonstrated, an
    in-module hook is held to it -- resolved and required to declare a second
    parameter -- because a hook that ignores the argument would thread the
    whole snapshot as the state with every count green, the one silent
    failure this rewrite could otherwise add. A hook defined elsewhere is
    accepted on the measurement above: following the import hop would mean
    cross-module function resolution built for a build that has never
    shipped, and that residue -- a future destructure build importing a
    selectorless hook -- is accepted here by name rather than guessed at.
    """
    handed = bag.parent
    render = handed.parent if handed is not None else None
    if render is None:
        return "the conversation bag is not a component call's props"
    args = js.arguments(render)
    if len(args) < 2 or args[1] != bag:
        return "the conversation bag is not a component call's props"
    component = args[0]
    jsx = render.child_by_field_name("function")
    hook = resolved.production.child_by_field_name("function")
    if jsx is None or hook is None:
        return "the conversation bag is not a component call's props"
    for expression in (jsx, component, hook):
        root = _root(expression)
        if root is None or not _unshadowed(js.text(root), bag):
            return "a name the live wrapper needs is not the render's to use"
    origin = js.climb(resolved.production, lambda n: n.type in js.FUNCTIONS)
    references = {
        js.text(node) for node in js.every(resolved.store, js.of_type("identifier"))
    }
    if resolved.store.type == "identifier":
        references.add(js.text(resolved.store))
    if origin is not None and any(
        not _unshadowed(reference, bag, above=origin) for reference in references
    ):
        return "the store's spelling does not reach the render"
    if not resolved.selected and hook.type == "identifier":
        defined = _module_function(source, hook)
        if defined is not None and len(js.positional(defined)) < 2:
            return "the store hook takes no selector on this build"
    name = _WRAPPER if serial == 0 else f"{_WRAPPER}{serial + 1}"
    declaration = (
        f"function {name}(__cc_props){{"
        f"let {_INJECTED}={js.text(hook)}(__cc_props.{_STORE_PROP},"
        f"(__cc_snapshot)=>__cc_snapshot.{_STREAMING});"
        f"return {js.text(jsx)}({js.text(component)},"
        f"{{...__cc_props,{_STREAMING}:{_INJECTED}}})}}"
    )
    return [
        Edit.before(_module_statement(render), declaration),
        Edit.replace(component, name),
        Edit.before(
            js.entry(js.props(bag)[_CONVERSATION[0]]),
            f"{_STORE_PROP}:{js.text(resolved.store)},",
        ),
    ]


def _step_prop_threading(source: Source, outcome: Outcome) -> Source:
    """Pass the live-thinking state into the renderers that need it."""
    step = outcome.step("prop-threading")
    edits: list[Edit] = []
    renders = _conversation_renders(source)
    # The durable witness behind a store read: the field the wrapper selects
    # is one the bundle's own objects still name (the store's snapshot
    # initialiser and its publish call -- `streamingThinking:null`, upstream's
    # own since 2.1.236). A store that renames the field would leave the
    # wrapper subscribing to `undefined` with every count green -- the same
    # hole `thinking-summaries` pays a header-name count for.
    field_named = any(
        (pair := js.named(node)) is not None and pair.type == "pair"
        for node in source.find(_STREAMING)
    )
    wrapped = 0
    unreached = 0
    for bag in renders:
        resolved = _state_in_scope(source, bag)
        if resolved is None:
            # By design on every build we hold: the resume view, the transcript
            # overlay and the message picker draw conversations too, in scopes
            # where nothing ever streams. Counted rather than worth a sentence
            # each -- the note below carries the number, and the number moving
            # is the signal (a render newly skipped was the only sign of the
            # 2.1.232 out-of-scope threading).
            unreached += 1
            continue
        carried = js.props(bag)
        if _STREAMING in carried:
            # Upstream already threads it into this render; the goal achieved.
            step.candidates += 1
            step.applied += 1
            continue
        if isinstance(resolved, _PairState):
            step.candidates += 1
            step.applied += 1
            edits.append(
                Edit.before(
                    js.entry(carried[_CONVERSATION[0]]),
                    f"{_STREAMING}:{resolved.state},",
                )
            )
            continue
        if not field_named:
            step.note(f"the stream store no longer names a {_STREAMING} field")
            continue
        step.candidates += 1
        wrap = _wrap(source, bag, resolved, wrapped)
        if isinstance(wrap, str):
            step.note(wrap)
            continue
        step.applied += 1
        wrapped += 1
        edits += wrap
    # Printed every run, green ones included: an early warning held back until
    # something breaks arrives too late to be one.
    step.note(
        f"{len(renders)} conversation render(s)"
        + (f", {unreached} with no live-thinking state in scope" if unreached else "")
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
        if declaration is None or declaration.id in seen:
            continue
        # The value itself, not the ternary that chooses it: what is edited is
        # what identified it, so there is no second reach for the same child.
        display = js.first(declaration, _chooses)
        if display is None:
            continue
        seen.add(declaration.id)
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
        # The block's home is the function that produced it -- the one holding
        # the `.find` whose predicate selects thinking -- resolved from the
        # summary outwards, since the summary itself sits inside the callback
        # that hands it over.
        scope = js.climb(summary, lambda n: n.type in js.FUNCTIONS)
        predicate = None
        while scope is not None and not _outermost(scope):
            if (predicate := _selects_thinking(scope, block)) is not None:
                break
            scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
        if predicate is None or scope is None:
            continue
        # Which `if` guards the summary is answered by what its condition
        # tests, never by being the nearest: 2.1.236 nested an unrelated gate
        # between the thinking test and the summary it guards, and the nearest
        # `if` read a shape that had merely moved as one that was gone. The
        # climb stays inside the block's own home, because its name is only a
        # spelling until the scope it belongs to is said.
        guard = js.up(summary, "if_statement")
        test = None
        while (
            guard is not None
            and guard.start_byte >= scope.start_byte
            and (test := _tests_thinking(guard.child_by_field_name("condition"), block))
            is None
        ):
            guard = js.up(guard.parent, "if_statement")
        if test is None:
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
    and took live thinking with it. The element is asked by membership among
    its possible values (:func:`js.values`), never asked to *be* the read:
    2.1.246 minted stable ids for streamed blocks and the element became
    ``ce?{...S.contentBlock,id:V}:S.contentBlock`` -- the same wrap behind an
    id choice, which the exact-node question read as no wrap at all and took
    nine required steps to zero, with every anchor count standing.
    """
    arguments = js.arguments(call)
    if call is None or len(arguments) != 1 or arguments[0].type != "object":
        return False
    content = js.props(arguments[0]).get(_CONTENT)
    return any(
        js.reads(value, _CONTENT_BLOCK)
        for element in js.elements(content)
        for value in js.values(element)
    )


def _flatmap_extras(source: Source) -> tuple[js.Node, js.Node] | None:
    """The memo that turns streaming tool-uses into renderable messages.

    Identified by what it computes: a memo hook over a ``flatMap`` that builds
    one virtual message per streaming content block. Everything the replacement
    needs -- the memo's own callee, the array it maps, the callback it runs,
    the builder the reducer's own splices will need, the normaliser the
    thinking half re-spells -- is read out of that computation rather than
    matched by name, because every one of those names is minified. The memo hook is one of them: the monolith
    reaches it as ``<React>.useMemo(...)`` (the property name survives), but a
    code-split module imports it under a local (``import{useMemo as te}`` ->
    ``te(...)``), so keying on ``useMemo`` read a build that memoizes exactly the
    same way as one with no memo at all. The computation is the identity -- an
    arrow whose whole body is a ``flatMap`` wrapping the block -- and the call
    that takes that arrow is the memo, however it is spelled.

    Both nodes come back, the memo to replace and the ``flatMap`` that proved
    it was the right one, so the caller reads what was identified rather than
    reaching down for it a second time.
    """
    found: dict[int, tuple[js.Node, js.Node]] = {}
    for node in source.find(_CONTENT_BLOCK):
        flat = js.climb(
            node,
            lambda n: (
                n.type == "call_expression"
                and js.reads(n.child_by_field_name("function"), _FLAT_MAP)
            ),
        )
        if flat is None or js.first(flat, _wraps_block) is None:
            continue
        # The arrow whose *whole body* is that flatMap, and the call that takes
        # it -- the memo. An expression-bodied arrow, so the flatMap is the body
        # itself, not a statement inside one; the callback the flatMap runs is a
        # different arrow, nested below the flatMap and never climbed into.
        arrow = js.up(flat, "arrow_function")
        body = js.body(arrow) if arrow is not None else None
        if body is None or body.id != flat.id:
            continue
        memo = js.up(arrow, "call_expression")
        if memo is None or next(iter(js.arguments(memo)), None) != arrow:
            continue
        found[memo.id] = (memo, flat)
    return js.only(list(found.values()), "streaming-extras memos")


# ---- cross-module name resolution for the one name two modules share ----
#
# The virtual-message builder is discovered in the memo's module and used again
# in the reducer's, and since 2.1.242 those are different modules that alias the
# same imported function under different locals. `_flatmap_extras`'s `Ah` is a
# regex-escape helper in the reducer's chunk; the builder is `xg` there. So the
# name is resolved on the reducer's side through the one export hop between them,
# never carried across as a spelling -- the same lesson `find_local` states for a
# module-local, reaching an *inter*-module name. In the monolith the two share a
# scope and there is no import to follow, which `_imported_name` reports as None.


def _imported_name(source: Source, local: js.Node) -> str | None:
    """The external name a local was imported under, in the local's own module.

    ``import{X as Ah}`` binds ``Ah`` to the external name ``X`` -- how any other
    module names the same value. A local that is not imported (defined in this
    module, or the monolith's single scope) returns ``None``: its own name is the
    only name there is.
    """
    name = js.text(local)
    for node in source.find_local(local, name):
        specifier = js.up(node, "import_specifier")
        if specifier is None:
            continue
        alias = specifier.child_by_field_name("alias")
        bound = alias if alias is not None else specifier.child_by_field_name("name")
        if bound is not None and js.text(bound) == name:
            external = specifier.child_by_field_name("name")
            return js.text(external) if external is not None else None
    return None


def _module_local(source: Source, anchor: js.Node, external: str) -> str | None:
    """This module's own local for an external name -- the reducer's ``xg``.

    A module names an imported value in one of two ways, and both are read as the
    grammar's own specifiers rather than spelled: it *defines and exports* the
    value (``export{xg as ZNa}`` -- the local is the export's ``name``), or it
    *imports* it (``import{ZNa as x}`` -- the local is the import's ``alias``).
    ``None`` when the module neither defines nor imports it, which fails the
    reducer step loudly rather than splicing a call to a name that is not there.
    """
    locals_found = []
    for node in source.find_local(anchor, external):
        export = js.up(node, "export_specifier")
        if export is not None:
            exported = export.child_by_field_name(
                "alias"
            ) or export.child_by_field_name("name")
            bound = export.child_by_field_name("name")
            if (
                exported is not None
                and bound is not None
                and js.text(exported) == external
            ):
                locals_found.append(js.text(bound))
            continue
        imp = js.up(node, "import_specifier")
        if imp is not None:
            imported = imp.child_by_field_name("name")
            if imported is not None and js.text(imported) == external:
                bound = imp.child_by_field_name("alias") or imported
                locals_found.append(js.text(bound))
    return js.only(
        list(dict.fromkeys(locals_found)), f"module-local bindings of {external!r}"
    )


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
    # The memo's own callee, reused verbatim: `<React>.useMemo` in the monolith,
    # a bare imported local (`te`) in a code-split module. Reading the receiver
    # and re-spelling `.useMemo` assumed a member call and crashed on the local;
    # copying the callee keeps the rewrite memoized the way this build memoizes.
    memo_fn = js.text(memo.child_by_field_name("function"))
    tool_uses = js.text(js.receiver(flat.child_by_field_name("function")))
    callback = js.arguments(flat)[0]
    if var is None:
        step.note("the streaming-extras memo is out of reach of the live state")
        return source
    block = js.body(callback)

    # What the callback builds, not what it declares first: an unrelated `let`
    # ahead of this one answered for it and the step raised on a number where a
    # call was expected. The rewrite below never re-spells this call -- the
    # callback carries it -- but the reducer's own splices build virtual
    # messages with the same helper, and this is where it is discovered.
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
    helper_node = (built.child_by_field_name("value") or built).child_by_field_name(
        "function"
    )
    if helper_node is None:
        step.note("the virtual-message builder call has no callee")
        return source
    helper = js.text(helper_node)
    # The call that turns the built message into a renderable list -- named by
    # what it is handed, since every helper name here is minified.
    # The call handed exactly `[<message>]` -- asked as the array it is (one
    # element, the built-message local), not as the text `[X]`, which asserted
    # the minifier put no space inside the brackets. The thinking half below
    # re-spells this one call, so it is the one name still read out.
    normalize = js.first(
        block,
        lambda n: n.type == "call_expression" and _handed_only(n, message),
        scoped=True,
    )
    if normalize is None:
        step.note("the built message is not normalised as it was")
        return source
    normalize_fn = js.text(normalize.child_by_field_name("function"))

    found.create_message_helper = helper
    found.helper_export = _imported_name(source, helper_node)
    found.helper_module = source.module_index(helper_node)
    step.candidates += 1
    step.applied += 1
    # The per-entry computation is upstream's callback *reused verbatim* --
    # invoked with the same three arguments `flatMap` hands it -- never
    # rebuilt from the names discovered above: the rebuilt copy spelled the
    # uuid stamp as it stood, and 2.1.246 grew id-minting inside the callback
    # (`{id,minted}` per block, the stamp now a choice), which a re-spell
    # would have silently shed. What upstream does per entry is upstream's;
    # this memo only owes the interleave. The dependencies are reused the
    # same way, each spread rather than transcribed -- an array literal and a
    # hoisted variable spread alike -- with the live state appended so the
    # memo recomputes as thinking streams.
    wrap = js.text(callback)
    deps = "".join(f"...{js.text(argument)}," for argument in js.arguments(memo)[1:])
    return source.apply(
        [
            Edit.replace(
                memo,
                f"{memo_fn}(()=>{{"
                f"let __cc_streamingToolUseExtras={tool_uses}.map("
                f"(__cc_entry,__cc_index,__cc_entries)=>({{"
                f"index:__cc_entry.index??9007199254740991,"
                f"messages:({wrap})(__cc_entry,__cc_index,__cc_entries)}})),"
                f"__cc_streamingThinkingExtras=({var}?.messages??[])"
                f".map((__cc_entry,__cc_index)=>({{"
                f"index:__cc_entry.index??9007199254740991+__cc_index,"
                f"messages:{normalize_fn}([__cc_entry.message??__cc_entry])}}));"
                f"return[...__cc_streamingToolUseExtras,...__cc_streamingThinkingExtras]"
                f".sort((__cc_a,__cc_b)=>__cc_a.index===__cc_b.index?0:__cc_a.index-__cc_b.index)"
                f".flatMap((__cc_entry)=>__cc_entry.messages)}},[{deps}{var}])",
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
            found[handler.id] = handler
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
    # Each dispatch point is an offset with the arm it belongs to, so the
    # insertion routes to the arm's own module (`Edit.at`'s `within`); the arm is
    # the natural anchor since the reducer, arm and update are all one module.
    points: dict[int, js.Node] = {}
    tests: list[js.Node] = []
    for node in js.every(handler, lambda n: any(_routes(n, label) for label in labels)):
        if node.type == "switch_case":
            if on is not None and _switch_on(node, on):
                points[js.dispatch(node)] = node
        else:
            tests.append(node)
    step.candidates += len(points) + len(tests)
    step.applied += len(points) + len(tests)
    return [Edit.at(at, f"{update};", within=points[at]) for at in sorted(points)] + [
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

    # The builder's name *in the reducer's module*. When the memo imports it
    # (every split build), the same function is a different local here -- the
    # memo's `Ah` is a regex helper in the reducer's chunk, which builds the
    # message as `xg` -- so it is resolved through the export the two modules
    # share, never carried across as a spelling. A build where the reducer's
    # module cannot reach the builder fails this required step loudly rather than
    # splicing a call to a name that resolves to something else.
    if found.helper_export is not None:
        helper = _module_local(source, handler, found.helper_export)
        if helper is None:
            step.note(
                "the virtual-message builder is not reachable in the reducer's "
                "module; skipped"
            )
            return source
    elif found.helper_module is not None and found.helper_module != source.module_index(
        handler
    ):
        # The helper is a local the memo *defines*, not one it imports, so its
        # spelling only carries to a reducer in that same module. Here they are
        # different modules (the monolith is one, so this never fires there), and
        # a memo-module local would resolve to something else in the reducer's --
        # the mirror of the imported-name hazard, closed the same loud way.
        step.note("the virtual-message builder is a local of another module; skipped")
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
