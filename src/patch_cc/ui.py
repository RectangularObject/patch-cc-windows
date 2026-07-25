"""Shared console helpers, so output styling lives in one place."""

from __future__ import annotations

from rich.console import Console

from .patches.base import Options, Outcome, Patch

console = Console()

#: Glyph and colour per :attr:`Outcome.health`, so the CLI and the menu render
#: the same verdict identically.
MARKS = {"ok": ("✓", "green"), "partial": ("~", "yellow"), "broken": ("✗", "red")}


def findings(outcome: Outcome) -> list[tuple[str, str]]:
    """Every line worth showing under a patch line, as ``(style, text)``.

    One source for *what* gets said; each surface decides only how to draw it.
    The CLI and the menu built this separately and had already drifted -- the
    menu showed neither the exception that broke a patch nor any note, which is
    where an early warning goes to die.

    Notes always appear, green runs included: a warning withheld until
    something breaks can only ever arrive late. Absences are the noisy half
    (most patches lack several shapes on any build), so they wait for a verdict
    that is not ``ok``.
    """
    lines = [("red", reason) for reason in outcome.failures()]
    lines += [
        ("yellow", f"sub-step matched but not applied: {name}")
        for name in outcome.missed_steps()
    ]
    if outcome.health != "ok" and (absent := outcome.absent_steps()):
        lines.append(("dim", f"not on this build: {', '.join(absent)}"))
    notes = (*outcome.notes, *(n for s in outcome.steps.values() for n in s.notes))
    return lines + [("dim", note) for note in notes]


def applied_value(patch: Patch, outcome: Outcome, options: Options) -> str | None:
    """The value a configurable patch actually wrote, for the report line.

    Branding, the version marker, model overrides, and the chosen Codex models
    each carry a chosen value; a plain toggle patch carries none. One source so
    the CLI and the menu report the same thing after an apply.

    Gated on ``health``, not ``applied`` alone: a ``broken`` patch is dropped
    from the binary (the patcher re-runs without it) yet keeps its outcome in
    the report, so echoing its value would assert a feature the bundle does not
    carry -- the very "did it bake?" signal this line exists to keep honest.
    (``health`` is ``broken`` whenever nothing landed too, so this subsumes the
    old "applied something" guard.)
    """
    if outcome.health == "broken":
        return None
    if patch.id == "branding":
        return options.brand
    if patch.id == "version-marker":
        return options.version_suffix
    if patch.id == "subagent-models" and options.subagent_models:
        return ", ".join(f"{a}={m}" for a, m in options.subagent_models.items())
    if patch.id == "codex-models" and options.codex_models:
        # The models + port that actually landed -- the visible confirmation that
        # codex-models baked (a dropped/no-op patch returned above), and the one
        # place a model dropped for not being offered any more shows as absent.
        ids = ", ".join(m.id for m in options.codex_models)
        return f"{ids}  ·  :{options.codex_port}"
    return None


def gateway_note(port: int) -> tuple[str, str]:
    """Where Codex requests go, and whether anything is there to take them.

    A patched binary routes to this port whether or not the gateway is up, so
    every surface that bakes or reports the redirect owes this answer --
    forgetting ``codex serve`` is the likeliest way the bridge "doesn't work",
    and a URL that leads nowhere reads exactly like one that works. Left
    unanswered it costs minutes of silence and then a connection error naming no
    cause, at which point the user is debugging Claude Code instead of starting a
    server. One home, so the apply report, ``status`` and ``codex status`` cannot
    word it three ways -- the CLI and the menu each worded findings once, and
    they had already drifted.
    """
    # Imported here so the server (and its http/threading machinery) stays out of
    # every patch-cc invocation that never asks about the gateway.
    from .codex.gateway import running  # noqa: PLC0415

    # Spaced to fit the menu's 72-column panel at a five-digit port, so the half
    # that says what to do cannot be the half an ellipsis eats.
    if running(port):
        return "green", f"http://127.0.0.1:{port} · running"
    return "yellow", f"http://127.0.0.1:{port} · not running (patch-cc codex serve)"


def heading(text: str) -> None:
    console.print(f"\n[bold]{text}[/bold]")


def ok(text: str) -> None:
    console.print(f"[green]✓[/green] {text}")


def warn(text: str) -> None:
    console.print(f"[yellow]![/yellow] {text}")


def err(text: str) -> None:
    console.print(f"[red]✗[/red] {text}")
