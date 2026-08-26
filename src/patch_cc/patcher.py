"""Orchestration: read a binary, run selected patches, write it back safely."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, locate
from .bun import Bundle, BunError, container
from .js import Source, SyntaxGateError
from .patches import ALL_PATCHES, Options, Outcome, Patch

#: Every patched bundle ends with one comment line recording exactly what was
#: applied. Comments cannot collide with code, survive re-extraction, and make
#: ``status`` a parse instead of a guess -- value-flip patches leave no other
#: fingerprint.
MANIFEST_PREFIX = "//patch-cc "


class AlreadyPatchedError(RuntimeError):
    """The only source available is already patched; patching it would stack.

    Our edits change lengths, so a second pass over a patched bundle corrupts
    rather than updates. There is deliberately no force-override: when no
    pristine backup exists the only honest fixes are ``restore`` or a
    reinstall.
    """


def landed_ids(results: list[tuple[Patch, Outcome]]) -> list[str]:
    """Exactly what the manifest may claim: every patch that is not broken.

    A patch that rewrote something but missed an expectation is not an applied
    patch; recording it would make ``status`` assert a feature that is not
    there. One home for the rule: ``apply``'s report and doctor's smoke bake
    both build their manifests from it, so they cannot drift on what "landed"
    means.
    """
    return [p.id for p, o in results if o.health != "broken"]


@dataclass(slots=True)
class PatchReport:
    version: str | None
    kind: str
    original_size: int
    patched_size: int = 0
    results: list[tuple[Patch, Outcome]] = field(default_factory=list)
    backup: Path | None = None
    output: Path | None = None

    @property
    def landed_ids(self) -> list[str]:
        return landed_ids(self.results)

    @property
    def regressions(self) -> list[Patch]:
        """Selected patches that did not land, or that missed an expectation."""
        return [p for p, o in self.results if o.health == "broken"]

    @property
    def ok(self) -> bool:
        """Did this run write a binary with no *broken* patch dropped from it?

        The exit code of both surfaces, in one place. ``broken`` is the bar, not
        ``whole``: a broken patch (nothing landed) is left out of the binary, so
        a run that dropped one has not done what it was asked and must not report
        success. A ``partial`` patch -- some sites landed, some drifted -- *does*
        ship and is not a regression here, because dropping a landed feature over
        a missing refinement would cost the user more than the drift does; that
        is the one verdict `apply` and `doctor` read differently on purpose
        (:attr:`patch_cc.doctor.DryRun.unhealthy`).
        """
        return self.output is not None and not self.regressions


def manifest_payload(landed: list[str], options: Options) -> dict:
    """Describe what is *in the binary* -- never what was merely asked for.

    Each configurable value belongs to a patch, so it is recorded only when
    that patch landed. Writing the brand while `branding` was dropped for
    drifting would have `status` assert a name the bundle does not contain,
    and re-applying from that manifest would keep asserting it.

    Kept apart from its serialisation because this *is* the description of a
    patched bundle's shape, and one other question is asked of it: the menu's
    "does my selection differ from the binary?" compares the payload this would
    write against the one the binary carries, rather than re-listing the fields
    by hand. A second, hand-written list of what counts is how the gateway port
    and the imported model set came to change with the menu reporting no change.
    """
    applied = set(landed)
    payload: dict = {"v": 1, "tool": __version__, "patches": landed}
    # Each configurable patch declares its own manifest key and how to fill it
    # (`Patch.setting`), so this records what is *in the binary* without a chain
    # of `patch.id ==` here that could drift from the cache's spelling. A value
    # is recorded only when its patch landed *and* there is one worth recording
    # (`recorded`): a brand at the default, or no override, leaves no key -- so
    # `status` can never assert a name the bundle does not carry. `org-label`'s
    # `recorded` is always true, because an empty org label is the real value
    # "hide the segment", not the absence of a choice.
    for patch in ALL_PATCHES:
        setting = patch.setting
        if setting is not None and patch.id in applied and setting.recorded(options):
            payload[setting.manifest_key] = setting.to_manifest(options)
    return payload


def build_manifest(landed: list[str], options: Options) -> str:
    """The manifest comment line a patched bundle ends with."""
    payload = json.dumps(manifest_payload(landed, options), separators=(",", ":"))
    return "\n" + MANIFEST_PREFIX + payload + "\n"


def read_manifest(source: Source) -> dict | None:
    """The applied-patch record, or ``None`` for pristine/legacy binaries.

    A byte scan, so asking it never buys a parse -- `status` and the menu's
    first screen answer from here without touching the grammar.
    """
    marker = ("\n" + MANIFEST_PREFIX).encode()
    start = source.data.rfind(marker)
    if start == -1:
        return None
    start += len(marker)
    end = source.data.find(b"\n", start)
    line = source.data[start:] if end == -1 else source.data[start:end]
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_patched(source: Source) -> bool:
    """Whether the bundle carries our manifest -- the one mark of our work.

    Authorship is declared, never inferred. This used to also sniff side
    effects of our edits (the ``__cc_`` identifier prefix, the old
    ``--version`` marker), until 2.1.227 shipped ``__cc_``-prefixed shell
    variables of its own and every pristine install read as patched. An
    inferred fingerprint is a bet that upstream's vocabulary never overlaps
    ours, and once upstream ships it, it fires on every build after, forever.
    """
    return read_manifest(source) is not None


def selected_patches(ids: list[str]) -> list[Patch]:
    """Resolve ids to patches, preserving registry (run) order."""
    wanted = set(ids)
    return [patch for patch in ALL_PATCHES if patch.id in wanted]


def run_patches(
    source: Source, patches: list[Patch], options: Options
) -> tuple[Source, list[tuple[Patch, Outcome]]]:
    results: list[tuple[Patch, Outcome]] = []
    current = source
    for patch in patches:
        current, outcome = patch.run(current, options)
        results.append((patch, outcome))
    return current, results


def _backup_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "patch-cc" / "backups"


def backup_path_for(install: locate.Installation) -> Path:
    """Where a *new* backup for this binary is written.

    A canonical native install is version-named, so the binary's own name is
    already the version and names the backup: ``2.1.219.orig``. When the name is
    not a version we cannot tell two unrelated ``claude`` binaries apart by name
    alone, so a short hash of the absolute path is mixed in to keep their
    backups distinct.
    """
    root = _backup_dir()
    if install.version:
        return root / f"{install.binary.name}.orig"
    digest = hashlib.sha256(str(install.binary.resolve()).encode()).hexdigest()[:8]
    return root / f"{install.binary.name}.unknown-{digest}.orig"


def existing_backup(install: locate.Installation) -> Path | None:
    """The pristine copy on disk, or ``None``.

    One home for "is there a backup, and which file is it" -- read, restore,
    dry-run and status all ask that one question, and asking it five ways is how
    a safety net grows a hole.

    Backups written before 0.2.0 doubled the name (``2.1.219.2.1.219.orig``:
    for a version-named install the name *is* the version, so composing the two
    only ever said it twice). Those are still adopted, because the alternative
    is an install whose pristine copy silently stops counting as one.
    """
    dest = backup_path_for(install)
    if dest.exists():
        return dest
    legacy = dest.with_name(f"{install.binary.name}.{install.version}.orig")
    return legacy if install.version and legacy.exists() else None


def read_pristine(
    install: locate.Installation, *, installed: Bundle | None = None
) -> Bundle:
    """The bundle patching starts from: the installed binary while it is still
    the original, and the kept copy only once it is not.

    Patching never stacks edits on edits -- each apply begins at this pristine
    source, so the selected set is always exactly what ends up in the binary.

    The install is asked *first* because it is the original of the version
    installed now, while a backup is only the original of the version it was
    taken from. Those coincide wherever the launcher is version-named, and part
    company wherever it is a fixed path: every Windows install, and any Homebrew
    or npm one. There, a Claude update leaves a backup describing the version
    before it, and starting from that would patch the old bundle over the new
    install -- silently downgrading Claude. An update replaces the whole binary,
    so "installed and unpatched" means "installed and pristine".

    ``installed`` may carry an already-read bundle of the installed binary, so a
    caller that needed it anyway (the menu reads it for status) stops paying for
    a second full read of the same 275 MB file.
    """
    backup = existing_backup(install)
    if installed is None:
        try:
            installed = container.read(str(install.binary))
        except (BunError, OSError):
            # An install we cannot read is not a pristine source of anything, and
            # is exactly when a kept copy earns its keep. With no copy the failure
            # is still the answer, so it propagates. `OSError` too: on Windows the
            # likely shape is a sharing violation from a scanner or an in-flight
            # updater, which never reaches the parser at all.
            if backup is None:
                raise
            return container.read(str(backup))
    if not is_patched(installed.source):
        return installed
    return container.read(str(backup)) if backup is not None else installed


def _backup(install: locate.Installation, pristine: Bundle) -> Path:
    """Record the pristine original, so ``restore`` is a plain copy back.

    Only ever reached with an unpatched original: :func:`patch_installation`
    refuses a patched binary that has no backup to start from, so there is no
    path here that could enshrine a poisoned "original" for ``restore`` to hand
    back as clean.

    Rewritten only where all three hold: the launcher's name carries no version
    (so the kept copy cannot vouch for which release it is), ``pristine`` *is*
    the installed binary (the other half of what :func:`read_pristine` decided),
    and that binary still carries its entrypoint bytecode. A version-named
    install is thereby left alone -- each release already backs up under its own
    name.

    The bytecode is a second opinion on "unpatched", because this is the one
    branch that can destroy a clean copy. Everything above rests on
    :func:`is_patched`, a string fingerprint that has already changed once; were
    it to miss again, a patched install would read as pristine and be written
    over the only pristine copy there was. Every shipped build gives its *entry*
    module bytecode -- ~154 MB of it before the 2.1.242 code split, 127 KB on the
    argv shim after -- and no binary we write has any there: the manifest is
    appended to that module on every apply, and an edited module loses its
    bytecode. So an entry module carrying bytecode was not written by us,
    whatever the fingerprint says. The total across every module is the wrong
    reading now -- a split build we patched keeps the untouched modules' bytecode
    (150 MB of it), so the sum stays large whether we wrote the binary or not.
    """
    existing = existing_backup(install)
    supersedes_the_kept_copy = (
        install.version is None
        and Path(pristine.path) == install.binary
        and pristine.blob.entry_module().ranges["bytecode"][1] > 0
    )
    if existing is not None and not supersedes_the_kept_copy:
        return existing
    dest = backup_path_for(install)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Staged, then renamed -- the same discipline `container.write` uses on the
    # binary itself, and for the same reason. Copying 275 MB straight onto the
    # canonical name means a full disk, a SIGKILL, or a closed lid leaves a
    # truncated file wearing it; `existing_backup` asks only whether that name
    # exists, and `restore` copies whatever it finds over the live executable
    # without reading it. The half-written "original" would be installed as the
    # clean one -- the brick this whole file exists to prevent. A rename is
    # atomic, so the name appears only once the bytes are all there.
    staged = dest.with_name(dest.name + ".partial")
    try:
        # Named by the bundle we vetted rather than by the install, so the file
        # copied is provably the one the decision above was made about.
        shutil.copy2(pristine.path, staged)
        os.replace(staged, dest)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return dest


def patch_installation(
    install: locate.Installation,
    selected: list[str],
    options: Options,
    *,
    bundle: Bundle | None = None,
) -> PatchReport:
    """Patch ``install`` with the ``selected`` patches.

    Patching always starts from a pristine source (:func:`read_pristine`), so
    re-applying replaces the previous patch set instead of stacking on it.
    ``bundle`` may carry that already-read source (the CLI reads it for
    validation first).
    """
    source = bundle if bundle is not None else read_pristine(install)
    if is_patched(source.source):
        raise AlreadyPatchedError(
            f"{install.binary} is already patched and no pristine backup exists, "
            "so there is nothing clean to patch from. Run `patch-cc restore`, "
            "or reinstall Claude to get a clean binary."
        )

    # Before anything is rewritten: a bundle that does not parse to begin with
    # cannot be told apart from one a splice broke, and every rewrite made to it
    # would be blamed in turn. Asked here, it is answered once and as itself.
    source.source.verify()

    # A broken patch still rewrote *something*, and those orphan edits would ship
    # unrecorded -- a feature half-present that the manifest cannot describe. So
    # the run is redone without it. Redone *repeatedly*: patches see each other's
    # output, so dropping one can change what the next finds, and only a whole
    # run that comes back clean proves the set has settled. Judging the bytes of
    # the last run by the verdicts of the first is how a manifest starts lying.
    patches = selected_patches(selected)
    seen: dict[str, tuple[Patch, Outcome]] = {}
    while True:
        patched_source, results = run_patches(source.source, patches, options)
        seen.update({p.id: (p, o) for p, o in results})
        healthy = [p for p, o in results if o.health != "broken"]
        if len(healthy) == len(patches):
            break
        patches = healthy

    report = PatchReport(
        version=install.version,
        kind=source.kind,
        original_size=source.binary_size,
        # Every patch reported by the last run it took part in: a dropped one
        # keeps the outcome that condemned it, a survivor the run that shipped.
        results=[seen[p.id] for p in selected_patches(list(seen))],
    )

    landed = report.landed_ids
    if not landed:
        # Nothing changed; writing would only strip bytecode for no benefit.
        return report

    # The manifest is appended to the entry module through the same gated edit as
    # every rewrite, so the bundle that gets written is one no splice -- the
    # manifest's included -- left unparseable, and it is checked as the bytes it
    # will be rather than as a promise about them. Raising here leaves the install
    # and the backup untouched: nothing below this line has run yet.
    patched_source = patched_source.append_manifest(build_manifest(landed, options))

    # One last gate, on the exact bytes about to be written, from trees that share
    # nothing with the incremental ones every edit above was checked against.
    # `Source.apply` keeps those in step and reparses each edited module at each
    # batch -- fast, and correct on every build in the corpus -- but it is one
    # lineage, trusted end to end, and the write verifier below re-extracts and
    # compares bytes, so it agrees with a splice the incremental parse blessed and
    # a full parse would reject. A parse from scratch of each edited module is the
    # only thing that would catch an incremental-reparse bug before it reaches the
    # user's binary; unedited modules were never incrementally touched, so this
    # costs the edits' own parses, not the whole surface's. `doctor` never
    # exercises it (a dry run does not write), so this is its one home.
    defect = patched_source.fresh_defect()
    if defect is not None:
        raise SyntaxGateError(
            "the patched bundle does not parse as a whole, though each edit did "
            f"incrementally; refusing to write: {defect}"
        )

    # Unconditional, and there is deliberately no way to ask for a write without
    # it. Two parameters have offered one now -- a switch that skipped the copy,
    # then an `out_path` that wrote somewhere else instead -- and neither ever had
    # a caller. What each really added was a path on which `restore` has nothing
    # to hand back, which is the one guarantee this file exists to keep.
    report.backup = _backup(install, source)

    container.write(source, patched_source, str(install.binary))
    report.output = install.binary
    report.patched_size = install.binary.stat().st_size
    return report


def restore(install: locate.Installation) -> Path | None:
    """Copy the pristine backup back over the installed binary.

    ``None`` when there was nothing to undo -- an outcome, not a failure: the
    binary is already what the user asked for, and the copy we would have
    written is not necessarily *this* version.

    That is refused only where the install is *readable, unpatched and
    unversioned*: the rule :func:`read_pristine` follows, scoped to the layout
    where it bites. Where the launcher is a fixed path the kept copy is of
    whichever release was patched last, so after a Claude update it is the
    *previous* one, and copying it back would answer "give me my clean binary"
    with a silent downgrade. Where the launcher carries the version its own name
    pins it, so a byte-exact re-copy is a reasonable thing to ask for.

    An install we cannot read is never refused: a working older Claude beats a
    broken newer one, and un-bricking must not be conditional on the brick being
    readable.

    The kept copy is read before it is installed. That is the only check worth
    making here and it is on the file being *written* -- one truncated by a full
    disk or a killed run would otherwise be installed over a working binary and
    reported as a success. Since ``_backup`` now rewrites that copy on every
    apply against a fixed-path install, its integrity carries more weight than
    it used to.
    """
    backup = existing_backup(install)
    if backup is None:
        raise FileNotFoundError(
            f"No backup found for {install.binary.name} at "
            f"{backup_path_for(install)}. If Claude auto-updated, the original "
            "for this version was never saved -- reinstall to get a clean binary."
        )
    container.read(str(backup))  # raises rather than install an unusable copy

    if install.version is None:
        try:
            if not is_patched(container.read(str(install.binary)).source):
                return None
        except (BunError, OSError):
            pass  # not known to be pristine, so not one we refuse to replace

    # A full-file copy-back, so it works for every container we patch. Staged
    # beside the target and renamed over it, which is also how a `claude` that is
    # running right now gets replaced.
    staged = f"{install.binary}{container.TMP_SUFFIX}"
    shutil.copy2(backup, staged)
    container.replace(staged, str(install.binary))
    return install.binary
