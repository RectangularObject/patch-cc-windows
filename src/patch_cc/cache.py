"""Remembered interactive selection.

The last selection made, so the next interactive run comes up pre-filled even
after a Claude auto-update wiped the patched binary (and its manifest) away.
Written by the interactive menu and by any ``apply`` given an explicit selection
-- a bare ``apply`` (the default set) leaves it untouched, so it never clobbers a
remembered custom pick. ``apply --from-cache`` is the non-interactive reader,
replaying that selection when explicitly asked. Deleting the file resets the
menu to defaults and leaves ``--from-cache`` with nothing to replay.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .patches import ALL_PATCHES, Options, default_ids, ids


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "patch-cc" / "selection.json"


@dataclass(slots=True)
class Selection:
    patches: list[str] = field(default_factory=default_ids)
    options: Options = field(default_factory=Options)
    #: Patch ids the cache named that this build no longer has, dropped from
    #: :attr:`patches` while loading. Transient (never saved), so a caller that
    #: *acts* on a replay can say it applied fewer patches than were saved
    #: instead of doing so silently. Empty for a pre-fill, which does not act.
    dropped_patches: list[str] = field(default_factory=list)


def load() -> Selection:
    """The last selection, or a fresh default set when none is cached or the
    file is unreadable -- the menu always gets a usable, pre-checkable set.

    Only shapes are validated here (strings in the right places); whether an
    agent or model still exists is the menu's question, answered against the
    binary it is about to patch.
    """
    return load_strict() or Selection()


def load_strict() -> Selection | None:
    """The cached selection, or ``None`` when there is not one to read.

    The same parse, with the fallback withheld. A pre-fill is happy to treat an
    unreadable cache as "no preference" and offer the defaults, but ``apply
    --from-cache`` *acts* on the answer: handed the defaults it would apply a set
    the user never chose while reporting that it replayed their selection. The
    two callers want different things from the same failure, so the substitution
    belongs to the caller that wants it, not to the parse.
    """
    try:
        data = json.loads(cache_path().read_text("utf8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None  # a cache that is not a JSON object holds no selection
    known = set(ids())
    saved = data.get("patches", default_ids())
    if not isinstance(saved, list):
        saved = default_ids()
    # Each configurable patch reads its own value back off the cache dict, under
    # the key it also writes (`Patch.setting`), with the shape validation the
    # setting owns -- so the cache and the manifest cannot spell the same fact
    # two ways, which is exactly how `org_label` and `models` came to differ.
    options = Options()
    for patch in ALL_PATCHES:
        if patch.setting is not None:
            patch.setting.from_cache(options, data)
    return Selection(
        patches=[p for p in saved if p in known],
        dropped_patches=[p for p in saved if isinstance(p, str) and p not in known],
        options=options,
    )


def save(selection: Selection) -> None:
    """Best-effort: a cache that cannot be written just means no pre-fill next
    run -- it must never break the patch it was only trying to remember."""
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Each configurable patch writes its own cache key(s) from the same
        # `Options` it reads at bake time, so the cache format is the settings'
        # to declare, not a second hand-kept list beside the manifest's.
        data: dict[str, object] = {"patches": selection.patches}
        for patch in ALL_PATCHES:
            if patch.setting is not None:
                data.update(patch.setting.to_cache(selection.options))
        tmp.write_text(json.dumps(data, indent=2) + "\n", "utf8")
        tmp.replace(path)
    except OSError:
        pass
