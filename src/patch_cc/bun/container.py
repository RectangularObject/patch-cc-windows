"""One API over the three binary containers we support: ELF, Mach-O and PE."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from .. import js
from . import blob as blobmod
from . import elf, macho, pe
from .errors import INSTALL_HINT, BunError

ELF_MAGIC = b"\x7fELF"
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",  # thin, LE
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",  # thin, BE
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",  # fat
}

#: Every write is staged under this name beside its target, then renamed over
#: it, so an interrupted run never leaves a half-written binary wearing the name
#: of a working one.
TMP_SUFFIX = ".patch-cc.tmp"

#: Where a running image is parked so the new one can take its name. Numbered,
#: because more than one generation can be mapped at once: patch, start a fresh
#: Claude Code as instructed, leave the previous session open, patch again. A
#: single fixed name would already be held, and the second patch would fail
#: after the whole binary had been written and verified.
ASIDE_SUFFIX = ".patch-cc.old"

#: How many generations may be parked beside one binary at once. Reached only by
#: keeping that many differently-versioned sessions alive together.
ASIDE_SLOTS = 10


class ContainerError(BunError):
    pass


def detect(path: str) -> str:
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic == ELF_MAGIC:
        return "elf"
    if magic in MACHO_MAGICS:
        return "macho"
    if magic[: len(pe.DOS_MAGIC)] == pe.DOS_MAGIC:
        return "pe"
    raise ContainerError(
        f"{path} is not an ELF, Mach-O or PE binary. Claude Code must be the "
        f"native build -- reinstall with `{INSTALL_HINT}`."
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
    # Mach-O is the odd one out: LIEF works on a file, the other two on bytes.
    if kind == "macho":
        section = macho.read_section(path)
    else:
        with open(path, "rb") as handle:
            raw = handle.read()
        section = pe.read_section(raw) if kind == "pe" else elf.read_section(raw)

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
    tmp = f"{out_path}{TMP_SUFFIX}"

    try:
        if bundle.kind == "macho":
            shutil.copy2(bundle.path, tmp)
            macho.write_section(tmp, section)
        else:
            with open(bundle.path, "rb") as handle:
                raw = handle.read()
            patched_bytes = (
                pe.write_section(raw, section)
                if bundle.kind == "pe"
                else elf.write_section(raw, section)
            )
            with open(tmp, "wb") as handle:
                handle.write(patched_bytes)
            os.chmod(tmp, os.stat(bundle.path).st_mode & 0o7777)

        # raises before we commit if anything is off, size half included
        verify(
            tmp,
            contents,
            edited=set(changed),
            original_size=bundle.binary_size,
            dropped_bytecode=dropped if drop_bytecode else 0,
            kind=bundle.kind,
        )
        replace(tmp, out_path)
    except BaseException:
        _discard(tmp)
        raise


def verify(
    path: str,
    expected: dict[int, bytes],
    *,
    edited: set[int],
    original_size: int | None = None,
    dropped_bytecode: int = 0,
    kind: str = "elf",
) -> None:
    """Re-extract from a written binary and assert it round-trips exactly.

    ``expected`` is every module's intended bytes by blob index; ``edited`` are
    the modules whose source changed, which must run source rather than stale
    bytecode. ``original_size`` and ``dropped_bytecode`` describe the *pristine*
    binary and switch on the size half of the tripwire below; ``kind`` scopes it
    to the container that reclaims space.
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


def _aside_names(dest: str) -> list[str]:
    """Every name a parked image may have, in the order they are claimed.

    One generator for both halves, because it is one question: which of these is
    free, and which are collectable. A prefix glob would answer the second with
    names parking could never produce -- a parked image is a *working binary*,
    which is why the rollback message points at one, and someone who renames it
    to ``claude.exe.patch-cc.old-2.1.219`` to keep it must not have it swept.
    """
    return [
        f"{dest}{ASIDE_SUFFIX}{n}"
        for n in ("", *(f".{i}" for i in range(1, ASIDE_SLOTS)))
    ]


def _park(dest: str) -> str:
    """Rename ``dest`` out of the way and say where it went; first free name wins.

    Bounded, because every attempt fails for the same reason -- something maps
    that generation too -- so an unbounded hunt would turn one clear failure into
    a slow one. The caller reports exhaustion, with the last attempt's cause.
    """
    last: OSError = OSError(f"no free name beside {dest}")
    for aside in _aside_names(dest):
        try:
            os.replace(dest, aside)
            return aside
        except OSError as exc:
            last = exc
    raise last


def replace(source: str, dest: str) -> None:
    """Move ``source`` onto ``dest``, even while ``dest`` is being executed.

    POSIX swaps a directory entry and the running process keeps the inode it
    already mapped, so patching a live ``claude`` needs nothing special. Windows
    refuses to *overwrite* an image mapped for execution -- but it does allow
    *renaming* one. So the running binary is parked and the new one takes its
    name, which keeps "restart Claude Code for changes to take effect" the answer
    on either platform rather than "close it first, then patch".

    A parked image stays mapped until its process exits, so the next run is what
    collects it -- swept here at the top rather than after the fallback below,
    since the fallback only runs while something still holds the file and a
    parked image can only be deleted once nothing does.
    """
    for stale in _aside_names(dest):
        _discard(stale)

    try:
        os.replace(source, dest)
        return
    except PermissionError as exc:
        # On Windows a mapped image, which parking gets around. Anywhere else a
        # permission we do not have -- on the directory, most likely -- and
        # parking needs the very same one, so it will fail too. Either way the
        # original error travels with us rather than being replaced by a guess.
        denied = exc

    try:
        aside = _park(dest)
    except OSError as exc:
        raise ContainerError(
            f"could not write {dest} ({denied.strerror or denied}), nor move it "
            f"aside to any of {ASIDE_SLOTS} names beside it "
            f"({exc.strerror or exc}). If Claude Code is running, close every "
            "session and try again."
        ) from denied

    try:
        os.replace(source, dest)
    except BaseException as exc:
        # Nothing has changed from the caller's point of view yet and it must stay
        # that way. If even putting it back fails, `dest` does not exist and only
        # this message can say where the working binary went.
        try:
            os.replace(aside, dest)
        except OSError as rollback:
            raise ContainerError(
                f"{dest} could not be written ({exc}) and the original could not "
                f"be moved back ({rollback.strerror or rollback}). The working "
                f"binary is at {aside} -- rename it to "
                f"{os.path.basename(dest)} to recover."
            ) from exc
        raise
    _discard(aside)


def _discard(path: str) -> None:
    """Best-effort unlink -- a leftover the next run will try again to remove is
    no reason to fail a patch that has already landed.

    A read-only file is unlinkable on POSIX and not on Windows, so the attribute
    is cleared there before giving up: a ``claude.exe`` that arrived read-only
    would otherwise park a copy no later sweep could collect. Only there --
    ``S_IWRITE`` alone is mode 0o200 on POSIX, so retrying that way would strip a
    file's read and execute bits to fix a problem POSIX does not have.
    """
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except OSError:
        if os.name != "nt":
            return
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except OSError:
        pass
