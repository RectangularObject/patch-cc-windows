"""One API over the two binary containers we support: ELF and Mach-O."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .. import js
from . import blob as blobmod
from . import elf, macho
from .errors import BunError

ELF_MAGIC = b"\x7fELF"
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",  # thin, LE
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",  # thin, BE
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",  # fat
}


class ContainerError(BunError):
    pass


def detect(path: str) -> str:
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic == ELF_MAGIC:
        return "elf"
    if magic in MACHO_MAGICS:
        return "macho"
    raise ContainerError(
        f"{path} is neither ELF nor Mach-O. Claude Code must be the native "
        "build -- reinstall with `curl -fsSL https://claude.ai/install.sh | bash`."
    )


@dataclass(slots=True)
class Bundle:
    """The JS bundle plus everything needed to put it back.

    ``source`` is every JavaScript module the container declares, spanned as one
    :class:`patch_cc.js.Source` -- the bytes the blob carries plus their parse,
    held together so a bundle is read once however many surfaces ask it a
    question. The parse is lazy per module, so a surface that only scans for a
    literal never buys grammar, and the many-module surface is the one-module
    surface with more than one module (:meth:`patch_cc.bun.blob.Blob.js_modules`).

    There is no decoding here. The layers below work in ``bytes``
    (``blob.js_modules``, ``blob.rebuild``), tree-sitter indexes ``bytes``, and a
    ``str`` in the middle bought nothing but a second unit of offset for a splice
    to be wrong in.

    ``bytecode_size`` is the total across every module -- one carried it before
    2.1.242, every chunk carries one after -- which is what ``status`` reports
    and the write's size tripwire measures its reclaim against.
    """

    path: str
    kind: str
    source: js.Source
    blob: blobmod.Blob
    header_size: int
    binary_size: int
    bytecode_size: int


def read(path: str) -> Bundle:
    kind = detect(path)
    if kind == "elf":
        with open(path, "rb") as handle:
            raw = handle.read()
        section = elf.read_section(raw)
    else:
        section = macho.read_section(path)

    payload, header_size = blobmod.unwrap_section(section)
    parsed = blobmod.parse(payload)
    modules = [
        (m.index, parsed.payload(m.ranges["contents"])) for m in parsed.js_modules()
    ]
    return Bundle(
        path=path,
        kind=kind,
        source=js.Source.over(modules, parsed.entry_point_id),
        blob=parsed,
        header_size=header_size,
        binary_size=os.path.getsize(path),
        bytecode_size=parsed.bytecode_size(),
    )


def write(
    bundle: Bundle, patched: js.Source, out_path: str, *, drop_bytecode: bool = True
) -> None:
    """Repack the patched modules into a copy of the binary at ``out_path``.

    Only the modules whose bytes actually changed are re-emitted (and, when
    ``drop_bytecode``, have their now-stale bytecode dropped); every other
    module keeps its bytes and its fast start. The patched image is staged to a
    temp file and verified -- re-extracted and compared module for module --
    *before* it is moved into place, so a rebuild bug fails without ever
    touching the live binary.
    """
    import shutil

    contents = patched.contents()
    changed = blobmod.changed_modules(bundle.blob, contents)
    dropped = sum(bundle.blob.modules[i].ranges["bytecode"][1] for i in changed)
    new_blob = blobmod.rebuild(bundle.blob, changed, drop_bytecode=drop_bytecode)
    section = blobmod.wrap_section(new_blob, bundle.header_size)
    tmp = f"{out_path}.patch-cc.tmp"

    try:
        if bundle.kind == "elf":
            with open(bundle.path, "rb") as handle:
                raw = handle.read()
            patched_bytes = elf.write_section(raw, section)
            with open(tmp, "wb") as handle:
                handle.write(patched_bytes)
            os.chmod(tmp, os.stat(bundle.path).st_mode & 0o7777)
        else:
            shutil.copy2(bundle.path, tmp)
            macho.write_section(tmp, section)

        # raises before we commit if anything is off, size half included
        verify(
            tmp,
            contents,
            pristine=bundle.blob,
            edited=set(changed),
            original_size=bundle.binary_size,
            dropped_bytecode=dropped if drop_bytecode else 0,
            kind=bundle.kind,
        )
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def verify(
    path: str,
    expected: dict[int, bytes],
    *,
    pristine: blobmod.Blob,
    edited: set[int],
    original_size: int | None = None,
    dropped_bytecode: int = 0,
    kind: str = "elf",
) -> None:
    """Re-extract from a written binary and assert it round-trips exactly.

    ``expected`` is every module's intended bytes by blob index; ``pristine``
    is the blob the write started from, which everything the module comparison
    cannot see is checked against; ``edited`` are the modules whose source
    changed, which must run source rather than stale bytecode. ``original_size``
    and ``dropped_bytecode`` describe the *pristine* binary and switch on the
    size half of the tripwire below; ``kind`` scopes it to the container that
    reclaims space.
    """
    try:
        written = read(path)
    except Exception as exc:
        raise ContainerError(f"patched binary could not be re-read: {exc}") from exc
    got = written.source.contents()
    for index, want in expected.items():
        if got.get(index) != want:
            raise ContainerError(
                "patched binary did not round-trip: extracted module "
                f"{index} differs from what we wrote"
            )
    # The graph structure around the modules, read back off the written file.
    # The payloads the record chain owns -- the shared bytecode string table
    # above all -- are what every untouched module's bytecode indexes, so bytes
    # lost or shifted here are a segfault at launch that no per-module
    # comparison would catch: that is exactly how 2.1.246 bricked while every
    # module compared equal.
    out = written.blob
    if out.flags != pristine.flags:
        raise ContainerError(
            f"patched binary carries flags {out.flags:#x} where the pristine "
            f"blob carried {pristine.flags:#x}"
        )
    if out.offsets_at - out.table_end != pristine.offsets_at - pristine.table_end:
        raise ContainerError(
            "patched binary's record-chain tail changed length; records or "
            "compileExecArgv were lost in the rewrite"
        )
    theirs = {r.label: r.rng for r in out.arena_records}
    for record in pristine.arena_records:
        if record.label not in theirs:
            raise ContainerError(f"patched binary lost the {record.label} record")
        if out.payload(theirs[record.label]) != pristine.payload(record.rng):
            raise ContainerError(
                f"patched binary's {record.label} does not round-trip; every "
                "untouched module's bytecode indexes it"
            )
        if theirs[record.label][0] % blobmod.ALIGN != record.rng[0] % blobmod.ALIGN:
            raise ContainerError(
                f"patched binary's {record.label} moved off its alignment "
                "phase; Bun deserializes it in place and would refuse or crash"
            )
    want_hashes = pristine.source_hashes()
    got_hashes = out.source_hashes()
    if (want_hashes is None) != (got_hashes is None):
        raise ContainerError("patched binary lost the source-hash record")
    if want_hashes is not None and got_hashes is not None:
        for index, (want_hash, got_hash) in enumerate(zip(want_hashes, got_hashes)):
            expected_hash = 0 if index in edited else want_hash
            if got_hash != expected_hash:
                raise ContainerError(
                    f"module {index}'s source-hash word is {got_hash:#x} where "
                    f"{expected_hash:#x} was meant; JSC would key "
                    + (
                        "our edited source under the pristine text's hash"
                        if index in edited
                        else "this module's source under the wrong hash"
                    )
                )
    # Read back off the written file, not asserted in memory. Bun runs a
    # module's bytecode in preference to its source, so any left on a module we
    # edited would run the *unpatched* code while every byte comparison above
    # agreed the source was ours -- a silent no-op. docs/INTERNALS.md calls this
    # the tripwire for a Bun that makes bytecode authoritative; this is where it
    # trips, now per edited module rather than for the one old entrypoint.
    by_index = {m.index: m for m in written.blob.modules}
    for index in edited:
        left = by_index[index].ranges["bytecode"][1] if index in by_index else 0
        if left:
            raise ContainerError(
                f"patched binary still carries {left:,} bytes of bytecode on "
                f"edited module {index}, which would run instead of our edits"
            )
    # Every *kept* bytecode payload must also still sit on its pristine
    # alignment phase (`blobmod.ALIGN`, docs/INTERNALS.md): Bun 1.4.1
    # deserializes bytecode in place and misalignment is a launch-time crash
    # that every byte comparison above would wave through -- the payloads are
    # identical, just parked one byte off. `rebuild` preserves the phase by
    # construction; this is the tripwire for the rebuild edit that stops it.
    for module in pristine.modules:
        off, length = module.ranges["bytecode"]
        if length and module.index not in edited and module.index in by_index:
            got_off = by_index[module.index].ranges["bytecode"][0]
            if got_off % blobmod.ALIGN != off % blobmod.ALIGN:
                raise ContainerError(
                    f"module {module.index}'s bytecode moved off its alignment "
                    "phase; Bun deserializes it in place and would refuse or "
                    "crash"
                )
    # The size half of the tripwire: dropping the edited modules' bytecode should
    # leave the file smaller by about that much. The ELF path rewrites in place
    # and reclaims it (measured: reclaimed == bytecode to within the manifest's
    # own bytes), so a write that reclaimed little or nothing bloated the binary
    # rather than trimming it, and is refused. Half the dropped bytecode is the
    # threshold -- far below a real shrink (~100%), far above a splice that kept
    # the freed bytes (~0) -- so manifest and padding variance never trip it.
    # Mach-O is exempt on purpose: `macho.py` only ever grows a segment, never
    # shrinks one, so the file keeps its size (a working binary that is not
    # smaller, docs/INTERNALS.md) -- a limitation to fix with a Mac in hand, not
    # a corruption to refuse a working Mac user over.
    if kind == "elf" and original_size is not None and dropped_bytecode:
        reclaimed = original_size - written.binary_size
        if reclaimed < dropped_bytecode // 2:
            raise ContainerError(
                f"patched binary reclaimed only {reclaimed:,} of "
                f"{dropped_bytecode:,} dropped bytecode bytes; the in-place ELF "
                "write bloated rather than trimmed it"
            )
