# Patch playbook

For maintainers. How the patches are built to survive Claude updates, and how to
repair one when a build breaks it.

This is a Python port of
[a-connoisseur/patch-claude-code](https://github.com/a-connoisseur/patch-claude-code);
that repo's git history is the best archive of how each matcher has drifted over
time.

## The rule

The minified bundle is regenerated on every Claude build, so identifiers churn
constantly. Two things survive that: the **names upstream's authors wrote**, and
the **grammar**. So there is one rule, and everything below is it applied:

> **Find by the name upstream wrote. Edit the grammar node. Never describe the
> syntax in between.**

The bundle is parsed (`src/patch_cc/js.py`), and the parse is what locates. A
patch scans for an authored name — a property, a `case` label, an API string, a
setting — takes the node that name *is*, climbs or descends to the node the edit
belongs to, and replaces that node:

```python
for node in source.find("latestThinkingSummary"):
    arm = js.climb(node, ...)          # the branch it identifies
    edits.append(Edit.replace(...))    # the node the edit belongs to
```

Everything a minifier regenerates lives strictly between those two: local names,
statement order, comma-fusion versus separate statements, braces around a single
statement, whether a helper was extracted, which JSX runtime is emitted. A tree
makes all of it invisible instead of obligatory.

The name is the **whole node**, never part of one — `agentType` is not
`subagentType` and `spinnerTipsEnabled` is not `spinnerTipsEnabledAt` — because
no later check can tell the difference: the longer name is a
`property_identifier` too, and a read of it rewritten as a read of this one
left `!1>0`, which counted, verified and parsed. Prose that *contains* a name
is the other question, and has its own search (`source.literals`): a product
name inside a sentence has no node of its own.

### The primitives

Each one retires a rule this playbook used to have to state, because the grammar
now answers what a regex had to assert.

| primitive | the question it answers | what it retired |
|---|---|---|
| `source.find(name)` | where is this authored name? | substring anchors — the name is the *whole* node, so `agentType` is not `subagentType` and `spinnerTipsEnabled` is not `spinnerTipsEnabledAt` |
| `source.literals(text)` | which strings say this? | the climb from a byte hit to the literal around it; prose is asked what it says, once per literal |
| `props(node)` | which properties does this object carry? | prop **order** and adjacency; `object` vs `object_pattern` (passed vs received) |
| `parameters(fn)` / `positional(fn)` | what does this function take? | destructuring-head matchers, and their declarator-edge bounds |
| `body(fn)` | what does this function do? | body matchers that broke when upstream grew a statement inside one |
| `reads(node, name)` | is this a member read of that name? | `.endswith(".x")` — a claim about the spelling of everything in front of the name, wrong in both directions at once |
| `written(node)` | is this read a *place* being assigned to? | the same hazard remembered per patch — every patch that rewrites *every* read of a name owes the answer, and `spinner-tips` had it while `thinking-summaries` did not |
| `first(node, want)` | which node under this one is the one I mean? | `ARRAY_CALL` — the skip over a schema factory whose callee is pure noise; the array under a property's value is *chosen by what it holds*, never by being the first one there |
| `first/every(…, scoped=True)` | what does this *scope* do, not counting the scopes inside it? | searches that read a nested function's work as the enclosing one's — a callback's `return null` taken for the arm's own, three functions that merely *contain* the reducer's dispatches |
| `visible(declarator, site)` | can this site see that binding? | position ("the render falls after the `useState`") and scope-shaped guesses; nested functions **and** blocks, in one rule |
| `only(found, what)` | which one of these did upstream mean? | first-match-wins — for a rewrite there is one answer or none, and a second is a cardinality change to be told about |
| `dispatch(case)` | where does this `switch` arm begin? | label-chain scans, and every arm body ever modelled |
| `conjuncts(expr)` | is this an `&&`, and of what? | gate matchers spelled per site |
| `returns(name)` | does this answer with the value it was handed? | `return x` spelled per site, semicolon and all — the proof three matchers use to tell one small minified function from another |
| `Edit.replace/before/after` | which bytes are being changed? | off-by-one splices — a node has boundaries |

Three properties fall out rather than being enforced. **Boundaries are never
computed**: delimiting an object literal in minified JS means telling a regex
literal from division, and a hand-rolled scanner that guesses returns a `}` that
is merely wrong. **Scope is knowable**: `prop-threading` resolves the state
variable from the render site outwards, which is how it found that on 2.1.232 the
two conversation renders sit in different top-level components and only one of
them declares the state — the old matcher threaded one bundle-wide answer into
both and put an out-of-scope identifier into every patched binary. It parses, so
no gate could see it; it throws when that component renders. Outwards is only
half of lexical, and the other half is `visible`: a declaration inside a nested
function is invisible to the render outside it, and so is one inside a plain
block — `let` and `const` are the block's, `var` the function's, and the grammar
says which. Give the 2.1.232 component that declares no state a block-local
`useState` and the unscoped search threads that binding into the render, reports
2/2, and silences the note that was the only sign a render had been skipped. A
*use* keeps the whole subtree — a scope may hand its setter to the reducer from
inside a callback, and that says nothing about where the state lives.

**Cardinality is part of the invariant.** A name locates a set; a rewrite needs
one node. So the two are asked differently: a *registration* — an id into every
known-model array, an arm into every resolver of the kind, a prop into every
renderer with that signature — is a fact about a set and applies to all of them,
while a *rewrite* asks `only` for the single site and raises if this build
carries two. Taking the first match is how a decoy wins without a single counter
moving: a prepended `["sonnet","opus","haiku","opusplan"]` absorbed the entire
Codex registration, and two throwaway functions with a `case"best":` arm absorbed
both resolvers — real sites untouched, eight of eight steps green.

### What still takes judgement

The tree removes the *mechanical* fragility. It does not decide these:

- **Identity is still yours to prove.** A name locates; membership, or what a
  function does with its argument, is what says you found the right one. `model:`
  plus an array occurs 12 times; the Task tool's describe-string is what picks
  the one that matters. Weak identity is now the only way to match the wrong
  node.
- **Narrow, not broad.** `branding` renames the product's *name badge*, and the
  bundle is full of bold prose that merely contains the words — including
  sentences that would end up in a system prompt. Rewriting "every string that
  says Claude Code" would be one line shorter and wrong.
- **Rewrite at the dispatch point; never model the arm.** When an update is valid
  at arm entry, splice it there and say nothing about what the arm goes on to do.
  Arm bodies are upstream's busiest surface: 2.1.226 threaded one flag through
  five arms of the stream reducer in a single release. Entry insertion does
  reorder against mid-arm work, so it asks that the update be independent of the
  arm's own channels — decide that, don't assume it.
- **Reuse upstream's own expression where there is one.** `thinking-inline`
  routes a thinking entry down the loop's existing "not grouped" path by copying
  that branch verbatim rather than constructing a flush call; `org-label` builds
  its line from upstream's own no-org string, so nothing here spells the
  separator. What you copy cannot drift from what you copied it from.
- **Match an identity by the weakest claim that still proves it; hold a rewrite
  to the exact node.** The two jobs pull in opposite directions.
  `thinking-inline`'s null-guard neutralises only a `return` that answers
  exactly `null`, because deleting a conditional answer would delete behaviour
  — for a rewrite, doubt means stop. The codex resolvers' *identity* is the
  opposite case: "rejects an unknown model" was once asked as "the default
  returns exactly `null`", and 2.1.234 wrapping that same rejection in a dead
  recognizer (`return vXu(e)?xVe(t):null`, the stub answering `!1` — behaviour
  identical) read the resolver as gone with every anchor count standing.
  Identity asks what the node *can* answer — membership among its possible
  values, the same rule props and arrays already follow — and exactness there
  is a break waiting on upstream's next harmless addition.
- **Never anchor on a minified local** (`A_`, `mET`, `wg6`). Unchanged, and now
  structural: you capture the node, so there is never a reason to spell one.

### What "timeless" can mean

No matcher — regex, AST walk or dataflow pass — survives an upstream *semantic*
rewrite: a component that stops existing, a prop that becomes context, a resolver
folded into a table. Discovery from the bundle (below) *moves* the root invariant
and never abolishes one; agents still rest on `agentType`/`source`, models still
on the Task schema's describe-string. So the promise a matcher can keep is not
"never breaks":

> Insensitive to representation changes while the declared semantic invariants
> hold; fail closed and loudly when those invariants — or their cardinalities —
> change.

Which is also why `doctor` cannot certify grace. It measures whether *today's*
build matches, so a lucky repair reports exactly like a right one: appending a
fourth name to the entrypoint list, adding the new prop to both ordered regexes,
widening to `(?:process\.env|<local>)\.IS_DEMO` — every one of those would have
gone green, and every one buys a branch against a single build. The counting
below is the net for drift; the rule above is the only thing standing between a
repair and the next repair.

## The many-module surface

Through 2.1.241 the bundle was one module — the entrypoint carried the whole app
— and every matcher searched that one buffer. 2.1.242 code-split it: the
entrypoint became a ~20 KB argv shim that lazily imports the app across ~1,300
`chunk-*.js` modules ([INTERNALS](INTERNALS.md#the-21242-split-and-the-patchable-surface)).
The anchors did not move — they scattered. So `js.Source` **spans every
JavaScript module the container declares** (`Blob.js_modules`, discovered off the
entrypoint's own loader), and the one-module world is exactly the many-module
world with one module: `find`/`literals`/`count` sweep every module, `apply`
routes each edit to the module its node came from, and a patch says
`source.find(name)` / `source.apply(edits)` unchanged. This is representation
change of the crudest kind — the same code in more files — so it *moved* no
invariant and abolished none; the surface widened, the rules held. Three things
the move made explicit, each a rule the single-module world could leave unsaid
because the module was the bundle:

- **A node carries its module; an offset does not.** An `Edit` is built from a
  node, and the node's program root (`node.id`, distinct across parses) names the
  module the edit belongs to, so a batch spanning a dozen chunks routes each
  splice home and reparses only the modules it touched. It is why dedup keys are
  the node's `id`, never its `start_byte`: a minified offset repeats in a
  thousand modules, and two different sites at the same local offset are two
  sites, not one to skip.

- **A minified local is scoped to its module.** Resolving one — a hoisted
  `agentType:uu` constant read through its value — searches *its own* module
  (`Source.find_local`), never the bundle, where `uu` binds a string in a dozen
  unrelated chunks and `js.only` would rightly refuse to choose. Before the split
  the module *was* the bundle, so a bundle-wide `find` was a module-wide one; the
  two only came apart when the app was dealt across files. The same lesson the
  playbook already states for identifiers within a build — "a name is a spelling
  until its scope is said" — now reaches the module boundary too. A scope search
  that climbs outward (`live-thinking`'s state and memo resolution) reads the
  module wrapper by what it *is* — the pre-split monolith's whole-body IIFE is a
  module of one top-level statement; a split module's `program` is dozens, and
  its top-level components are real scopes to search — never by a fixed position,
  which read every split component as the un-searchable wrapper and found nothing.

- **The gate reads code, not linkage.** tree-sitter's JavaScript grammar does not
  model a reserved word as an import/export alias (`export{x as if}`,
  `import{if as a}` — legal ES2015, which the split minifier emits when its
  two-letter alias generator lands on `if`/`in`/`do`), so it plants a localized
  `ERROR` inside the clause. patch-cc never locates in or edits module linkage —
  every anchor is executable code — so `Source.defect` reads *past* an error
  confined to an import/export statement and returns the first one that is not,
  while a splice that broke real code still lands outside linkage and is still
  caught. It is the gate's scope stated precisely, not a tolerance bolted on: the
  eight such modules on 2.1.243 parse clean for every purpose the tool has.

## Discovery instead of hardcoding

Anything the binary can enumerate for us, it does:

- **The entrypoint module** is the one the container *declares*
  (`entry_point_id`), never the one whose name we recognise — Bun resolves its
  own entrypoint by that same index, so patch-cc agrees with the runtime by
  construction rather than by coincidence. The name list it replaced had reached
  three entries when 2.1.229 renamed the module out from under it
  (`…/src/entrypoints/cli.js` → `…/cli`) and the tool stopped opening binaries
  at all — with the answer already parsed, already carried on the dataclass, and
  already written back unchanged by `rebuild`. A list that only ever grows is
  answering the wrong question. An id past the end of the module table is a
  corrupt blob and raises; there is deliberately no fallback to guessing by
  name, because a container that cannot say which module it runs is not one to
  write to.

- **Built-in agents** are the objects carrying `agentType`, `whenToUse` and
  `source:"built-in"` (`agents.discover_agents`) — membership of one object,
  so nothing is said about their order or about how much sits between them.
  That retired a 3,000-character scan window and the `getSystemPrompt:`
  stop-word it used to bound itself with: an object has an end, a window has to
  guess one. Definitions whose `whenToUse` begins with `"Internal` are plumbing
  and are not offered; an `agentType` that is a hoisted constant
  (`agentType:Ehr` with `Ehr="worker"` elsewhere — 4 of 11 built-ins on
  2.1.233) is read *through* its value, resolved to the single declarator that
  binds it, because a name read through its value is still one we can write.
  Only a type that resolves to no single string is skipped, as unreadable.
- **Model aliases** come from the Task tool's own input schema: the array
  reached from the `model` property whose describe-string starts
  `Optional model override` — `agents.model_enums`, one home because
  `codex-models` splices the chosen Codex ids into the very arrays
  `discover_models` reads back out. Every such array, since the anchor is a
  sentence a second tool could carry too, and "every list of aliases a subagent
  may be pinned to" answers both questions where "the first one" answers
  neither.

A new upstream agent or model appears in `patch-cc list`, the menu, and
`--model` validation without a code change. If the enum anchor ever vanishes,
discovery offers *nothing* rather than a guessed default: `doctor` prints
`models: inherit` alone, and a requested pin fails its required step instead of
landing on a hardcoded triple the real bundle may not even accept. A masked
absence is the one thing the whole report exists to prevent, so absence flows
through the path a present-but-different name does — the same emptiness
`codex-models`' own `enum` step already reports as broken.

**The hardcoded list, and the guard that is derived instead.**
`models._RESERVED` names Claude's short built-ins and patch-cc's own menu
sentinels (`inherit`/`keep`/`default`). Its one irreducible job is the path
with *no binary in hand*: `family_aliases` derives a shortcut from the chosen
ids alone, so a pathological `gpt-5.6-opus` cannot mint an `opus` shortcut.
There it is a snapshot, and upstream outgrowing it costs at most a shortcut that
resolves nowhere — never a brick.

The **collision** guard is derived, because registration cannot be trusted to
refuse one itself: `_register` counts a name the bundle already holds as
*landed* by design (an upstream that ships the id has achieved the goal), so
registering `opus` reported `ok 7/7` and baked `["opus"].includes(...)` into the
redirect — diverting real Opus requests to the gateway. So a chosen id is
refused *against the bundle's own tables* before it is registered: the validator
strings and every registry `provider_ids` value, both read from the binary in
hand (`patches.codex.claimed_model_names`). That catches the thirteen provider
ids on 2.1.233 (`us.anthropic.claude-opus-5` and kin) that are valid slugs no
`claude-` prefix would stop — a duplicate `provider_ids` is what the registry's
own `safeParse` rejects at first use with `provider id collision across distinct
entries`, bricking every command. The CLI refuses at the front door
(`_codex_selection`), the patch refuses at the back (`_codex_models`), and both
read the same fact off the bundle, so no path bakes a colliding id and neither
answer can fall behind upstream.

## The manifest

Every patched bundle ends with one comment line recording what was applied; its
shape lives in [INTERNALS.md](INTERNALS.md#the-manifest). What matters here is
that it makes `status` a parse instead of a guess — several patches are value
flips (`verbose:!0`) that leave no other fingerprint — and that it is the
**only** evidence `is_patched` accepts. Authorship is declared, never inferred:
`is_patched` once also took side effects of our edits as proof — the `__cc_`
identifier prefix, the old `--version` marker — until 2.1.227 shipped
`__cc_name`/`__cc_line`/`__cc_set` shell variables of its own and every
pristine install read as patched, hard-blocking `apply`. A prefix tracks
upstream's naming fashion, never authorship (the same lesson as the `.enum(`
callee above), and once upstream ships it, it fires on every later build, so no
narrowing rescues an inferred fingerprint — `__cc_line` was already both our
arrow-param and their shell variable. Binaries patched by pre-manifest versions
therefore read as clean, which costs nothing now that Claude's daily
auto-update has long since replaced that population.

## How resilience is detected

`patch-cc doctor` runs every patch against a **clean** bundle (the pristine
backup if the installed binary is already patched) and reports, per patch:

- `candidates` — how many times the anchor/shape was found
- `applied` — how many rewrites actually changed something

Configurable patches are fed a synthetic configuration built from the bundle's
own discovered agents and models (every agent assigned a model different from
its current one, a test brand), so branding and the model overrides are
exercised for real — nothing is exempt from the dry run.

The patches are **composed**, one feeding the next, exactly as one pass of
`apply`'s fixpoint composes them. Running each against the pristine source in
isolation was cheaper and could not see the one ordering this project calls
load-bearing — `codex-models` registering the ids `subagent-models` then pins —
nor anything at all about the bundle the run produced, which it discarded.

The composed result is then **baked and executed**: doctor writes it into a
temp binary through the same `container.write` as a real apply — staging,
round-trip verification, the container checks — and runs `<binary> --version`,
expecting the version-marker suffix in the output (our own edit's print, so the
check proves patched code *executes*, not merely that the binary boots around
it). Matching and running are different truths with different failure owners:
matchers break in the patches, the run breaks in the container layer, and
2.1.246 is the build where they split — every matcher green, every module
round-tripping byte-perfect, and the written binary `SIGSEGV`ing in Bun's graph
loader because what the rewrite had destroyed (the Bun 1.4.1 record chain and
the shared bytecode string table, docs/INTERNALS.md) lives in bytes no module
owns. The temp binary is always removed; the backup under test is never
written to.

Three readings of the two numbers, kept distinct:

| symptom | meaning |
|---|---|
| `candidates == 0` | the anchor is **gone** — a real regression |
| `candidates > 0, applied == 0` | shape found, rewrite was a no-op — usually already patched |
| `0 < applied < candidates` | some sites landed, some drifted — **partial**, so non-green on a clean bundle |

The middle row and the last are different verdicts: the first is `broken`
(nothing landed), the last `partial` (some did). A rewrite that *undercounts* —
`applied > candidates`, the durable witness behind more rewrites than the
witness has (a header read at several sites) — is not that row and stays `ok`.
Both `applied == 0` and the partial middle are the same concern one number
apart, so `Outcome.health` reads them off `applied < candidates` rather than off
`landed` alone, which called `cand=2 applied=1` fully applied.

Both zero-rows are verdicts, not commentary: a rewrite has *landed* only when it
found something **and** changed something (`Outcome.landed`). Reading `applied` alone
was a hole exactly the size of the two patches that pay for a second, durable
witness — `thinking-summaries` counts off the header name behind the read,
`org-label` off the interpolation behind the conditional — so renaming
`x-cc-atis` left every read rewritten, nothing left to rewrite them *for*, and
a green tick.

Where the two numbers come from one place, they move together and the second
row above cannot fire; it is worth knowing which patches those are, because for
them `candidates` is a restatement rather than a witness. The row earns its keep
exactly where a patch pays for a witness the rewrite does not produce: the two
above, `codex-models`' registrations (a name already present is landed without
an edit), and `live-thinking`'s two core updates, which are credited off the
markers found in the bundle the run produced rather than off any matcher. A
witness that disagrees with its own rewrite reads as `candidates == 0, applied
== 1` — a state the table has no meaning for, and a defect in the witness rather
than in the build (see `org-label`).

`doctor` prints the documented anchor counts for any broken patch, so a `0`
next to an anchor points straight at what moved.

**A dry run is the one place the second row cannot mean "already patched."**
`doctor` is handed a *clean* bundle — the pristine backup when the install is
patched — so the benign reading of `candidates > 0, applied == 0` is
unavailable to it, and what is left is drift. It therefore exits non-zero on
`partial` as well as `broken` (`DryRun.unhealthy`), where it used to print `~`
on the patch's own line and then close with "all patches still match this
build" and a zero exit. `apply` reads the same verdict and answers differently
on purpose (`PatchReport.regressions` takes `broken` alone): there a partial
patch *has* landed and does ship, and dropping it would cost a working feature
over a missing refinement. Two questions, one `Outcome.health`, neither
re-deriving it.

### The syntax gate

Both numbers above are counts, and a count cannot tell a rewrite that landed
from a rewrite that landed *one prop-name to the left*. So the bundle is parsed,
and any `ERROR` or `MISSING` node **outside module linkage** aborts
(`src/patch_cc/js.py`; the linkage read-past is
[above](#the-many-module-surface)).

The parse is not a separate pass any more: locating already needs it, and an
edit costs one incremental reparse — ~75 ms on a 25 MB bundle against ~3 s for a
full one, measured to produce a structurally identical tree. So the gate sits at
**every batch of edits**, which is what lets it name the culprit: a patch whose
rewrite produces rubble raises out of `Source.apply`, is reported broken, and is
dropped from the set while the rest still apply. `apply` then checks once more on
exactly the bytes it is about to write — manifest included, after the fixpoint
settled — because interactions between patches are the one thing no single patch
can check.

A `Source` is **immutable**, and that is what makes the sentence above true on
every pass rather than only the first. `Tree.edit` mutates, so `apply` edits a
copy: editing the live one left the *input* holding a tree that no longer
described its bytes, and since the fixpoint re-runs every patch from that same
pristine `Source`, one drifted matcher on pass 1 made pass 2 report twelve
healthy patches as broken — each blamed for an anchor that was never missing —
and wrote nothing at all. `doctor` is the one shape that cannot see it: a dry
run is a single pass, and it hands each patch the previous patch's *output*.

This is the only check that reads the bundle as a *language*, and it exists
because everything else agrees with rubble. `doctor` counts. The write verifier
re-extracts the bundle and compares it against what we meant to write, so a
corrupt splice matches itself perfectly. The failure is not hypothetical, and it
recurred during the move to the tree: a callback's parameter read as the whole
callback spliced an arrow function into its own argument list. The gate caught
it in the same second, named the step, and nothing was written.

One corruption the parse *does* agree with is our own, so it is refused a step
earlier. Two edits over the same bytes splice each other's output — replace `x`
twice in `let x=1;` and the result is `let abcdefbcdef=1;`, which parses — so a
batch has to be **disjoint**: every edit inside the bytes no earlier one has
already rewritten. That is the same range check that keeps an edit inside the
bundle rather than a second rule beside it, and it costs nothing to hold; no
patch emits an overlapping batch on any build in the corpus. What it buys is the
matcher that *starts* to — an `if` and the `if` nested inside it, both read as
the same guard — being told so instead of writing the difference into a binary.

It is deliberately **not optional** — a safety check the install may or may not
have is not a guarantee, and this is the last thing standing in front of
CONDUCT's first rule. tree-sitter is what parses it, from a stable-ABI wheel,
with no Node or Bun involved.

What it is not is a compiler. tree-sitter recovers from errors and does not
implement every ECMAScript early-error rule, so a green gate means the token
stream still assembles into a tree — not that the program is legal in every
respect. That is the right scope: structural damage is what splicing causes. It
is also why an *out-of-scope identifier* is not its business — that one parses —
and why `prop-threading` resolves scope itself.

### Expectations — why a green tick means something

Counting alone cannot tell "this build lacks that shape" from "the feature is
dead": a patch whose optional shapes rewrite happily while a load-bearing one
is gone still changes bytes, and would read as green. So each sub-step is
declared with what its absence *means* (`Outcome.declare`):

| mark | meaning | absence is |
|---|---|---|
| `optional` | a shape only some builds carry | informational |
| `required` | the patch does nothing useful without it | a regression |

`Outcome.unmet()` turns a violation into a sentence (`required step
group-routing found nothing`); `Outcome.failures()` adds any exception the
patch raised, because a patch that threw and a patch that missed an
expectation are the same verdict wearing different clothes; `Outcome.health`
folds them into `ok` / `partial` / `broken`. Every surface (`apply`, `doctor`,
the menu) reads *those two* and never re-derives either — `doctor` once judged
on counts alone and printed a red cross and "all patches still match" in the
same report. Adding a sub-step means deciding which row of the table it is in;
that decision is the whole safety net.

Three rules keep the net from having holes:

- **A step nobody declared cannot report its own death.** `branding` was the
  one multi-step patch here with no expectation anywhere, and its `badge` step
  is the product name where you always see it. Make the badge's boldness
  conditional upstream and the step reads `0/0`, the patch reads green, the
  banner still says Claude Code, and `status` reports the new name from the
  manifest. `badge` is required now; `styled` and `welcome` are sentences
  upstream may reword, which is the other row of the table.

- **Count what the rewrite *achieved*, not that some rewrite happened.** A step
  built from many literal edits lands as soon as *one* of them applies, so an
  incidental edit can vouch for the essential ones. `live-thinking` learned this
  the hard way: a reducer whose setter threading applied while every event arm
  had drifted reported hits and streamed nothing. The two edits that *are* the
  feature are now checked by the markers their builders emit
  (`streaming._CORE_UPDATES`), which no amount of neighbouring churn can fake.
- **Declare an expectation before the work, not inside it.** A step created only
  by its own success cannot report its own absence. This is the API's shape,
  not a discipline: `Outcome.declare` is the only way a step comes to exist
  (up front, required and optional named apart), and `outcome.step(name)`
  retrieves — an undeclared name raises, so a typo cannot mint a silently
  optional step. Work that is owed conditionally declares under the same
  condition it runs (`codex._register_context`), and a name resolved from the
  bundle is declared the moment it resolves — `agents.bypassed_agents` has no
  step to name until the helper's guard gives up the pinned agent, and a guard
  that vanishes leaves the always-printed note, with no name left to declare.

`apply` acts on the verdict: a broken patch is re-run out of the final pass, so
its orphan edits never reach the binary, the manifest never claims it, and the
command exits non-zero. The healthy patches still apply.

That re-run is a **fixpoint**, not one retry. Patches see each other's output,
so dropping one can change what the next finds; the set is settled only when a
whole run comes back with nothing broken, and each patch is reported by the last
run it took part in. Judging the bytes of the final run by the verdicts of the
first is how a manifest starts lying. The manifest is held to the same rule:
`brand`, `suffix` and `models` are recorded only when *their* patch landed, so
`status` can never assert a name the bundle does not contain.

## Sub-steps, and why `live-thinking` has them

Most patches are one rewrite. `live-thinking` is fourteen named sub-steps,
because upstream has reshaped the stream reducer at least three times and a
single hit count cannot tell "all landed" from "half silently drifted".

Each sub-step records its own `candidates`/`applied`:

- **The chain from stream event to rendered row**, all required —
  `prop-threading` (the state reaches the renders), `display-mode` (the API is
  asked for summaries), `transcript-signature` (the renderer receives the
  state), `inline-extras` (it is drawn in order), `reducer` (the stream handler
  is found and threaded). Without any one of them live thinking is dead however
  many other steps land.
- **One step per dispatch point** — `request-start`, `message-stop`,
  `text-clear`, `message-delta-clear`, `thinking-start`, `thinking-append`. A
  build that folds an arm away reads as that point's absence *by name*, never as
  a bare count drop inside an aggregate. Each point is found by the API string
  it dispatches on and never by the read that reaches it: the two wrapped in a
  test match the `===` against `"message_stop"`, wherever the value came from.
  Spelling the whole test cost `message-stop` on one character — `e.event.type`
  gaining a `?.` left the live block never marked finished, shimmering after
  every turn, with the step reporting *absent* and the patch green.
- **The two updates that are the feature** — `block-start` and
  `thinking-delta`, required, and not matchers at all but *proof*: each is
  credited only when its marker is found in the bundle the run produced. A
  reducer whose setter threading applied while every arm had drifted once
  reported hits and streamed nothing.
- **`final-summary`**, optional: redacted thinking in the closing summary is a
  refinement, not the feature. Its guard is whichever enclosing `if` *tests the
  block* — 2.1.236 nested an experiment gate between the test and the summary
  it guards, and "the nearest `if`" read a shape that had merely moved as one
  that was gone. The climb is bounded by the function that declares the block,
  because a receiver's name is only a spelling until its scope is said.

An *optional* sub-step that finds nothing is reported as absent, not broken —
it is just a shape this build doesn't have. A sub-step that finds a shape but
fails to rewrite it (`candidates > 0, applied == 0`) shows up in
`missed_steps()`.

The reducer used to be a **variant group** of two heads (`reducer-options` for
2.1.138+, `reducer-legacy` before it) plus four legacy cleanups (`memo-cache`,
`memo-removal`, `linger`, `bottom-row`). All six matched nothing on any build in
the corpus and are gone; see [Removed](#removed-patches). The group mark went
with them — no step declares one, so `expect` is a `bool`. The reducer is now
one step because there is one shape to find: a function that dispatches on all
three stream events, whose options bag is reached from the parameter it is
destructured from rather than matched. The setter is taken out of that bag
under upstream's own name into one
of ours, unconditionally: two patterns naming one property both read it, so
"upstream already binds it" is not a case to handle.

Notes print on every run, green ones included — an early warning held back
until something breaks arrives too late to be one. Absences are the noisy half
(most patches lack several shapes on any build) and wait for a verdict that is
not `ok`. Both surfaces draw the same list (`ui.findings`): the CLI and the menu
each worded their own once, and the menu's copy had quietly lost the exception
that broke a patch along with every note.

## Repairing a broken patch

1. Get a clean bundle from a current binary:

   ```bash
   patch-cc extract ~/.local/share/claude/versions/<ver> > clean.js
   ```

   Since 2.1.242 that is every JavaScript module concatenated, each behind a
   `// ==== patch-cc module <n> ====` header, so `rg` still works over one file
   and the header says which chunk a hit lives in.

2. Run `patch-cc doctor`. Note which patch dropped to `candidates == 0`, or —
   for `live-thinking` — which sub-step.

3. Ask the bundle where the anchor went, as the name it is:

   ```bash
   rg -c 'case"collapsed_read_search"|thoughtForMs|spinnerTipsEnabled|Backgrounded agent' clean.js
   ```

   A count of `0` means upstream renamed or retired the thing. A count that
   stands while the patch fails means the *name is still there and the shape
   around it moved* — which is the case the tree is for, so the repair is
   usually a different climb, not a new anchor.

4. Reproduce the location in a REPL before editing any patch. `js.Source` is
   the whole tool, and the loop is short enough to run by hand:

   ```python
   from patch_cc import js
   src = js.Source(open("clean.js", "rb").read())
   for node in src.find("thoughtForMs"):
       arm = js.up(node, "if_statement")
       print(node.type, js.text(arm)[:120] if arm else "(in no if)")
   ```

   The `if arm else` is the lesson, not noise: a climb lands on nothing as
   readily as on a node — `thoughtForMs` is also a property of the group
   *factory*, which no `if` encloses — and `js.text` raises rather than hand
   back an empty name for a splice to be built from.

   Then update the climb in the relevant `src/patch_cc/patches/*.py`. Prefer a
   weaker structural claim (one more `props` membership, one fewer assumption
   about which node the edit hangs off) over a second branch; a second branch is
   the answer only when upstream genuinely ships two shapes.

5. Re-run `doctor` until the patch (and each expected sub-step) is green, then
   apply to a real binary and check the behaviour at runtime.

6. Sweep the fix over the versions you still have. `doctor` takes a path, and
   every binary patch-cc has ever touched left a pristine copy in
   `~/.local/share/patch-cc/backups/`:

   ```bash
   sweep() { for b in ~/.local/share/patch-cc/backups/*.orig; do
     echo "== $(basename "$b")"; patch-cc doctor "$b" || true
   done; }
   git stash && sweep > /tmp/before.txt; git stash pop && sweep > /tmp/after.txt
   diff /tmp/before.txt /tmp/after.txt
   ```

   That is what keeps a repair honest, and the `diff` is the half that matters:
   a red build is loud on its own, but a *widened* locator shows up as an old
   build's counts quietly moving — 1 candidate becoming 2, a step that used to
   find nothing now finding something. Read the diff, not just the exit codes,
   and be able to say what each moved number means before you keep it. Nothing
   is persisted between runs and nothing needs to be: the corpus is on disk —
   enumerated with its hashes in [corpus.md](corpus.md) — so both sides are
   recomputed from the binaries themselves.

   Each build's report ends with its baked binary's own `--version` output (the
   smoke run above), so the sweep also proves every corpus build still *bakes
   and boots* — the half matcher counts cannot see, and the half 2.1.246 broke
   while every count held. A moved runtime line is as much a finding as a moved
   count.

## Patch reference

Grouped by source module. The menu's three groups (Output & display, Models &
effort, Chrome & branding) are display categories that each draw from several
modules — module says where a matcher lives, group says what the patch changes
for you. Each entry: what it changes, the stable anchor, and where it lives.

### Output & diffs — `output.py`

- **`tool-calls`** — force verbose collapsed read/search rows.
  Anchor: the `"collapsed_read_search"` label — the name, and the arm as the
  node it labels, never the two spelled together with the minifier's spacing in
  between. The row is whatever inside that arm is *handed* a `verbose` prop —
  an object literal, never a pattern binding the same name, because writing a
  value over a binding is rubble and not a flip. Two matchers used to spell the
  arm's two statement forms, the second carrying six neighbouring prop names
  purely to prove it had found the right call — the label proves that.
  *Value-flip* (`verbose:!0`) — the manifest is its only fingerprint.
- **`create-diff`** — render created files through the diff renderer with `+`
  lines. Anchors: the `"create"` and `"update"` labels of **one** `switch` —
  membership of that switch and never a direction, since which arm upstream
  lists first is not a fact about either and walking forward only read a build
  that puts `update` above `create` as the anchor being gone. Both renders are
  found by what they are *handed*, by membership and never by the whole set:
  the created row carries `filePath`/`content`/`verbose`, the update row
  `filePath` and `structuredPatch` (to which upstream has since added
  `previewHint` and `collapsed` — the same thing it would one day do to the
  other). Exact-set equality on the created row read one added prop as the
  anchor being gone, and read it that way while `doctor` printed all three
  anchor strings present; on every build in the corpus the two rules count the
  same single site. The renderer, component and `style` are copied from the
  update arm rather than named, so the JSX runtime this build emits is never
  mentioned; the line count is borrowed from the arm's own "Wrote N lines"
  helper, which is required rather than fallen back on — the inline count that
  stood behind it ran on no build and disagreed with the borrow it stood in for
  (upstream drops the empty field after a trailing newline; `split` keeps it).
  Which call *is* that helper is settled by the word the arm pluralises the
  number with (`"line"`, upstream's own), because "the first call in the arm
  taking the content" is a description of every call: a bare `String(t);` at the
  arm's head was baked in as the file's length, `newLines:String(t)`, counted
  and green.

  **Alive by eye:** ask for a new file *via the Write tool* — the row reads
  `Added N lines` over green `+` rows. Two things legitimately show no diff and
  are not this patch failing: a file created through **Bash** (heredoc, `tee`,
  a script) is Bash output and never enters this renderer; and upstream keeps
  planting early returns *ahead of* the spliced render — plan-directory files
  ("/plan to preview"), condensed contexts (subagent progress rows, session
  summaries: "Wrote N lines to path"), and scratchpad/`.workshop.*` files in
  non-verbose re-renders ("Wrote N lines ⧉"). The live conversation renders
  the current message verbose, so the diff shows there today; those guards are
  the patch's effective coverage narrowing upstream-side, which `doctor` —
  counting matchers, not paths — cannot see. Measured on 2.1.234 (live,
  resume, plain/workshop/scratchpad files): every reachable path drew the
  diff.

### Thinking — `thinking.py`

- **`thinking-summaries`** — stop echoing the account's server-side experiment
  bucket, so the API returns thinking blocks with text in them.
  Anchor: the `atis` property, read as a member (header name: `x-cc-atis`) —
  the name, not `?.atis`, because the optional chain in front is the caller's
  choice and the text also fits the *front of a longer name*: `?.atisVersion`
  read as this bucket and became `(<cache read>,void 0)`, discarding a value
  this patch has no claim on. **Every** read is
  replaced by `(<cache read>,void 0)` — the cache call kept, its value
  discarded — so there is no getter to identify. Emptying *the* getter meant
  proving which function was it, because replacing a brace-free body wherever
  the property appeared would delete whatever an unrelated reader did, and one
  `.atis` per bundle is happenstance rather than an invariant. A read that
  reports nothing is correct however many there are, so the branch that had to
  tell them apart is gone. Candidates are counted off the header name, not the
  reads, so a bucket that survives as something other than a member read reads
  as `candidates > 0, applied == 0` — something to repair — instead of the zero
  that would equally mean upstream retired the mechanism. The other half of
  that trade is the header *going*: rewriting reads that no longer feed it
  achieves nothing, which is why landing takes both counts. A read that is a
  *write* is skipped, out of the same home (`js.written`) `spinner-tips` draws
  from: upstream writes neither today, which is exactly when the hazard is
  cheap to answer for, and `(cache(),void 0)=x` is rubble that would take the
  one read this patch exists for down with it.
  Claude Code caches a GrowthBook assignment (`clientDataCacheSlots[...].atis`
  in `~/.claude.json`, one slot per account × entrypoint × model) and replays it
  to the API on every request so the server applies the same bucket. A slot in a
  bucket that withholds thinking summaries is served thinking blocks carrying a
  signature and an **empty string** — no `display` mode, effort level or
  `thinking.type` changes it, and two requests that differ only in the header
  differ in nothing else. Because the slot is per model too, one model can think
  visibly while another stays blank in the same session. Every other thinking
  patch then renders that empty string faithfully, so the symptom reads as
  "thinking works on one account and not another, same binary, same config".
  The value is read to set that one header, so reporting nothing lets the
  header's existing `if(value!==void 0)` guard skip it — nothing is sent, and no
  branch was added to stop it. Keeping the cache call is what preserves the
  next sentence's promise. Mind the breadth: this drops the
  bucket for *every* experiment the account is enrolled in, not just the one
  that empties thinking. Local feature values still come from the on-disk cache,
  so only the server's view of the assignment changes.

  Diagnose from a transcript rather than by eye — `thinking` blocks are
  recorded whether or not they carry text:

  ```bash
  jq -r 'select(.type=="assistant").message.content[]?
         | select(.type=="thinking") | (.thinking|length)' \
     ~/.claude/projects/<slug>/<session>.jsonl | sort -n | uniq -c
  ```

  A column of `0`s is this patch missing (or a bucket it does not yet cover);
  a spread of real lengths means the text arrived and the problem is rendering.

- **`max-effort`** — let `/effort max` save as the session default, as
  low–xhigh already do. `max` is first-class in session (the binary's level
  list is `["low","medium","high","xhigh","max"]`; the CLI `--effort` and
  `CLAUDE_CODE_EFFORT_LEVEL` both accept it) — only *persistence* runs through
  two whitelists that stop at xhigh, and both are required rewrites because
  either alone changes nothing:

  - **`gate`** — one tiny whitelist function
    (`if(e==="low"||…||e==="xhigh")return e;return`), and the whole choke
    point: the `/effort` save path writes `effortLevel` to userSettings only
    when it returns a value, the startup resolver reads the setting back
    through it, and the model-picker flow persists through it too — so write
    and read widen together in one rewrite. It is identified by what it
    *does* — the value it admits is the value it returns — which is what keeps
    it off any other function that merely compares effort levels, and the
    rewrite extends the `if`'s condition rather than rebuilding the function,
    so neither minifier statement form is mentioned.
  - **`schema`** — the settings validator's `effortLevel` enum, reached from
    the property down to the array *that carries the four levels*, so the
    schema factory in between is skipped without being described and without
    "the first array in there" standing in for it. The array is
    confirmed by *containing* the four levels, which is what tells it from the
    twelve other `effortLevel` properties in the bundle — a set, not a
    sequence, the same rule `codex-models/validator` states outright. Asking
    for the exact ordered list read both a reshuffle and a fifth level as the
    persisted-settings schema having vanished; membership counts the same one
    array on every build in the corpus. Its `.catch(void 0)` is the safety
    property: a **clean** binary reading a settings file that still says
    `"max"` (baked, then reverted by a Claude update) treats it as unset — the
    default effort, never a broken settings parse. Losing the patch costs the
    preference, nothing else.

  Both matchers tolerate the level already being present and count it as
  landed — an upstream that adopts `max` persistence itself is the goal
  achieved, not a miss (the same judged-on-achievement rule
  `subagent-models` follows). Effort semantics downstream are untouched: the
  org-limit clamp runs before the save, and a model that cannot run `max`
  clamps at request time exactly as an interactive `/effort max` does today.

- **`thinking-inline`** — make historical thinking blocks render inline.
  Three required rewrites:

  - **`group-routing`** — the activity-group builder swallows a finished
    thinking message into the collapsed "✻ … (thought for Ns)" group, so it
    never reaches the renderer. Anchor: `thoughtForMs`. Of every branch in that
    loop, exactly one both accounts think-time and absorbs the entry **into the
    same group** (`<group>.messages.push(…)`, where `<group>` is the local the
    think-time was just added to) — that pairing *is* the thinking arm, and
    neither half is a shape. Naming the group is what makes the second half an
    identity rather than a description: `.messages.push` alone matched any push
    at all, so a decoy one statement ahead of the real absorb was rewritten
    instead of it, at 1/1 with thinking still swallowed. The absorb call is
    replaced by the chain's own
    terminal `else` copied verbatim (flush the group, push the entry into the
    visible list), and the summary write is sent to a dead property name rather
    than deleted, which is a single node whether upstream fuses it into an `if`
    head or guards it as a statement of its own. The eight-line regex this
    replaced modelled the arm body and died on 2.1.232, when upstream extracted
    the summary into two helper calls and split the `if`.
  - **`null-guard`** — the early return that hides thinking outside transcript
    mode: an `if` keyed on the very flag the arm hands its renderer as
    `isTranscriptMode`, whose branch answers `return null`. Both minifier forms
    (`if(x)return null;` and `if(x){return null}`) are one tree, so there is no
    longer a form to miss — which is exactly how this patch broke once. What is
    neutralised is the **answer**, not the conditional around it: the `return
    null` becomes `;`, so a statement upstream puts before it still runs (a
    build that grew one had it deleted, at 1/1 and green) and a branch that
    merely *contains* another gate's return cannot claim it — reading the
    subtree put one return in a batch twice, which `Source.apply` refuses.
    Splitting the test across nested `if`s is a shape this does not follow: it
    reports the step absent, which is loud, rather than guessing which of the
    two conditions was the transcript one.
  - **`renderer-props`** — force `isTranscriptMode:!0` (and
    `hideInTranscript:!1` where present) on every object *passed* in the
    `case"thinking":` arm that carries them. Presentation is a set of
    properties, never a call shape — of a bag being handed over, never one
    being taken apart, for the reason `tool-calls` gives above.

  The component itself has no gate — an empty summary renders nothing, which is
  why trivially short thinks may still show no block.

### Live thinking — `streaming.py`

- **`live-thinking`** — the fourteen-step patch above.

  **`prop-threading`** hands the live-thinking state to every conversation
  render — a props bag *handed to a component* (an argument) that carries
  `conversationId` and `messages`. Being an argument is part of the identity:
  a module-level literal, a return-value payload, or a config object may
  legitimately carry the pair, and none of them is a render. On a build whose
  component owns the state it inserts `streamingThinking:<state>,` before the
  bag's `conversationId`; on a store build it reroutes the render through a
  wrapper component of ours that subscribes itself (below). Three things do
  the work, and each replaced something that had broken:

  - **Identity is membership — of the essential props alone.** The props that
    make a render a conversation render, asked of one object rather than of a
    span of text. Four matchers once modelled this — a brace-free
    `createElement` call, an ordered prompt renderer, and two regexes differing
    only in the order two call sites listed the same props. 2.1.229 killed the
    ordered pair at once by inserting `onRateLimitAutoQueueContinue:` between
    two of them. The membership then carried `agentDefinitions` as a third
    conjunct — witness and insertion point in one — and 2.1.235 retired that
    prop from the bag: an identity resting on a *neighbour* read a build that
    plainly drew four conversation renders as drawing none, with every anchor
    count standing (`agentDefinitions` still occurs 97 times — as other
    components' prop). The conversation pair is what the render *is*; the
    neighbour was one more thing upstream had to keep, and upstream owes the
    matcher nothing. Same class as the resolver default on 2.1.234: the
    identity must be the weakest claim that still proves it, and every conjunct
    past that is a break waiting on a harmless refactor.
  - **The insertion point is a prop boundary**, so nothing computes where a
    literal ends. Being an `object` and not an `object_pattern` is what
    separates a prop being *passed* from one being *received*; the regex spelled
    that as a call-opening it had to match, and inserting into a pattern would
    have rebound a local.
  - **Scope is resolved per site.** The state variable is found from the render
    outwards — the enclosing scope that hands `onStreamingThinking` to the
    reducer is the one that owns the state — and the declaration it lands on
    has to be one that render can *see* (`js.visible`). This is the claim the
    old matcher named as owed and could not discharge, and it was not idle: on
    2.1.232 the
    two conversation renders sit in *different* top-level components, only one
    of which declares the state. Threading one bundle-wide answer into both put
    an out-of-scope identifier into every patched binary — it parses, so no gate
    could see it, and it throws when that component renders. The second site is
    now skipped with a note instead. Making it work would mean threading the
    prop through the intermediate component too, which is a change nobody has
    asked for; it renders nothing today either way. What the state was
    *initialised* to is deliberately not asked: `useState(null)` is every
    build's spelling and `useState(void 0)` would be the same state, while the
    setter is the identity that matters.
  - **The state has two semantic homes, and each is identified by the only
    name it has.** Through 2.1.235 the scope that hands `onStreamingThinking`
    declared the state itself, and the state is the array pattern binding the
    handed setter — `useState`'s pair, which nothing else names; its value is
    threaded into the bag as it stands. 2.1.236 moved the state into an
    external stream store (`subscribe`/`getSnapshot`/`_publish` — the
    `useSyncExternalStore` shape), and there the question this step used to
    ask — *which idiom holds the state* — is retired, because it broke on
    every answer it ever gave. Each answer was a spelling of React's
    data-access fashion, the busiest surface upstream owns: the handing ("the
    call's only argument is the very expression the setter was read off")
    died on 2.1.246, when the handing moved into the engine
    (`this.stream.setStreamingThinking`) with the state unmoved and
    `onStreamingThinking:` still twice in the bundle; the whole-snapshot
    destructure that replaced it (`{streamingToolUses:…}=useX(<store>)`, the
    pattern naming the store's own field) died on 2.1.247, which reads the
    store through per-field selectors instead (`et(<store>,wv)` with
    `wv=(s)=>s.streamingToolUses`) — same store, same fields, a third
    spelling, and a fourth break in this one question while the other
    thirteen steps rode through both restructures untouched. A matcher
    enumerating idioms is a whitelist against a fashion, each branch buying
    exactly one build.

    What survives every spelling is the **twin**. Live thinking is the
    sibling of live tool-uses — same store, same snapshot, same renderer —
    and the tool-use half is a feature upstream ships working, so every build
    must carry, in the render's own reach, a read of `streamingToolUses` off
    the store. That read is the *production*, found as whichever of the field
    name's two grammar positions this build spells — a snapshot pattern's own
    key (the hook call its sole argument, since a second would make the
    pattern something other than the snapshot), or a selector beside the
    store (a function answering the field off its own parameter, inline or
    hoisted behind a module-local name) — one answer or none per scope
    (`js.only`), behind whatever `??` fallback the read wears (`js.values`;
    2.1.247 arrived wearing one). The production is then *reused verbatim* —
    the reuse-their-expression rule `org-label` and the extras memo already
    follow — so the state is read the way this build reads its twin, with one
    field name swapped, and upstream can only break the claim by breaking
    their own feature. That is the anchor-selection rule this step's whole
    history teaches: prefer claims upstream cannot drop without paying for
    them — the dispatch strings, the store's field names, the twin's dataflow
    — over claims only this patch needs; a neighbour prop, a handing site and
    a storage idiom were all the second kind, and every break above was one
    of them.

  - **On a store build the rewrite is a subscription, not a value.**
    Threading the state's value was sound while the resolved render was
    live-computed, and 2.1.247 retired that render too: the surviving
    conversation render is react-compiler cached behind a fixed slot-test
    chain (`if(Yr[15]!==ph||…)Du=r(Ih,{…})`), and a prop the compiler never
    saw is a prop no slot tests — the element is reused, the child bails on
    identity, and a perfectly threaded value renders exactly once.
    Parse-green and frozen, a failure class `doctor` cannot see. So the
    render is rerouted through a module-scope wrapper component
    (`__cc_LiveConversation`) spliced beside it: the bag gains
    `__cc_stream:<store>,` (the production's own store argument, whose
    identity is as fresh as the conversation the cache already tests), the
    render's component argument becomes the wrapper, and the wrapper
    subscribes itself through the production's own hook with a selector of
    ours in upstream's per-field shape, handing the component
    `streamingThinking:` beside the spread props. The hook's optional
    selector parameter is upstream's since the store era began — 2.1.236's
    hook and 2.1.247's are the same two-parameter function, measured — and
    where the destructure spelling leaves it undemonstrated, an in-module
    hook is held to it (resolved, and required to declare a second
    parameter), because a hook that ignored the argument would thread the
    whole snapshot with every count green; an *imported* hook is accepted on
    the measurement, a residue accepted by name rather than a cross-module
    resolver built for a build that has never shipped. The transcript
    renderer's own memo comparator compares unknown props by
    identity (since 2.1.210), so a fresh value re-renders it and a quiet one
    does not, cached parent or not: a component re-rendering from its own
    subscription is the one contract in this chain no minifier or compiler
    rewrites. Every name the wrapper re-spells — the JSX callee, the
    component, the hook — is read off the site and owed module-scope meaning
    (`_unshadowed`): any binding of the spelling on the way — a parameter, a
    declarator, a function's or class's declared name, a catch clause's
    parameter — would make the wrapper name something else that parses,
    verifies, and throws at first render, so it refuses the wrap loudly
    instead, and the store expression's own spellings are walked the same
    way, bounded at the scope upstream wrote them in. The witness is
    unchanged, paid the way `thinking-summaries` pays
    it: the field the wrapper selects must still be named by the bundle's own
    objects (the store's snapshot initialiser, its publish call —
    `streamingThinking:null`, upstream's own since 2.1.236), so a store that
    renames the field reads as the step reporting the store rather than
    subscribing to `undefined`. Two things still come free with the store:
    its setter takes functional updaters — React's own contract, which the
    reducer splices already speak — and it hides a finished block itself
    after 30 s, upstream's own linger.

  A render was once selected by *position* — the observation that the real sites
  fall after the state's `useState` declaration. That is worth recording as a
  mistake, because it looked like a scope proof and was not one in either
  direction: bundle module order is the bundler's, not a fact about a render,
  and a closure written before a `let` may legally read it once invoked.

  **The reducer** is the function that *dispatches* on all three of
  `stream_request_start`, `thinking_delta` and `content_block_start` **and** is
  handed the stream callbacks to answer them with — where
  dispatching is a `switch` arm labelled with the event or the `===` test that
  routes it, whichever this build spells, since upstream uses both at once.
  Both halves are needed, and dispatching is asked of the scope itself
  (`scoped=True`): *containing* three dispatches is not performing them, and on
  2.1.210 the engine loop and `submitMessage` each contain the whole reducer, so
  the right function was picked by nothing better than sitting earlier in the
  bundle. Its options bag is reached from the declarator that destructures one
  of its positional parameters, so the head is never matched —
  2.1.226 broke the old head matcher purely by appending a declarator to its
  `let`. Every state update is an insertion at a dispatch point: after a run of
  `case` labels (`js.dispatch`), or by wrapping the dispatch test itself
  (`(test&&((update),!0))`, which preserves the test's value exactly and so
  survives any surrounding composition). The arm bodies are never matched.

  **`transcript-signature` and `inline-extras`** are one hand-off and two
  steps: the first threads `streamingThinking` into every renderer whose
  signature carries `messages` and `streamingToolUses`, inserting before the
  latter — an insertion point inside the identity, for `prop-threading`'s
  reason. The signature was a trio until `showAllInTranscript` was measured to
  discriminate nothing on any build in the corpus: a third conjunct that was
  find-anchor, identity and insertion point in one, the exact triple role
  `agentDefinitions` held when 2.1.235 retired it. Deriving the renderer from
  its consumer instead — the scope that binds what the extras memo computes
  over — was measured and rejected: the memo's receiver is a react-compiler
  memoized *local* on every current build, with real dataflow between it and
  the signature, and a dataflow pass is a tool this project deliberately does
  not build. The second step rewrites that renderer's
  extras memo to draw the live block in order. What the memo reads is resolved
  in **its own scope** — the prop its enclosing function was handed — rather
  than carried over from whichever renderer the first step happened to see
  last: a second component with the same four props donated its local to the
  first one's memo, spliced an identifier that component cannot see, and
  reported both steps green. The memo itself is found by what it computes — an
  arrow whose whole body is a `flatMap` that wraps each streaming block as a
  message, and the call that takes that arrow — never by the memo hook's name.
  The hook is one more minified spelling: the monolith reaches it as
  `<React>.useMemo(...)` (the property name survives a member read), but a
  code-split module imports it under a local (`import{useMemo as te}` → `te(...)`),
  so keying on `useMemo` read a build that memoizes exactly the same way as one
  with no memo at all, and the rewrite reuses the memo's *own* callee rather than
  re-spelling `.useMemo`. What a claim about the *text* around the block
  (`.contentBlock]}`) only approximated is the computation itself: one sibling
  property beside `content` read as the whole thing being gone. The wrapped
  block is asked by membership among the element's *possible values*
  (`js.values` — the same value routing the codex resolvers read), never asked
  to be the read: 2.1.246 minted stable ids for streamed blocks
  (`ce?{...S.contentBlock,id:V}:S.contentBlock`), and the exact-node question
  read the same wrap one choice deeper as no wrap at all — nine required steps
  to zero, every anchor count standing. The rewrite reuses upstream's callback
  and dependencies verbatim — the callback invoked per entry with the same
  three arguments `flatMap` hands it, each dependency spread rather than
  transcribed — because a rebuilt copy is a copy free to shed whatever
  upstream adds next: the re-spell this replaced would have silently dropped
  the 2.1.246 minting, and with it went the uuid-stamp identity that re-spell
  had to prove and the reuse no longer asks.

  **`display-mode`** defaults the request's thinking display to `"summarized"`;
  without it the API only streams summary text when the `showThinkingSummaries`
  setting is on. Two shapes used to be spelled out — the inline env check and
  the 2.1.216 form that hoists it into its own variable behind extra
  feature-helper calls. They are one edit: the display value gains a default,
  and whatever guards reach it are untouched because they are never matched.
  The env-var *name* is the witness; whatever reads it is never described, so a
  hoisted `process.env` (the same migration that killed `org-label` on 2.1.228)
  costs nothing here.

### Subagents — `agents.py`

- **`subagent-prompt`** — show the Prompt block outside transcript mode.
  Every gate is the same conjunction — *in transcript mode, **and** there is a
  prompt* — and dropping the first half is the whole patch. Both halves are
  named by upstream: the transcript flag is a parameter property
  (`isTranscriptMode`), and the prompt is whatever the component passes as
  `prompt`. That pairing is what keeps the rewrite off the neighbouring
  `transcript && content && …` conjunction in the same component, which gates
  the agent's *output* and is not this patch's business.

  Three matchers used to spell three appearances of the one conjunction and
  reached three of the four; the fourth — the prompt render in the
  progress-messages component — was left gated while that component's *empty
  state* was un-gated, so the block still never appeared there. One rule reaches
  all four (`3/3` → `4/4` on every build in the corpus).

  What counts as the prompt is every identifier *inside* the value passed under
  that name, not a value that is one. The progress-messages component has a
  single `prompt:h` pair feeding two of the four gates, so one defensive `??""`
  would take that component's Prompt block *and* its empty state with it, at
  `2/4` and green — the very bug the fourth matcher was added to fix. Reading
  through the expression is strictly weaker and finds the same four gates on
  every build in the corpus.

  Neither half is asked to be on a particular *side* of the `&&`, and both are
  asked to be the variables they look like. Requiring the flag on the left lost
  a whole Prompt block the moment one gate was spelled the other way round
  (`3/4`, green); and a name is only a spelling until you say which scope it
  belongs to — a callback inside the component taking its own `b` matched the
  component's `b`, and the conjunction that got rewritten was the callback's
  own. Both rules find the same four gates on every build in the corpus.
- **`subagent-models`** — write the chosen model into each overridden built-in
  definition (discovered as above): rewrite the `model` literal when the
  definition has one, insert `model:"…",` before a property it must carry when
  it doesn't — a new property before an existing one is valid in any object
  literal, so nothing here looks for where the definition ends. Discovery runs
  once and every edit is committed together, so no offset can shift under
  another.
  Every requested override is a **required step**: each one reaching this patch
  has already been validated against the bundle by whichever surface asked for
  it, so a pin that cannot be written is not a shape this build lacks — it is
  the asked-for change failing, and the patch is dropped rather than shipping a
  manifest that claims it. An override whose target the definition already
  carries counts as landed: the step is judged on what it achieved, not on
  whether bytes moved.

  **The bypass:** one helper ignores the definition's model for a single pinned
  agent (Explore today) —
  `function f(def,main){if(def.agentType!==X.agentType||def.source!=="built-in")return def.model;…;return g(main)?PIN:"inherit"}`.
  Its **guard** identifies it and names the pinned agent (resolved by following
  `X` back to the definition assigned to it) — as a comparison of two nodes,
  never as the phrase `.source!=="built-in"`, which also asserts which side
  upstream writes the string on and read `"built-in"!==e.source` as no helper
  at all. Following `X` back has one right answer or none, too: two definitions
  held under one minified name are two, and taking the first mapped the bypass
  to the wrong agent, left the real override inert, and fired no note because a
  bypass *was* found. Its **body** is what gets
  replaced, and upstream keeps growing it — 2.1.217 inserted a
  `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP` escape hatch in the middle, which
  silently cost every Explore override until the body matcher learned to skip
  intervening statements. The body is a node now, so there is nothing left to
  skip. Resolving the agent from the guard alone is why a future body reshape
  costs nothing at all: the body is replaced whole, however upstream grows it,
  and `bypass:<agent>` stands in the report as the record that an override was
  at stake.

  If the **guard** goes too there is no step to fail — nothing left names the
  agent — and no way to tell "upstream stopped pinning" from "the guard
  reshaped". That one is a note instead, on a patch that stays green; it is why
  notes print on green runs. Each helper is carried with the body node to
  replace, so identifying one helper and rewriting whichever one a fresh search
  finds first — how you neutralise an unrelated helper and report success — is
  not expressible, and a build that pins two agents gets both handled.

### Codex — `codex.py`

- **`codex-models`** — make Claude Code accept and show the chosen Codex models,
  and divert *only* those models' requests to the localhost gateway.
  Up to eight sub-steps — the count depends on what was chosen: `--codex opus`
  would be seven (no family shortcut, so no `general-resolver` step), a model
  with no context window six (no `context` step), a family model with a window
  eight. With nothing chosen the patch is a no-op, like
  `subagent-models`. The ids and the port are ordinary patch configuration
  (`Options.codex_models` / `codex_port`) — chosen in the menu or with
  `--codex`/`--codex-port`, never read from a store of the patch's own.

  | step | what it changes | how it is found |
  |---|---|---|
  | `enum`\* | the Task tool's `model` enum, so a subagent can be pinned to a Codex id | `agents.model_enums` — the same arrays `discover_models` reads |
  | `validator`\* | the known-model array — it gates *resolution*, not just acceptance | the array whose strings *include* `sonnet`/`opus`/`haiku`/`opusplan` — membership, never their order |
  | `resolver`\* | the override resolver (managed `availableModels` only) | the `"best"` arm whose `switch` *can* answer `null` for an unknown model |
  | `general-resolver`\* | the resolver every ordinary request uses | the `"best"` arm whose `switch` has no answer of its own for one |
  | `redirect`\* | swaps the request origin to `127.0.0.1:<port>` | the `buildRequest` method that builds a URL (the others — two to four across the corpus — only delegate to `super`) |
  | `picker` | the `/model` list | the function every row list is handed to: it loops, adds, and returns its first parameter |
  | `context` | the real context window | the function that *reads* `CLAUDE_CODE_MAX_CONTEXT_TOKENS` as a member and returns what it read, taking the model as a parameter; the table goes before its first statement |
  | `registry` | the binary's own model table — the status-line name, effort capabilities, `/advisor` eligibility | the object carrying both `models` and `aliases`, confirmed by its entries carrying `id`/`family`/`display_name` |

  \* required — without any one of them the feature is dead.
  `picker`, `context` and `registry` are refinements: absent, you can still
  type `/model <id>` and get the 200k default under the model's raw id.
  `context` has no step at all when no chosen model reports a window — there
  is no rewrite owed, and reporting that as either a missing shape or a missed
  rewrite would blame the build for having nothing to do.

  The first four steps *register a name in a list*, so each applies to **every**
  list of its kind and counts one candidate per list — and a name already there
  counts as landed, the same judged-on-achievement rule `max-effort` and
  `subagent-models` follow. The last four *rewrite one site*, so each asks
  `js.only` for it and raises on a build that offers two. That difference is not
  bookkeeping: taking the first match let a prepended
  `["sonnet","opus","haiku","opusplan"]` absorb the ids while the real array
  kept none of them, and two throwaway `case"best":` switches absorb both
  resolvers, at eight of eight steps green.

  **`context` is why "found *a* function" is not identity.** Climbing from any
  occurrence of the env-var name took the first one in the bundle, which is a
  *key* in esbuild's export map — so the climb left the resolver entirely and
  landed on the CommonJS module wrapper, whose five parameters
  (`exports, require, module, …`) passed every check. The window table was
  spliced into byte 91 of the bundle, keyed on `String(exports)`, on every
  build in the corpus, reporting `candidates=1 applied=1` and answering
  nothing. Requiring a *member read* is the same discipline `spinner-tips`
  uses; requiring the function to return what it read is what tells the
  resolver from the two other readers — the compaction override answers with it
  but takes no model to key on, and the unknown-model warning reads it only to
  decide whether to complain.

  **An id is a model's whole identity.** There is no second name to carry: the id
  is what `--codex` takes, what the manifest records, what the enum and the
  picker and the redirect array hold, and what a diverted request already names
  by the time the gateway sees it. Everything else is derived — the display name
  and the window from the plan, the shortcut from the id — so nothing downstream
  keeps a mapping that could disagree with the bundle. That is why the gateway
  needs only the port, and it is structural rather than a rule to remember:
  there is no map left to read.

  **The picker's label rule is one rule.** Every row's label is its handle
  *spelled as a name* — `_display_name` turns `sol` into `Sol` and
  `gpt-5.6-sol` into `GPT 5.6 Sol` — because the binary's own rows are named,
  not slugged. Labelling id rows with the raw id instead made them the only
  entries in the list wearing a different sort of name than their neighbours
  (`gpt-5.6-sol` sitting under `Opus`). Two label rules is what let that
  through, so there is one.

  **The registry is the binary's own model table, and the sharpest splice
  here.** Everything Claude Code knows about a model it did not hardcode a
  check for lives in one embedded object — `models:[{id, family, display_name,
  provider_ids, context, capabilities:[...], default_effort, advisor_rank,
  ...}]` plus the `aliases` map its resolvers read — validated by a zod
  `safeParse` whose fallback is **empty** (`models:[]`): one malformed entry
  strips every model, Claude's included, of its metadata. So the step emits
  only fields the schema declares — the required four (`id`, `family`,
  `display_name`, `provider_ids.first_party`) plus values with something true
  to record. `default_effort` is deliberately not among them: the binary
  resolves a missing default as `high` (`?.default_effort??"high"` — the very
  default its own flagships declare), so omission makes `/effort auto` and an
  untouched session mean on these models exactly what they mean on Opus,
  instead of importing Codex-the-product's own default (`low` on sol). An
  entry is what turns "accepted" into "first-class": the status line resolves
  `display_name` through it (raw id without one), `/advisor` eligibility is
  exactly "has an `advisor_rank`", the effort gates read `capabilities`, and
  surfaces nobody has enumerated inherit the same answers. `/advisor sol` on a
  freshly baked binary is the cheap end-to-end check that the entry parsed.

  **What the entry deliberately leaves out.** `pricing` and
  `max_output_tokens` (readers guard for absence; a subscription has no
  per-token price; the cap never reaches the wire), `context` (the window
  resolver ends on a flat 200k without consulting the registry — the `context`
  step stays the one home for the window), and the `aliases` /
  `latest_per_family` maps (shortcuts already resolve through the spliced
  arms; a second mechanism would be a second home — and `latest_per_family`
  feeds Claude's own system-prompt text). The `/model` picker does not iterate
  the registry either — its rows are hand-built per family upstream, which is
  why `picker` exists — and our row push is the same `{value,label,description}`
  + `.some()` idiom the binary itself uses for `ANTHROPIC_CUSTOM_MODEL_OPTION`.

  **Capabilities can only say yes.** The binary reads an *absent* capability
  as "ask the provider fallback", which on the first-party API is permissive —
  upstream's own choice for models it does not know. That is why the `/effort`
  menu offers the whole ladder on any imported model, and why an entry cannot
  *hide* a level; hiding would mean splicing the per-level exclusion chains,
  a new matcher surface bought for a cosmetic win. Which levels a model
  actually runs is the backend's per-model ruling, refused with a structured
  400 (`param:"reasoning.effort"`, `code:"invalid_value"`) before any
  generation — measured: gpt-5.5 runs `xhigh` and refuses `max`. Baking that
  ruling would be a copy free to go stale between bake and runtime, so the
  gateway clamps off the refusal itself (`translate.clamp_effort`): one rung
  down per retry, mirroring Claude Code's documented "highest supported level
  at or below" rule for its own models. A menu that over-offers costs one
  extra round-trip, never a dead turn.

  **It runs before `subagent-models`, and that ordering is load-bearing.**
  `enum` registers the ids in the very schema `discover_models` reads, so a
  subagent pinned to a Codex model is offered exactly when that model is really
  in the bundle. Registering *after* meant `subagent-models` had to be told
  about the ids out of band, so it landed a pin whether or not
  `codex-models` did: drop `codex-models` for a drifted anchor and the binary
  kept `model:"gpt-5.6-sol"` on an agent, pointing at a model nothing had
  registered, with the manifest asserting the override. Now the dropped patch
  takes its pins down with it.

  The two resolvers are told apart by **what each answers for an unknown
  model, never by their minified names or by a statement form** — `null` is
  among what the override one's default *can* answer, read off the grammar's
  own value routing (a ternary answers with either branch, `||`/`??` with
  their right side; 2.1.234 wrapped the same rejection in a dead recognizer,
  `return vXu(e)?xVe(t):null` with the stub answering `!1`, and the exact
  spelling read a behaviourally identical build as the resolver being gone),
  while the general one has no answer of its own — nothing in its default
  scoped to return or throw — and falls through to handing the name back.
  Both identities are asked positively, of switches whose labels carry the
  built-in models (the membership `validator` asks of its array, asked of the
  labels — a throwaway `case"best":` elsewhere is nothing, not an arm to
  classify), and an arm answering neither *raises* instead of swelling the
  other side: under the complement this replaced, a reshaped override slid
  silently into the general list, and only the required step's zero was left
  to speak. Braces around an arm are no discriminator either: they were, once,
  and a `let` in the general resolver's `case"best"` was enough to make every
  family shortcut vanish with the patch still green and seven of seven steps
  applied. The `buildRequest` methods and the `models`-shaped objects are
  likewise separated by what they *do* and *carry*.

  **Two resolvers, and why both.** The **general** one turns `opus`
  into `claude-opus-4-8`, its return value *replaces* the model before the
  request is built, and it passes an unknown-but-valid name straight through
  (`return e`). The **override** one runs only when managed `availableModels`
  are active and defaults to `null`, not passthrough. An id needs neither arm
  (identity *is* passthrough); a **family shortcut** (`sol` → the newest
  `gpt-<ver>-sol`) needs one in both, and the general one is what makes it work
  on the ordinary path. Neither is named here on purpose: they are minified
  locals (`Ei`/`J9n` on the build this paragraph was first written against,
  something else on the next one), and a doc that spells one is the same mistake
  as a matcher that does.

  **The shortcut gate.** Shortcuts are registered only when the general resolver
  is found. Absent it, none are registered anywhere and the ids — which need none
  of this — carry on. Accepted-but-unresolved is the failure worth engineering
  against: it leaves the redirect (which matches ids only), reaches Anthropic as
  an unknown model, and 404s.

  **Why the "already added?" checks are bounded.** The resolver check reads
  only the sibling arms *following the insertion point*, never the whole
  bundle: a short word like `auto` occurs as `case"auto":return` in
  stock code, so a global check would skip its arm while the id arms still
  marked the step applied — a shortcut that resolves nowhere. The picker drops
  its build-time check for the same reason and leans on the runtime `.some()`
  guard it injects. Both resolvers are spliced by one function reading that one
  check, so idempotency is the same sentence twice rather than two mechanisms:
  the arms already there are the arms not added again.

  **Routing knows nothing about shortcuts.** The redirect tests `body.model`
  against the baked id array, and the context table is keyed the same way — by
  the time either runs, the general resolver has already rewritten the shortcut.
  Measured on the
  wire: `claude --model sol` arrives at the gateway as `"gpt-5.6-sol"`. That is
  why a drift in the shortcuts costs shortcuts and nothing else.

  **A diverted request still carries Claude Code's auth header.** Measured, not
  assumed: point a listener at the gateway port and a Codex turn arrives with
  `Authorization: Bearer sk-ant-oat01-…` and the whole prompt. The gateway
  ignores it and never forwards it, so the exposure is to whatever holds the
  port. Stripping it from `redirect` does **not** work — don't retry it blind.
  `options.headers` is the last source `buildHeaders` merges and its merge
  treats `null` as delete, so setting a null (or an inert value) there ought to
  win; neither reaches the wire. With the injected block proven to run — a
  marker spliced into the replacement URL came through in the path — a probe
  header set on the options object *and* on the local copy was absent from the
  request both times, so something between `buildRequest` and `fetch` discards
  `options.headers` on this build. A real fix needs its own anchor further
  down, in `prepareRequest` (it receives the final `Headers` and the URL): a new
  required step and new matcher surface, deliberately not taken for 0.2.0.

### Chrome & branding — `chrome.py`

- **`spinner-tips`** — force spinner tips off. Anchor: the setting name. Every
  *member read* of `spinnerTipsEnabled` that a **gate** consults is replaced by
  `!1`, which satisfies the
  enabling guard (`f().x===!1` → true) and the enabling expression
  (`v.x!==!1` → false) by arithmetic, and satisfies whatever third path upstream
  adds next without being told about it. *Value-flip.*
  The two paths this replaced each had their own matcher and each counted
  candidates off the setting name rather than off itself, because either could
  drift while the other carried the patch to green with tips still showing.

  One rule reaches every path *of that kind*, and the kind has three edges,
  each of them the difference between reaching every path and reaching past
  them:

  - The name is a **whole name**. `spinnerTipsEnabledAt` starts with it and is
    another setting; rewritten as this one it left `!1>0`, which counts,
    verifies and parses.
  - A read is an **expression**. An assignment *target* is a place, and `!1=!1`
    is rubble the gate reports as this patch broken, taking every other read
    with it — so a write is skipped. A destructured read
    (`let{spinnerTipsEnabled:x}=settings()`) binds a name, offers nothing to
    replace, and would be missed quietly; the count of reads is only ever the
    count of reads there are.
  - A **settings row is not a gate**. `/config` shows the setting as a row's
    `value` and hands the user's answer to its `onChange`, and answering "off"
    there disables nothing: it freezes the row at `false` and, because the row
    toggles by negating what it shows, makes pressing it write `true` into the
    user's `settings.json`. That one read is deliberately skipped — a patch that
    reaches out of the binary and into the config has left its blast radius, and
    `restore` cannot follow it there.

  What the whole rule costs is bounded by the anchor: no read rewritten is the
  patch failing, and the anchor `doctor`
  prints then separates a renamed setting (`0`) from one that stopped being read
  as a member (still there).
- **`version-marker`** — append `\n<suffix>` after `}.VERSION} (Claude Code)`
  (default `(patched)`, customisable via `--suffix`; escaped for the template
  literal it lands in).
- **`branding`** — rename visible `Claude Code` startup/help strings to a
  chosen name. Three sub-steps, and the line between them and everything else
  is the point: `badge` is a render whose **whole** bold content is the product
  name, `styled` is the themed one-liner (`theme("claude",x)("Claude Code")`),
  `welcome` is the `Welcome to Claude Code…` sentence. The bundle holds sixteen
  exact `"Claude Code"` literals and many more inside prose — a notification
  title, a native-host id, a fallback name, sentences that end up in system
  prompts — and this patch renames the badge, not everything that mentions the
  product. A rule of "every string containing the words" would be shorter and
  wrong. Whichever JSX runtime the build emits is not mentioned: a props bag
  that *names* its children is the runtime that puts them there, and only a bag
  that names none leaves the content standing in the arguments after it. Which
  is not the same as reading both at once — the automatic runtime's third
  argument is the element's **key**, so counting it as a child stopped a keyed
  badge from being a badge, and 2.1.232 already emits 448 three-argument
  `jsx`/`jsxs` calls (none of them a badge yet).
  *Whole* content, not "one of the children is the name": upstream already
  splits prose into JSX children whose first child is the bare product name
  (`["Claude Code","'","ll be able to read…"]`, twice), and bold is one prop
  away from either. Everything the badge draws is the name or upstream's own
  padding — which finds every badge on every build in the corpus: three sites
  through 2.1.216, four from 2.1.217 (upstream added one), and the count is a
  measurement rather than a promise.
  `badge` is required; see [Expectations](#expectations--why-a-green-tick-means-something).
  On by default, deriving `<username>'s Code`; `--brand NAME` names it
  explicitly, and selects the patch when it is not already in the set.
- **`org-label`** — replace or hide the welcome screen's third segment
  (`model · plan · <organizationName>`; for a personal claude.ai account the
  org name *is* the account email). Anchor: the one conditional whose true branch
  interpolates `.organizationName` into a template and whose false branch does
  not — that false branch is upstream's own no-org line, and the replacement is
  **built from it**: taken verbatim to hide the segment, or with one more
  segment appended to bake a label. The separator that introduces that segment
  is *copied out of the line itself* — the bytes between its last two
  interpolations — so nothing here spells the separator, the model or the plan,
  and a build that renames either interpolation, writes `\xB7` as a raw `·`, or
  changes the separator outright, changes nothing. Spelling it put ours beside
  theirs the moment the two differed: a build using `/` baked
  `` `${model} / ${plan} \xB7 Ada` ``, half its punctuation and half ours.
  `IS_DEMO` is the node found inside the condition, never a path spelled out
  (`process.env.IS_DEMO` through 2.1.227, `J.IS_DEMO` on 2.1.228, `Q.IS_DEMO`
  on 2.1.229) — spelling that path is what broke this patch on 2.1.228, a
  build before anyone noticed. The `!{read}&&` prefix is consumed,
  not left standing: `&&` binds tighter than `?:`, so upstream's expression is
  `` (!demo && hasOrg) ? withOrg : noOrg `` — which means its demo value is the
  *no-org string*, not `false`, and replacing only the ternary would leave
  `` !{read}&&`template` `` and make demo mode evaluate to `false`. A label is
  therefore `` !{read}?baked:no-org ``, measured to reproduce upstream's demo
  output exactly. An empty label (`--org-label` with no value — empty is the
  *value* "hide", not
  "unset") emits that false branch verbatim, so the separator leaves with the
  segment; a label takes the org text's place, escaped for the template
  literal it lands in. Candidates count off the org name **interpolated into a
  template** — the composition, found as the node it is rather than as text, so
  the `/status` row `{label:"Organization",value:r.organizationName}` is not
  mistaken for one. A reshaped conditional then reads as
  `candidates > 0, applied == 0` — something to repair — rather than the zero
  that would equally mean upstream retired the segment. Being *in* a
  substitution is the whole claim; ending one was a claim about how the
  substitution closes, and `${x.organizationName??""}` — the same composition,
  one defensive operator later — then reported the one state the table above has
  no row for: `candidates == 0, applied == 1`, the anchor "gone" and the rewrite
  landed. Only this line is
  touched: the `/status` Organization/Email rows, the login screen, and the
  org's startup message (`"Message from <org>:"`) still show the real account.

  **2.1.246 retired the surface.** Upstream deleted the welcome-banner variant
  that composed the segment; the surviving banner draws `model · billing` with
  no org anywhere — the hidden state an empty `--org-label` asks for, now
  upstream's own default. That is a semantic change, not a matcher to repair
  (the `help-title` precedent), and it is *absence*, reported apart from broken
  (CONDUCT): the patch declares its surface (`Patch.absence`, answered by the
  same `_org_segments` its candidates count off, so the two cannot disagree),
  and every surface derives its answer from the bundle in hand — the menu shows
  the row dimmed (`not on this build`, unselectable), an explicit `--org-label`
  is refused at the front door with the same sentence, a cached replay skips it
  with a note, and `doctor` prints a `-` row apart from ✓/✗ and stays green.
  Nothing is keyed on a version, so a build that composes the segment again
  un-dims the row with no code change. What absence cannot tell apart is a
  composition respelled out of the locator's sight — that build would read as
  absent, visibly dimmed on a screen that still draws the segment — and the
  sweep over the corpus, where thirty-four builds carry the surface, is what
  keeps the locator honest.

## Removed patches

Kept here so nobody reintroduces them without knowing why they left:

- **`word-diff-bg`** — as of 2.1.216 the word spans are nested inside a row
  element that already carries the line background; the fallback could never
  change a pixel. Confirmed redundant in live A/B.
- **`installer-label`** — its target string left the bundle in ~2.1.186.
- **`redacted-thinking`** — untestable against the real API (no way to elicit
  a `redacted_thinking` block), and the native-only tool keeps its surface to
  what can be verified.

### Sub-steps removed with the move to the tree

All measured against the corpus — the pristine backups patch-cc has saved,
enumerated with their hashes in [corpus.md](corpus.md). Each removed matcher hit
**zero** sites on **every** bundle there, so none was carrying a build anybody
can still be running; each was carried as "kept for older builds", which is a
claim the corpus disproves. The verbatim matchers were re-run from `HEAD` over
the whole corpus after the move, and they are still zero everywhere. The
measurements this section quotes were taken during the move across
2.1.210 → 2.1.233 (2.1.230 was never published) — a span the corpus has since
grown to hold in full, so they are re-checkable rather than historical, and
re-running `doctor` over it is one command (below).

- **`live-thinking` / `reducer-legacy`** — the pre-2.1.138 reducer with
  positional parameters. Its removal collapsed the `reducer` variant *group*
  into one required step, because there is one head shape left to find and its
  options bag is reached from the parameter it destructures rather than matched.
- **`live-thinking` / `memo-cache`, `memo-removal`, `linger`, `bottom-row`** —
  legacy render cleanups upstream has since done itself.
- **`branding` / `help-title`** — the help screen's
  `title:` + `defaultTab:"general"` shape. Zero on all 23; the string it
  wanted (``title:`Claude Code v${…}` ``) is on none of them either — that
  screen's title is a plain `"Help"` now, which is a semantic change and not a
  matcher to repair.

Four counters moved *up* against `HEAD`, and none is a widening — every one is
a site the old matchers should have reached and didn't:

- **`subagent-prompt` 3 → 4 applied, on every build** — the fourth prompt
  render, above.
- **`subagent-models` candidates up on every build** (7 → 9 on 2.1.216, 7 → 10
  on 2.1.228, and so on to 11 on 2.1.233), `applied` moving in lock-step — the
  discovered-agent count rising as `discover_agents` now resolves an `agentType`
  that is a *hoisted constant* (`agentType:Ehr` with `Ehr="worker"` elsewhere)
  through its value, where the old literal-only read dropped it. Every extra
  candidate is a real built-in agent the override patch can now pin, not a
  looser match, and the two numbers move together so no build reads partial.
- **`spinner-tips` 3 → 4 applied, on 2.1.210 and 2.1.211 only** — those two
  carry four gate reads and the old pair of matchers reached three of them
  (`candidates` was already 4, so the miss showed as `4/3`). From 2.1.212 the
  count is 3 → 3: upstream dropped a read, and nothing moved. The settings-row
  read is not among these on any build — it is skipped deliberately, so
  `/config` still shows the toggle as the user's own setting.
- **`org-label` 0 → 1 applied, from 2.1.228 on** (228, 229, 231–233) — the
  builds that hoisted `IS_DEMO` behind a local: `process.env.IS_DEMO` drops to
  zero occurrences at 2.1.228 and stays there, so the old matcher, which spelled
  that path, found each build's conditional but rewrote nothing (`1/0`) while
  the node-based one reads `J.IS_DEMO`/`Q.IS_DEMO` and lands. Five builds, not
  the one this once claimed.

Nothing moved *down*. Everything that moved *up* is one of the four above; the
current per-build counts are `docs/corpus.md`, recomputed from the binaries
themselves whenever `doctor` runs.
