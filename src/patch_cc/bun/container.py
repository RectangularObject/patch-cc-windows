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

    ``source`` is the entrypoint as a :class:`patch_cc.js.Source` -- the bytes
    the blob carries, plus the parse of them, held together so a bundle is read
    and parsed at most once however many surfaces ask it a question. Its tree is
    lazy, so the surfaces that only scan for a literal never buy one.

    There is no decoding here any more. The layers below work in ``bytes``
    (``blob.entry_source``, ``blob.rebuild``), tree-sitter indexes ``bytes``,
    and a ``str`` in the middle bought nothing but a second unit of offset for a
    splice to be wrong in.
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
    return Bundle(
        path=path,
        kind=kind,
        source=js.Source(parsed.entry_source()),
        blob=parsed,
        header_size=header_size,
        binary_size=os.path.getsize(path),
        bytecode_size=parsed.bytecode_size(),
    )


def write(
    bundle: Bundle, source: bytes, out_path: str, *, drop_bytecode: bool = True
) -> None:
    """Repack ``source`` into a copy of the binary at ``out_path``.

    The patched image is staged to a temp file and verified -- re-extracted and
    compared against ``source`` -- *before* it is moved into place. A rebuild
    bug therefore fails without ever touching the live binary.
    """
    import shutil

    new_blob = blobmod.rebuild(bundle.blob, source, drop_bytecode=drop_bytecode)
    section = blobmod.wrap_section(new_blob, bundle.header_size)
    tmp = f"{out_path}.patch-cc.tmp"

    try:
        if bundle.kind == "elf":
            with open(bundle.path, "rb") as handle:
                raw = handle.read()
            patched = elf.write_section(raw, section)
            with open(tmp, "wb") as handle:
                handle.write(patched)
            os.chmod(tmp, os.stat(bundle.path).st_mode & 0o7777)
        else:
            shutil.copy2(bundle.path, tmp)
            macho.write_section(tmp, section)

        # raises before we commit if anything is off, size half included
        verify(
            tmp,
            source,
            original_size=bundle.binary_size,
            bytecode_size=bundle.bytecode_size,
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
    expected: bytes,
    *,
    original_size: int | None = None,
    bytecode_size: int = 0,
    kind: str = "elf",
) -> None:
    """Re-extract from a written binary and assert it round-trips exactly.

    ``original_size`` and ``bytecode_size`` describe the *pristine* binary and
    switch on the size half of the tripwire below; ``kind`` scopes it to the
    container that reclaims space.
    """
    try:
        written = read(path)
    except Exception as exc:
        raise ContainerError(f"patched binary could not be re-read: {exc}") from exc
    if written.source.data != expected:
        raise ContainerError(
            "patched binary did not round-trip: extracted source differs from "
            f"what we wrote ({len(written.source):,} vs {len(expected):,} bytes)"
        )
    if written.bytecode_size:
        # Read back off the written file, not asserted in memory. Bun runs the
        # bytecode in preference to the source, so any left behind would run the
        # *unpatched* program while every check above agreed the source was ours
        # -- every patch a silent no-op. docs/INTERNALS.md calls this the tripwire
        # for a Bun that makes bytecode authoritative; this is where it trips.
        raise ContainerError(
            f"patched binary still carries {written.bytecode_size:,} bytes of "
            "entrypoint bytecode, which would run instead of our edits"
        )
    # The size half of the tripwire: dropping the entrypoint bytecode should
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
    if kind == "elf" and original_size is not None and bytecode_size:
        reclaimed = original_size - written.binary_size
        if reclaimed < bytecode_size // 2:
            raise ContainerError(
                f"patched binary reclaimed only {reclaimed:,} of "
                f"{bytecode_size:,} dropped bytecode bytes; the in-place ELF "
                "write bloated rather than trimmed it"
            )
