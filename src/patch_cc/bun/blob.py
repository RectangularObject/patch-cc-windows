"""Read and rewrite the Bun standalone-executable blob.

Layout of the blob (little-endian throughout)::

    [ data arena: name / contents / sourcemap / bytecode / ... payloads ]
    [ module table: N records of `struct_size` bytes                    ]
    [ compileExecArgv payload                                           ]
    [ 32-byte offsets struct                                            ]
    [ 15-byte "\\n---- Bun! ----\\n" trailer                              ]

Every pointer in the blob is a ``(u32 offset, u32 length)`` pair relative to the
start of the blob, and they live in exactly two places: the module table and the
offsets struct. That is what makes rewriting tractable -- move a payload, then
fix up the pointers that describe it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import BunError

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32

#: Index of the ``loader`` byte in a module record's four trailing flags
#: (``encoding, loader, module_format, side``). It is how Bun decides whether a
#: module is JavaScript it compiles or an asset it hands over as bytes.
LOADER = 1

#: Module record field order. Old Bun (<1.3.7) stops after ``bytecode``.
FIELDS_NEW = (
    "name",
    "contents",
    "sourcemap",
    "bytecode",
    "module_info",
    "bytecode_origin_path",
)
FIELDS_OLD = FIELDS_NEW[:4]


class BlobError(BunError):
    """The .bun payload did not look like a Bun module graph."""


@dataclass(slots=True)
class Module:
    index: int
    ranges: dict[str, tuple[int, int]]
    trailing: bytes  # encoding, loader, module_format, side


@dataclass(slots=True)
class Blob:
    data: bytes
    struct_size: int
    modules: list[Module]
    modules_ptr: tuple[int, int]
    argv_ptr: tuple[int, int]
    entry_point_id: int
    flags: int
    offsets_at: int

    @property
    def fields(self) -> tuple[str, ...]:
        return FIELDS_NEW if self.struct_size == 52 else FIELDS_OLD

    def payload(self, rng: tuple[int, int]) -> bytes:
        off, length = rng
        return self.data[off : off + length]

    def entry_module(self) -> Module:
        """The module Bun runs -- as the container itself declares it.

        ``entry_point_id`` indexes the module table, and Bun resolves its own
        entrypoint with exactly this expression, so we agree with the runtime by
        construction rather than by coincidence.

        This used to scan a list of names the entrypoint had been seen under. A
        name is a *correlate* of entrypoint-ness, never the thing itself: 2.1.229
        renamed the module from ``/$bunfs/root/src/entrypoints/cli.js`` to
        ``/$bunfs/root/cli`` and the tool stopped reading binaries at all -- with
        the answer already parsed and sitting one field away, and already written
        back unchanged by :func:`rebuild`. A list that only ever grows (this one
        was at three names) is answering the wrong question. Prefer what the
        artifact declares over anything you can correlate with it, the same rule
        that makes agents and model aliases discovered rather than listed
        (docs/PLAYBOOK.md).
        """
        return self.modules[self.entry_point_id]

    def js_modules(self) -> list[Module]:
        """Every module Bun loads the way it loads the entrypoint: the JS the
        app is made of.

        Since 2.1.242 the entrypoint is a ~20 KB argv shim that lazily imports
        the app across ~1,300 ``chunk-*.js`` modules; before it, the entrypoint
        *was* the whole app. Both are one question -- which modules carry the
        JavaScript we patch -- and the loader byte answers it: a module's
        ``loader`` (the second trailing flag) says how Bun reads it, and the
        entry is by definition the JS Bun runs, so its loader *is* the JS loader.
        Read off the artifact, exactly as :meth:`entry_module` reads the
        entrypoint -- never hardcoded to a value, and never by name -- so a build
        that renumbers the loader still answers correctly. The assets (native
        addons, the bundled ``mermaid``/``hljs`` minified files, the HTML
        template) carry other loaders and are excluded: they are opaque bytes to
        Bun and would not parse as our JavaScript.

        Pre-split, this is the one-element list ``[entry_module()]`` -- the
        monolith -- so the single-module world is exactly the many-module world
        with one module, and every layer above flows through the same code.
        """
        loader = self.entry_module().trailing[LOADER]
        return [m for m in self.modules if m.trailing[LOADER] == loader]

    def bytecode_size(self) -> int:
        """Total precompiled Bun bytecode across every module, for either layout.

        ``bytecode`` is the fourth pair, so *both* record formats carry it --
        ``FIELDS_OLD`` is the first four of ``FIELDS_NEW``. Before 2.1.242 only
        the entrypoint carried any; the code-split builds carry it on every
        chunk, so the figure ``status`` reports and the write reclaims is the sum,
        not one module's.
        """
        return sum(m.ranges["bytecode"][1] for m in self.modules)


def _detect_struct_size(modules_len: int) -> int:
    """The module-record stride: 52 bytes (Bun >=1.3.7) or 36 (older).

    The table is a whole number of records, so the stride is the one of the two
    that divides its length. When neither does, the layout is one this code does
    not know, and picking a stride would scatter every pointer silently -- so it
    raises, the same answer :func:`parse` gives a corrupt ``entry_point_id``,
    rather than the old guess of 52 that left the entrypoint readable (its first
    pairs are common to both layouts) while every other module pointed at
    rubble. When *both* divide -- a length that is a multiple of both, which only
    some record counts produce -- the current era's 52 is taken, and the record
    loop's own bounds check is what would catch that guess if it were wrong: a
    36-byte table read as 52 runs its last records off the end of the blob.
    """
    old_ok = modules_len % 36 == 0
    if old_ok and modules_len % 52 != 0:
        return 36
    if modules_len % 52 != 0:
        raise BlobError(
            f"module table length {modules_len} is a whole number of neither "
            "52- nor 36-byte records -- not a Bun module-table layout patch-cc "
            "knows"
        )
    return 52


def parse(data: bytes) -> Blob:
    """Parse a raw Bun blob (already unwrapped from its container section)."""
    if len(data) < OFFSETS_SIZE + len(TRAILER):
        raise BlobError("blob is too small to hold offsets and trailer")
    if data[-len(TRAILER) :] != TRAILER:
        raise BlobError("missing Bun trailer -- not a Bun standalone payload")

    offsets_at = len(data) - len(TRAILER) - OFFSETS_SIZE
    (_byte_count,) = struct.unpack_from("<Q", data, offsets_at)
    modules_ptr = struct.unpack_from("<II", data, offsets_at + 8)
    (entry_point_id,) = struct.unpack_from("<I", data, offsets_at + 16)
    argv_ptr = struct.unpack_from("<II", data, offsets_at + 20)
    (flags,) = struct.unpack_from("<I", data, offsets_at + 28)

    struct_size = _detect_struct_size(modules_ptr[1])
    fields = FIELDS_NEW if struct_size == 52 else FIELDS_OLD
    table_off, table_len = modules_ptr
    if table_off + table_len > len(data):
        raise BlobError("module table runs past the end of the blob")

    modules: list[Module] = []
    for index in range(table_len // struct_size):
        base = table_off + index * struct_size
        ranges = {
            field: struct.unpack_from("<II", data, base + i * 8)
            for i, field in enumerate(fields)
        }
        # Every pair points into the blob; one that runs past it means either a
        # corrupt table or a stride guessed wrong (a 36-byte table read as 52
        # scatters its ranges). Either way this is not a graph to write back, so
        # it raises here rather than surfacing as garbage payloads downstream --
        # which is also what settles the both-strides-divide case for free.
        for off, length in ranges.values():
            if off + length > len(data):
                raise BlobError(
                    f"module {index} names a payload [{off}:{off + length}] past "
                    f"the {len(data)}-byte blob; the record layout does not fit"
                )
        trailing = data[base + len(fields) * 8 : base + len(fields) * 8 + 4]
        modules.append(Module(index=index, ranges=ranges, trailing=trailing))

    if not modules:
        raise BlobError("Bun blob contains no modules")
    if entry_point_id >= len(modules):
        # Checked here, where a bad value means the blob is corrupt, rather than
        # left to fail as an IndexError deep in a patch run. Bun refuses the same
        # condition when it loads the graph. There is deliberately no fallback to
        # guessing by name: a container that cannot say which module it runs is
        # not one we should be writing to.
        raise BlobError(
            f"entry point id {entry_point_id} is past the end of the "
            f"{len(modules)}-module table"
        )

    return Blob(
        data=data,
        struct_size=struct_size,
        modules=modules,
        modules_ptr=modules_ptr,
        argv_ptr=argv_ptr,
        entry_point_id=entry_point_id,
        flags=flags,
        offsets_at=offsets_at,
    )


def rebuild(
    blob: Blob, sources: dict[int, bytes], *, drop_bytecode: bool = True
) -> bytes:
    """Return a new blob with the given modules' source replaced.

    ``sources`` maps a module index to its new ``contents``; every other
    module's bytes are re-emitted unchanged. Payloads keep their original file
    order so the result stays as close to the input layout as possible. One
    edited module or a thousand is the same code -- the pre-split monolith is
    just ``{entry_index: new_bytes}``.

    ``drop_bytecode`` removes the precompiled Bun bytecode of exactly the
    modules whose source changed. Editing a module's source invalidates its
    bytecode -- Bun recompiles that module from source -- so keeping it would
    run the recompile cost *and* the megabytes; dropping it reclaims the space
    and, more importantly, guarantees our edits are what runs
    (docs/INTERNALS.md). A module we did not touch keeps its bytecode and its
    fast start. The set actually changed is returned by :func:`changed_modules`;
    here we drop for every module handed new bytes, since a caller only passes
    bytes it means to replace.
    """
    fields = blob.fields
    changed = set(sources)

    # Every payload, in the order it appears in the source arena.
    placed: list[
        tuple[int, int, int, str]
    ] = []  # (offset, length, module_index, field)
    for module in blob.modules:
        for field in fields:
            off, length = module.ranges[field]
            if length:
                placed.append((off, length, module.index, field))
    placed.sort()

    out = bytearray()
    new_ranges: dict[tuple[int, str], tuple[int, int]] = {}
    prev_end = 0
    for off, length, mod_index, field in placed:
        edited = mod_index in changed
        if edited and field == "bytecode" and drop_bytecode:
            new_ranges[(mod_index, field)] = (0, 0)
            prev_end = off + length
            continue

        payload = (
            sources[mod_index]
            if (edited and field == "contents")
            else blob.data[off : off + length]
        )
        # Preserve the 1-byte separators Bun emits between payloads.
        if prev_end and off > prev_end:
            out += b"\0" * (off - prev_end)
        new_ranges[(mod_index, field)] = (len(out), len(payload))
        out += payload
        prev_end = off + length

    out += b"\0"
    table_off = len(out)
    table_len = len(blob.modules) * blob.struct_size
    out += bytearray(table_len)

    argv = blob.payload(blob.argv_ptr)
    argv_off = len(out)
    out += argv + b"\0"

    offsets_at = len(out)
    out += bytearray(OFFSETS_SIZE)
    out += TRAILER

    for module in blob.modules:
        base = table_off + module.index * blob.struct_size
        for i, field in enumerate(fields):
            off, length = new_ranges.get((module.index, field), (0, 0))
            struct.pack_into("<II", out, base + i * 8, off, length)
        tail = base + len(fields) * 8
        out[tail : tail + 4] = module.trailing

    struct.pack_into("<Q", out, offsets_at, offsets_at)
    struct.pack_into("<II", out, offsets_at + 8, table_off, table_len)
    struct.pack_into("<I", out, offsets_at + 16, blob.entry_point_id)
    struct.pack_into("<II", out, offsets_at + 20, argv_off, len(argv))
    struct.pack_into("<I", out, offsets_at + 28, blob.flags)

    if drop_bytecode:
        # Each edited module now carries no bytecode -- whether we dropped a real
        # payload or it had none to begin with. Reading the outcome with a
        # ``(0, 0)`` default is what lets the had-none case flow through the same
        # check instead of raising ``KeyError``: a field the ``placed`` loop never
        # saw a length for is absent from ``new_ranges``, and absent *is*
        # stripped.
        for index in changed:
            assert new_ranges.get((index, "bytecode"), (0, 0)) == (0, 0)
    return bytes(out)


def changed_modules(blob: Blob, sources: dict[int, bytes]) -> dict[int, bytes]:
    """The subset of ``sources`` whose bytes actually differ from the blob.

    A module the patches parsed but left byte-identical must not have its
    bytecode dropped: that would trade a working fast-start module for a slow
    recompile of code that never changed. So the write reclaims bytecode for the
    modules that moved and no others, and the size tripwire measures against that
    same set.
    """
    return {
        index: data
        for index, data in sources.items()
        if data != blob.payload(blob.modules[index].ranges["contents"])
    }


def unwrap_section(section: bytes) -> tuple[bytes, int]:
    """Strip the length prefix a container section puts in front of the blob.

    Bun >=1.3.4 uses a u64 prefix; older builds use u32. Both are followed by
    padding up to the section's alignment, hence the 4 KiB slack window.
    """
    size = len(section)
    if size >= 8:
        (as64,) = struct.unpack_from("<Q", section, 0)
        if 8 + as64 <= size and 8 + as64 >= size - 4096:
            return section[8 : 8 + as64], 8
    if size >= 4:
        (as32,) = struct.unpack_from("<I", section, 0)
        if 4 + as32 <= size and 4 + as32 >= size - 4096:
            return section[4 : 4 + as32], 4
    raise BlobError("unrecognised .bun section header")


def wrap_section(blob: bytes, header_size: int) -> bytes:
    prefix = (
        struct.pack("<Q", len(blob))
        if header_size == 8
        else struct.pack("<I", len(blob))
    )
    return prefix + blob
