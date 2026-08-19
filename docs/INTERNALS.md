# Internals

How patch-cc gets from a Claude binary to a patched, smaller one.

## The shape of a native Claude binary

Claude Code ships as a [Bun](https://bun.sh) single-file executable. The whole
app — a ~20 MB minified JS bundle plus a few asset modules — is embedded in the
binary:

- **Linux**: an ELF section named `.bun`
- **macOS**: a Mach-O section `__BUN,__bun`
- **Windows**: a PE `.bun` section

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
guaranteeing our edits are authoritative, and — on Linux and Windows — is
smaller by exactly the bytecode.

The size figures are the **ELF** path: the `.bun` section is rewritten in place,
so the dropped bytecode is genuinely reclaimed (`container.verify` refuses a
Linux write that did not shrink). **PE** reclaims it the same way, for a
different reason — `.bun` is the *last* section in the image, so shrinking it
simply ends the file sooner. On **macOS** the file keeps its original size:
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

### Windows: the easy one

PE (`pe.py`) needs no shifting at all. In a Bun executable `.bun` is the **last**
section by both file offset and virtual address, and the only bytes past it are
the Authenticode certificate — so resizing it moves nothing. Four header fields
restate the size and the image ends where the section now ends:

| field | new value |
|---|---|
| `.bun` `VirtualSize` | the payload length exactly |
| `.bun` `SizeOfRawData` | that, padded up to `FileAlignment` |
| `SizeOfImage` | `align(.bun.VirtualAddress + VirtualSize, SectionAlignment)` |
| `SizeOfInitializedData` | adjusted by the change in raw size |

The guards are narrower than the ELF ones because the claim is narrower, but
each asserts one half of "nothing follows": no later section payload, nothing
mapped above, no COFF symbol table behind us, no data directory reaching into
`.bun`, and a file tail that is the signature rather than something we would
truncate away unexamined.

**The signature is dropped, not updated.** Editing `.bun` invalidates it
whatever we do, and a stale signature is worse than none — it reads as
*tampered* rather than *unsigned*. Nothing is re-signed: macOS signs ad-hoc only
because arm64 refuses to run an unsigned image, Windows runs one happily, and a
self-signed certificate would assert an identity that is not ours. `restore`
copies the pristine backup back, signature and all. `CheckSum` is recomputed for
the same honesty — Windows does not verify it for user-mode executables, so
check it against `imagehlp!MapFileAndCheckSumW`, which is the authority, not our
own arithmetic.

No third-party dependency: PE is pure `struct`, and LIEF stays macOS-only.

### Replacing the file

`container.replace` puts the staged binary in place. POSIX swaps a directory
entry and any running process keeps the inode it already mapped. Windows refuses
to *overwrite* an image mapped for execution but does allow *renaming* one, so a
running `claude.exe` is parked at `claude.exe.patch-cc.old` and the new binary
takes its name — which is what keeps "restart Claude Code for changes to take
effect" the answer on either platform rather than "close it first".

A parked image cannot be deleted until its process exits, so the sweep is the
first thing a *later* replace does, not the last thing this one does — down
there it could never fire, since the only path reaching it runs while the file is
still held. The name takes a counter, because more than one generation can be
mapped at once: patch, start a fresh session as instructed, leave the previous
one open, patch again. Once every slot is taken the patch fails saying so, rather
than hunting indefinitely.

If the new binary cannot take the name after the old one has been parked, the old
one is moved back before the error propagates. If even *that* fails, `claude.exe`
does not exist — so the error says where the working binary is parked and what to
rename it to; nothing else in the tree would tell the user.

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
- Patching never stacks edits on edits: each apply starts from a pristine
  source, so the selected set is always exactly what ends up in the binary. An
  apply where **nothing lands** — every selected patch broken, so the manifest
  would claim nothing — leaves the binary untouched entirely (stripping
  bytecode for nothing would only slow startup). A patch that *lands* still
  writes even where it changed no bytes, because landing includes an override
  the build already satisfies: the manifest records what was asked and
  verified present, so `status` can report it.
- *Which* pristine source: the installed binary while it is unpatched, and the
  kept copy only once it is not. An update replaces the whole binary, so
  "installed and unpatched" means "installed and pristine" — the original of the
  version installed now, which a backup only is for the version it was taken
  from. Those coincide wherever the launcher is version-named and part company
  wherever it is a fixed path (every Windows install, and any Homebrew or npm
  one), where preferring the backup would patch a superseded bundle over the
  current install. An install we cannot *read* is not a pristine source of
  anything and falls through to the kept copy — on Windows the likely shape is a
  sharing violation from a scanner or an in-flight updater, which never reaches
  the parser at all.
- The one branch that can overwrite a good backup also asks whether the bundle
  still carries entrypoint bytecode. Everything else here rests on the manifest
  fingerprint, which has changed once already; bytecode cannot mislead the same
  way, because every shipped build has ~154 MB of it and `verify` refuses to emit
  a binary that has any.
- The bundle is parsed, and any syntax error aborts before the binary or the
  backup is touched — the two checks below answer "did we write what we meant
  to", which a corrupt splice satisfies perfectly. The parse is the same one
  the patches locate with, so it costs nothing extra and sits at *every* batch
  of edits as well as on the final bytes: a patch that produces rubble is named
  and dropped rather than aborting the run. See
  [PLAYBOOK.md](PLAYBOOK.md#the-syntax-gate).
- `restore` reads the **backup** before installing it — the check belongs on the
  file being written, since a copy truncated by a full disk would otherwise land
  over a working binary and be reported as a success. It declines only when the
  install is readable, unpatched *and* unversioned, where the kept copy is of an
  older release; it never reads the install to decide whether it *may* proceed,
  since making un-bricking conditional on the brick being readable would invert
  the point of the command.
- Every write is verified: patch-cc re-extracts the JS from the binary it just
  wrote and asserts it equals what it meant to write.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused outright — there is nothing clean to start from, and our edits change
  lengths, so a second pass would corrupt rather than update. `restore` or a
  reinstall are the only honest fixes; there is deliberately no override.
