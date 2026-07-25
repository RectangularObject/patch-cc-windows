"""Health checks over an installed binary and the patch set.

Two different questions, deliberately kept apart:

* **status** -- is the *installed* binary patched right now? Answered by the
  manifest comment every patched bundle ends with (plus legacy fingerprints),
  and the bytecode-stripped invariant.
* **dryrun** -- would our patches still apply to *this* bundle? Answered by
  running every patch and reporting per-step hits, so a silently drifted
  matcher shows up as a concrete "reducer.message_stop missed" instead of a
  lump count.

The dry run feeds every configurable patch a synthetic configuration built
from the bundle's own discovered agents and models, so branding and the model
overrides are exercised for real instead of being exempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bun import Bundle
from .codex.models import CodexModel
from .patcher import is_patched, read_manifest
from .patches import ALL_PATCHES, Options, Outcome, Patch
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models


@dataclass(slots=True)
class Status:
    patched: bool
    bytecode_stripped: bool
    bytecode_size: int
    #: Parsed manifest for binaries patched by this tool; ``None`` when the
    #: binary is pristine or predates the manifest.
    manifest: dict | None

    @property
    def patch_ids(self) -> list[str]:
        if not self.manifest:
            return []
        patches = self.manifest.get("patches")
        return (
            [p for p in patches if isinstance(p, str)]
            if isinstance(patches, list)
            else []
        )


def status(bundle: Bundle) -> Status:
    source = bundle.source
    return Status(
        patched=is_patched(source),
        bytecode_stripped=bundle.bytecode_size == 0,
        bytecode_size=bundle.bytecode_size,
        manifest=read_manifest(source),
    )


@dataclass(slots=True)
class DryRun:
    #: Shaped exactly like :attr:`PatchReport.results`, so every surface renders
    #: a dry run and a real apply with the same loop.
    results: list[tuple[Patch, Outcome]] = field(default_factory=list)
    anchors: dict[str, dict[str, int]] = field(default_factory=dict)
    #: What discovery found in this bundle -- the agents and model aliases the
    #: override patch would offer.
    agents: list[BuiltinAgent] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    @property
    def broken(self) -> list[Patch]:
        """Patches that failed, by :attr:`Outcome.health` and nothing else.

        A second opinion on health here is how ``doctor`` once printed a red
        cross and "all patches still match" in the same report: it judged on
        counts alone, so a patch that raised half-way was red on its own line
        and absent from this list.
        """
        return [p for p, o in self.results if o.health == "broken"]


def _synthetic_options(agents: list[BuiltinAgent], models: list[str]) -> Options:
    """A configuration that forces every configurable patch to do work.

    Each discovered agent is assigned a model different from its current one, so
    the rewrite (not the already-desired no-op) is what gets tested. Targets come
    from the bundle's own aliases, because each patch here runs against pristine
    in isolation: a Codex id would not be registered in the bundle a lone
    ``subagent-models`` run reads, and would correctly fail. What the two do
    together is an apply-time ordering, not something a dry run can show.
    """
    # Codex models are chosen by the user and described by their plan, neither of
    # which a dry run has, so it supplies a synthetic one -- with a context window
    # (so the context step is exercised), a `gpt-<ver>-<family>` id (so a family
    # shortcut is derived, exercising the general-resolver step too), and the full
    # effort ladder (so the registry step bakes every capability string) -- to
    # hold every one of codex-models' anchors in the net like the other patches.
    codex = [
        CodexModel(
            "gpt-9.9-doctor",
            "Doctor",
            272_000,
            efforts=("low", "medium", "high", "xhigh", "max"),
        )
    ]
    overrides = {
        agent.name: target
        for agent in agents
        if (target := next((m for m in models if m != agent.effective_model), None))
    }
    return Options(
        brand="patch-cc doctor", subagent_models=overrides, codex_models=codex
    )


def dryrun(bundle: Bundle) -> DryRun:
    """Run every patch against the bundle without writing anything."""
    source = bundle.source
    result = DryRun(
        agents=discover_agents(source),
        models=[INHERIT, *discover_models(source)],
    )
    options = _synthetic_options(result.agents, result.models)

    for patch in ALL_PATCHES:
        _, outcome = patch.run(source, options)
        result.results.append((patch, outcome))
        if patch.anchors:
            result.anchors[patch.id] = {a: source.count(a) for a in patch.anchors}

    return result
