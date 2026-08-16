"""Health checks over an installed binary and the patch set.

Two different questions, deliberately kept apart:

* **status** -- is the *installed* binary patched right now? Answered by the
  manifest comment every patched bundle ends with, and the bytecode-stripped
  invariant.
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

from . import js
from .bun import Bundle
from .codex import EFFORT_LADDER
from .codex.models import CodexModel
from .patcher import read_manifest
from .patches import ALL_PATCHES, Options, Outcome, Patch
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models


@dataclass(slots=True)
class Status:
    bytecode_stripped: bool
    bytecode_size: int
    #: Parsed manifest for binaries patched by this tool; ``None`` when the
    #: binary is pristine.
    manifest: dict | None

    @property
    def patched(self) -> bool:
        return self.manifest is not None

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
    return Status(
        bytecode_stripped=bundle.bytecode_size == 0,
        bytecode_size=bundle.bytecode_size,
        manifest=read_manifest(bundle.source),
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
    #: Where this build's *pristine* bundle stopped parsing, when it did --
    #: which `apply` refuses to patch at all. A rewrite that produces rubble is
    #: already reported as the patch that produced it, so this is the one
    #: parse failure no patch can be blamed for and none would survive.
    defect: js.Defect | None = None

    @property
    def broken(self) -> list[Patch]:
        """Patches that failed, by :attr:`Outcome.health` and nothing else.

        A second opinion on health here is how ``doctor`` once printed a red
        cross and "all patches still match" in the same report: it judged on
        counts alone, so a patch that raised half-way was red on its own line
        and absent from this list.

        Kept apart from :attr:`unhealthy` because only these have an anchor
        count worth printing: a patch that found its shape and failed to rewrite
        it has already told you the anchor is there.
        """
        return [p for p, o in self.results if o.health == "broken"]

    @property
    def unhealthy(self) -> list[Patch]:
        """Every patch not fully ``ok`` -- what the verdict answers for.

        A dry run is the one place ``candidates > 0, applied == 0`` cannot mean
        "already applied": ``doctor`` runs against a **clean** bundle (the
        pristine backup when the install is patched), so the reading that makes
        a missed sub-step benign is unavailable here and what is left is a
        matcher to repair. Ending green over one is the same silence ``expect``
        exists to break -- the per-patch line said ``~`` while the closing
        sentence said every patch still matched.

        ``apply`` judges the same outcome differently on purpose
        (:attr:`patch_cc.patcher.PatchReport.regressions` reads ``broken``
        alone): there a partial patch has still landed and still ships, and
        dropping it would cost the user a working feature over a missing
        refinement. Two questions, one health verdict, neither re-derived.
        """
        return [p for p, o in self.results if o.health != "ok"]

    @property
    def clean(self) -> bool:
        """The one green verdict: every patch fully ``ok`` *and* the bundle parses.

        The single home for the dry-run exit code, so no surface can disagree
        about it. The menu once read :attr:`broken` alone where the CLI read
        :attr:`unhealthy` (partial included) and :attr:`defect` together, so a
        partially-drifted or unparseable build showed thirteen green ticks and
        exit 0 in the menu and red in the CLI.
        """
        return not self.unhealthy and self.defect is None


def _synthetic_options(agents: list[BuiltinAgent], models: list[str]) -> Options:
    """A configuration that forces every configurable patch to do work.

    Each discovered agent is assigned a model different from its current one, so
    the rewrite (not the already-desired no-op) is what gets tested. Targets come
    from the bundle's own aliases: they are what a user without a Codex plan
    picks, so they are what the common path must keep working. The dry run
    composes the patches, so pinning an agent to the synthetic Codex id below
    would also be honest now -- `codex-models` runs first and registers it --
    but the aliases exercise the same rewrite without making the model
    overrides depend on the Codex patch landing.
    """
    # Codex models are chosen by the user and described by their plan, neither of
    # which a dry run has, so it supplies a synthetic one -- with a context window
    # (so the context step is exercised), a `gpt-<ver>-<family>` id (so a family
    # shortcut is derived, exercising the general-resolver step too), and the full
    # effort ladder (so the registry step bakes every capability string) -- to
    # hold every one of codex-models' anchors in the net like the other patches.
    codex = [CodexModel("gpt-9.9-doctor", "Doctor", 272_000, efforts=EFFORT_LADDER)]
    overrides = {
        agent.name: target
        for agent in agents
        if (target := next((m for m in models if m != agent.effective_model), None))
    }
    return Options(
        brand="patch-cc doctor",
        subagent_models=overrides,
        codex_models=codex,
        # The replace branch; the hidden form differs only in the emitted tail,
        # so one configuration holds the whole matcher in the net.
        org_label="patch-cc doctor",
    )


def dryrun(bundle: Bundle) -> DryRun:
    """Run every patch against the bundle without writing anything.

    The patches are *composed*, exactly as one pass of ``apply``'s fixpoint
    composes them, and the result is parsed. Running each patch against the
    pristine source and discarding its output was cheaper and answered a
    question nobody asks: it could not see `codex-models` registering the ids
    that `subagent-models` then pins -- the one ordering the playbook calls
    load-bearing -- and it could not see the bundle at all, only counters. The
    bug that motivated the parse was invisible twice over: the patch reported
    ``candidates=2 applied=2``, and the string it had corrupted was thrown away
    on the next line.

    Anchor counts and the parse both stay measured against the *pristine*
    source. They answer "what did this build ship", which is a question about
    the build, not about what our own edits left behind -- and a rewrite that
    left rubble is already reported against the patch that made it, by the same
    gate that stops it being applied.
    """
    source = bundle.source
    result = DryRun(
        agents=discover_agents(source),
        models=[INHERIT, *discover_models(source)],
        defect=source.defect(),
    )
    options = _synthetic_options(result.agents, result.models)

    current = source
    for patch in ALL_PATCHES:
        current, outcome = patch.run(current, options)
        result.results.append((patch, outcome))
        if patch.anchors:
            result.anchors[patch.id] = {a: source.count(a) for a in patch.anchors}

    return result
