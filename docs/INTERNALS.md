# Internals

How patch-cc gets from a Claude binary to a patched, smaller one.

## The shape of a native Claude binary

Claude Code ships as a [Bun](https://bun.sh) single-file executable. The whole
app — a ~20 MB minified JS bundle plus a few asset modules — is embedded in the
binary:

- **Linux**: an ELF section named `.bun`
- **macOS**: a Mach-O section `__BUN,__bun`
- **Windows**: a PE `.bun` section (not supported here)

Inside that section is a *Bun module graph*: a flat arena of payloads, a module
table describing them, and a trailer. Before 2.1.242 the graph was the app in one
module plus a few asset modules; since then the app is **code-split** across
~1,300 `chunk-*.js` modules that the entry lazily imports (see
[the split](#the-21242-split-and-the-patchable-surface)). Either way patch-cc
treats every module the container declares to be JavaScript as one surface.

```
.bun section
└── [u64 size prefix]           (u32 on Bun < 1.3.4)
    └── Bun blob
        ├── payload arena       name / contents / sourcemap / bytecode / ... bytes
        ├── module table        N records × 52 bytes (36 on old Bun)
        ├── compileExecArgv
        ├── offsets struct       32 bytes: byteCount, modulesPtr, entryId, argvPtr, flags
        └── "\n---- Bun! ----\n"  15-byte trailer
```

Every pointer is a `(u32 offset, u32 length)` pair relative to the blob start,
and pointers live in only two places: the module table and the offsets struct.
That is what makes rewriting tractable — move a payload, fix the handful of
pointers that describe it.

A module record (new 52-byte format) is six such pairs — `name`, `contents`,
`sourcemap`, `bytecode`, `moduleInfo`, `bytecodeOriginPath` — followed by four
`u8` flags (`encoding`, `loader`, `moduleFormat`, `side`).

Code: `src/patch_cc/bun/blob.py`.

## The 2.1.242 split, and the patchable surface

Through 2.1.241 the entrypoint module *was* the app: one ~28 MB `contents`
carrying every line patch-cc anchors on. 2.1.242 turned on Bun code-splitting
with lazy loading, and the shape changed under the tool:

| build | modules | entrypoint `contents` |
|---|---|---|
| 2.1.241 | 11 | 28,249,679 bytes (the whole app) |
| 2.1.243 | 1,385 | 19,952 bytes (an argv shim) |

The entrypoint is now a ~20 KB shim that parses argv and lazily
`import()`s the app across ~1,300 `/$bunfs/root/chunk-*.js` modules; ~46 MB of
JS, the largest chunk 7.3 MB. The code did not disappear — every anchor is still
in the binary — but it left the one module patch-cc used to read, and it does not
concentrate in a single chunk (`branding` spans a dozen modules, `org-label` ten,
`spinner-tips` six).

So the patchable surface is **every module the container declares to be
JavaScript**, discovered the way the entrypoint itself is discovered — off the
artifact, never hardcoded. A module's *loader* (the second trailing flag) is how
Bun decides whether to compile it as source or hand it over as opaque bytes, and
the entrypoint is by definition the JS Bun runs, so its loader *is* the JS loader
(`Blob.js_modules`). The asset modules (the native addons, the bundled
`mermaid`/`hljs`, the HTML template) carry other loaders and are left alone. A
pre-split build is the one-module case of this — `js_modules()` returns just the
monolith — so [`js.Source`](PLAYBOOK.md#the-many-module-surface) spans one module
or a thousand through the same code.

The entrypoint still matters for one thing: it is where the manifest lives and
what `status` reads, named by the offsets struct's `entry_point_id` — the same
index Bun resolves it by. Its *name* is upstream's to change and we never read
it: 2.1.229 renamed it `/$bunfs/root/src/entrypoints/cli.js` → `/$bunfs/root/cli`.

## The bytecode, and why we drop it

Modules carry precompiled Bun **bytecode** — most of the binary. Before the
split only the entry module had any (~half of it); the code-split builds carry it
on nearly every chunk (232 MB of 377 on 2.1.243, across ~1,375 modules).

Any edit to a module's `contents` invalidates *that module's* bytecode; Bun
detects the mismatch and recompiles that module from source at launch. So keeping
a stale copy buys nothing — the recompile is paid either way — and dropping it
reclaims the space and guarantees our edits are what runs. patch-cc drops the
bytecode of exactly the modules it edited (`rebuild` over `changed_modules`) and
leaves every untouched module its bytecode and its fast start. On Linux, where
the ELF section is rewritten in place, the binary is smaller by exactly the
edited modules' bytecode.

Measured on 2.1.243, the full patch set:

| binary | size | bytecode | startup |
|---|---|---|---|
| pristine | 378 MB | 232 MB (every module) | ~13 ms `--version` |
| patched (edited modules' bytecode dropped) | **295 MB** | 150 MB (untouched modules) | ~15 ms |

The 83 MB reclaimed is the ~45 edited modules' bytecode; the rest stays, which is
why a split-build patched binary is smaller but not the *half* a patched monolith
was. The recompile is now per lazily-imported edited module rather than the whole
app at once, so startup barely moves. Read the current figures off any binary
with `patch-cc status` rather than off this table — the bytecode total grows every
few builds.

The size story is the **ELF** path: the `.bun` section is rewritten in place, so
the dropped bytecode is genuinely reclaimed (`container.verify` refuses a Linux
write that did not shrink by about that much). On **macOS** the file keeps its
original size: `macho.py` grows a segment but never shrinks one, so the freed
bytes stay as dead space. The binary still runs correctly (the bytecode is gone),
it is just not smaller — reclaiming it means shrinking the Mach-O segment and
re-laying `__LINKEDIT`, which is not done yet.

Every write asserts each **edited** module carries `bytecode == 0` in the binary
it produced (`container.verify`, beside the round-trip check), and `patch-cc
status` reports the total for an installed one. `doctor` cannot: a dry run is
handed a *clean* bundle, which still has all its bytecode by definition. If a
future Bun build makes bytecode authoritative over source, that assert is the
tripwire — every edit would silently no-op otherwise.

## Writing it back without ballooning

`.bun` is the last *allocated* ELF section; only non-allocated metadata
(`.comment`, `.symtab`, `.strtab`, `.shstrtab`) follows it. patch-cc rewrites
the ELF bytes in place:

1. Splice the new (smaller) blob over the old `.bun` bytes.
2. Shift `e_shoff`, `e_phoff`, and the trailing non-alloc sections/segments by
   the size delta.
3. Grow or shrink the containing `PT_LOAD` segment's `filesz`/`memsz` to match.

`.bun` keeps its original file offset. This is deliberately *not* done with a
general ELF library: LIEF rebuilds the binary and relocates `.bun` so its file
offset equals its virtual address (`0x20000000`), which inflates the file to
~715 MB. Raw in-place surgery avoids that entirely.

Guards refuse anything that could corrupt the mapping: allocated sections after
`.bun`, growth into a header table, an unrelated spanning segment, or a
misaligned `PT_LOAD` shift. If any fires, the write aborts rather than guesses.

Code: `src/patch_cc/bun/elf.py`. macOS uses LIEF (`macho.py`) — Mach-O segment
growth is page-aligned and bounded, with no relocation pathology, and every
edit is followed by an ad-hoc `codesign` (mandatory on Apple Silicon).

## The manifest

Every patched bundle carries a single comment line — appended to the **entry
module**, the one module always present and always re-extracted, and the one
`status` reads — describing its shape; [PLAYBOOK.md](PLAYBOOK.md) covers what it
means for matcher health:

```
//patch-cc {"v":1,"tool":"<version>","patches":[...],"brand":...,"suffix":...,
            "models":{...},"org":...,"codex":{"port":8817,"models":["gpt-5.6-sol"]}}
```

Every key after `patches` is a configurable patch's own, declared in one place
(`Patch.setting`) so the manifest here, the cache, and the menu cannot spell it
three ways. Each is written only when *that* patch landed **and** has a value
worth recording, so `status` can never assert a name, marker, or model the
bundle does not contain — and so this is the *widest* the line gets, not its
fixed shape.

That line is why `patch-cc status` can name exactly what is applied: several
patches are value flips (`verbose:!0`) that leave no other trace. A comment
can't collide with code and travels with the bundle through extract/repack.
The menu also reads it to pre-select the current patch set — the binary is the
state.

Each key records what was *asked for*, never what was derived from it. `codex`
carries model ids and a port and nothing else: a Codex model's display name and
context window are already baked into the bundle, and repeating them here would
be a second copy — one that a relabelling upstream could make disagree with the
binary it claims to describe. That is also what makes the manifest the single
home for the gateway port: `codex serve` and `codex status` read it from here
rather than from a store of their own.

## Safety

- Before the first patch of a version, the pristine binary is copied to
  `~/.local/share/patch-cc/backups/`. `restore` copies it back — never an
  inverse patch (insertions cascade, so a reverse diff is meaningless).
- Patching always starts from that pristine copy, so re-applying never stacks
  edits on edits, and an apply where **nothing lands** — every selected patch
  broken, so the manifest would claim nothing — leaves the binary untouched
  entirely (stripping bytecode for nothing would only slow startup). A patch
  that *lands* still writes even where it changed no bytes, because landing
  includes an override the build already satisfies: the manifest records what
  was asked and verified present, so `status` can report it.
- The bundle is parsed, and any syntax error aborts before the binary or the
  backup is touched — the two checks below answer "did we write what we meant
  to", which a corrupt splice satisfies perfectly. The parse is the same one
  the patches locate with, so it costs nothing extra and sits at *every* batch
  of edits as well as on the final bytes: a patch that produces rubble is named
  and dropped rather than aborting the run. See
  [PLAYBOOK.md](PLAYBOOK.md#the-syntax-gate).
- Every write is verified: patch-cc re-extracts the JS from the binary it just
  wrote and asserts every module equals what it meant to write, and that each
  module it edited carries no leftover bytecode to run instead of the edit.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused outright — there is nothing clean to start from, and our edits change
  lengths, so a second pass would corrupt rather than update. `restore` or a
  reinstall are the only honest fixes; there is deliberately no override.
