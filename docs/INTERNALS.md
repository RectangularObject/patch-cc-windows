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
table describing them, and a trailer.

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

The module we patch is the entrypoint, which the offsets struct names by index
(`entry_point_id`) — the same index Bun itself resolves it by. Its `contents` is
the JS we edit. Its *name* is upstream's to change and we never read it:
2.1.229 renamed it `/$bunfs/root/src/entrypoints/cli.js` → `/$bunfs/root/cli`.

Code: `src/patch_cc/bun/blob.py`.

## The bytecode, and why we drop it

The entry module also carries precompiled Bun **bytecode** — more than half the
binary. Every other module has none.

Any edit to `contents` invalidates that bytecode; Bun detects the mismatch and
recompiles from source at launch. So keeping it buys nothing:

| binary | size | startup |
|---|---|---|
| original (valid bytecode) | 323 MB | ~100 ms |
| patched, bytecode kept | 323 MB | ~650 ms |
| patched, bytecode dropped | **125 MB** | ~650 ms |

Patching pays the recompile cost either way, so patch-cc drops the entry
module's bytecode (`rebuild(..., drop_bytecode=True)`). The result runs source,
guaranteeing our edits are authoritative, and — on Linux — is smaller by
exactly the bytecode.

The size figures are the **ELF** path: the `.bun` section is rewritten in place,
so the dropped bytecode is genuinely reclaimed (`container.verify` refuses a
Linux write that did not shrink). On **macOS** the file keeps its original size:
`macho.py` grows a segment but never shrinks one, so the freed bytes stay as
dead space. The binary still runs correctly (the bytecode is gone), it is just
not smaller — reclaiming it means shrinking the Mach-O segment and re-laying
`__LINKEDIT`, which is not done yet.

Those are **2.1.232's** Linux numbers, and they are a measurement rather than a
promise: the same table read 267 / 113 MB against 2.1.216, because the bytecode
grew from 154 MB to 198 MB in the fifteen builds between them (2.1.230 was
never published). Read the current pair off any
binary with `patch-cc status` rather than off this table.

Every write asserts the binary it produced carries `bytecode == 0`
(`container.verify`, beside the round-trip check), and `patch-cc status` reports
the field for an installed one. `doctor` cannot: a dry run is handed a *clean*
bundle, which still has its bytecode by definition. If a future Bun build makes
bytecode authoritative over source, that assert is the tripwire — every patch
would silently no-op otherwise.

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

Every patched bundle ends with a single comment line — the one description of
its shape; [PLAYBOOK.md](PLAYBOOK.md) covers what it means for matcher health:

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
  wrote and asserts it equals what it meant to write.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused outright — there is nothing clean to start from, and our edits change
  lengths, so a second pass would corrupt rather than update. `restore` or a
  reinstall are the only honest fixes; there is deliberately no override.
