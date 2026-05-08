"""
Council deliberation CLI — run multi-round agent discussions.

Runs a council directly against LLM backends without NATS.
Uses :class:`CouncilRunner` (the council equivalent of WorkerTestRunner).

Commands::

    heddle council run <config> --topic <text>    # Run a council discussion
    heddle council validate <config>              # Validate a council config
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import structlog

from heddle.cli.config import DEFAULT_CONFIG_PATH, resolve_config

if TYPE_CHECKING:
    from heddle.contrib.council.schemas import TranscriptEntry

logger = structlog.get_logger()


def _run_async(coro):
    """Run a coroutine, closing it if a mock leaves it pending."""
    try:
        return asyncio.run(coro)
    finally:
        if inspect.iscoroutine(coro) and coro.cr_frame is not None:
            coro.close()


@click.group()
@click.option("--config-path", default=DEFAULT_CONFIG_PATH, help="User config file path.")
@click.pass_context
def council(ctx: click.Context, config_path: str) -> None:
    """Multi-agent council deliberation — no NATS needed."""
    from heddle.cli.config import apply_config_to_env

    ctx.ensure_object(dict)
    ctx.obj["config"] = resolve_config(config_path=config_path)
    # Lifted from ``run`` so ``validate`` (and future siblings) see config-file
    # backend settings via the env vars that ``build_backends_from_env`` and
    # the tier-availability checks in ``validate`` both read.
    apply_config_to_env(ctx.obj["config"])


# ---------------------------------------------------------------------------
# heddle council run
# ---------------------------------------------------------------------------


@council.command()
@click.argument("config", type=click.Path(exists=True))
@click.option("--topic", "-t", default=None, help="Topic string or path to a text file.")
@click.option("--output", "-o", default=None, type=click.Path(), help="Write full result as JSON.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show full agent responses.")
@click.option("--rounds", default=None, type=int, help="Override max_rounds from config.")
@click.option(
    "--no-convergence", is_flag=True, default=False, help="Disable convergence (run all rounds)."
)
def run(  # noqa: PLR0915 — body is a linear CLI flow, splitting hurts readability
    config: str,
    topic: str | None,
    output: str | None,
    verbose: bool,
    rounds: int | None,
    no_convergence: bool,
) -> None:
    """Run a council discussion on a topic.

    CONFIG is the path to a council YAML config file.

    The topic can be provided via --topic (as a string or file path),
    or will be prompted interactively if omitted.
    """
    from heddle.contrib.council.config import load_council_config
    from heddle.contrib.council.runner import CouncilRunner
    from heddle.worker.backends import build_backends_from_env

    # Resolve topic.
    if topic is None:
        topic = click.prompt("Topic")
    elif Path(topic).is_file():
        topic = Path(topic).read_text().strip()

    # Load and optionally override council config.
    council_config = load_council_config(config)

    if rounds is not None:
        council_config.max_rounds = rounds

    if no_convergence:
        council_config.convergence.method = "none"

    # Build backends from env.  ``council`` group has already lifted user-config
    # settings into the environment so ``build_backends_from_env`` sees them.
    backends = build_backends_from_env()

    if not backends:
        raise click.ClickException(
            "No LLM backends available. Set OLLAMA_URL or ANTHROPIC_API_KEY."
        )

    click.echo()
    click.echo(click.style("  Heddle Council", fg="cyan", bold=True))
    click.echo(f"  Config:   {council_config.name}")
    click.echo(f"  Agents:   {', '.join(a.name for a in council_config.agents)}")
    click.echo(f"  Protocol: {council_config.protocol}")
    click.echo(f"  Rounds:   {council_config.max_rounds} max")
    click.echo(f"  Topic:    {topic[:80]}{'...' if len(topic) > 80 else ''}")
    click.echo()

    # on_turn callback for live output.
    def on_turn(entry: TranscriptEntry) -> None:
        _print_turn(entry, verbose=verbose)

    runner = CouncilRunner(backends, config=council_config)

    async def _run_and_close() -> Any:
        # Run the council and release any cached chatbridge / backend
        # httpx clients in the same event loop they were bound to.
        try:
            return await runner.run(topic, on_turn=on_turn)
        finally:
            await runner.aclose()
            for backend in backends.values():
                try:
                    await backend.aclose()
                except Exception as exc:
                    logger.warning(
                        "council.backend_close_failed",
                        backend=type(backend).__name__,
                        error=str(exc),
                    )

    result = _run_async(_run_and_close())

    # Synthesis.
    click.echo()
    click.echo(click.style("  Synthesis", fg="cyan", bold=True))
    click.echo()
    for line in result.synthesis.splitlines():
        click.echo(f"  {line}")
    click.echo()

    # Summary footer.
    converged_str = click.style("yes", fg="green") if result.converged else "no"
    score_str = f"{result.convergence_score:.2f}" if result.convergence_score is not None else "n/a"
    elapsed_str = f"{result.elapsed_ms / 1000:.1f}s"
    total_tokens = sum(result.total_token_usage.values())
    click.echo(
        click.style("  Summary: ", fg="cyan")
        + f"{result.rounds_completed} round(s), "
        + f"converged: {converged_str}, "
        + f"score: {score_str}, "
        + f"{elapsed_str}, "
        + f"{total_tokens} tokens"
    )
    click.echo()

    # Write JSON output if requested.
    if output:
        Path(output).write_text(result.model_dump_json(indent=2))
        click.echo(f"  Result written to {output}")
        click.echo()


def _print_turn(entry: TranscriptEntry, *, verbose: bool = False) -> None:
    """Print a single agent turn to the console."""
    header = click.style(f"  [{entry.agent_name}]", fg="yellow") + f" (round {entry.round_num})"
    click.echo(header)

    content = entry.content
    if not verbose and len(content) > 200:
        content = content[:200] + "..."
    for line in content.splitlines():
        click.echo(f"    {line}")


# ---------------------------------------------------------------------------
# heddle council validate
# ---------------------------------------------------------------------------


@council.command()
@click.argument("config", type=click.Path(exists=True))
def validate(config: str) -> None:
    """Validate a council config file.

    Reports agent count, protocol, convergence method, and required tiers.
    Checks backend availability from environment. Exit 0 if valid, 1 on error.
    """
    import os

    import yaml

    from heddle.contrib.council.config import validate_council_config

    path = Path(config)
    try:
        with path.open() as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        click.echo(click.style(f"  ✗ Invalid YAML: {exc}", fg="red"))
        raise SystemExit(1) from None

    if not raw or not isinstance(raw, dict):
        click.echo(click.style("  ✗ File is empty or not a YAML mapping", fg="red"))
        raise SystemExit(1)

    errors = validate_council_config(raw)
    if errors:
        click.echo()
        click.echo(click.style("  ✗ ", fg="red") + click.style("[council]", fg="cyan") + f" {path}")
        for err in errors:
            click.echo(click.style(f"    → {err}", fg="red"))
        click.echo()
        raise SystemExit(1)

    # Valid — show details.
    click.echo()
    click.echo(click.style("  ✓ ", fg="green") + click.style("[council]", fg="cyan") + f" {path}")

    name = raw.get("name", "unnamed")
    agents = raw.get("agents", [])
    protocol = raw.get("protocol", "round_robin")
    convergence = raw.get("convergence", {})
    method = convergence.get("method", "none") if isinstance(convergence, dict) else "none"
    max_rounds = raw.get("max_rounds", 4)

    click.echo(f"    Name:        {name}")
    click.echo(f"    Agents:      {len(agents)}")
    click.echo(f"    Protocol:    {protocol}")
    click.echo(f"    Convergence: {method}")
    click.echo(f"    Max rounds:  {max_rounds}")

    # Collect required tiers.
    tiers_needed: set[str] = set()
    for agent in agents:
        tiers_needed.add(agent.get("tier", "standard"))
    facilitator = raw.get("facilitator", {})
    if isinstance(facilitator, dict):
        tiers_needed.add(facilitator.get("tier", "standard"))

    # Check backend availability.
    has_ollama = bool(os.getenv("OLLAMA_URL"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))

    tier_available = {
        "local": has_ollama,
        "standard": has_anthropic,
        "frontier": has_anthropic,
    }

    click.echo("    Tiers:")
    for tier in sorted(tiers_needed):
        available = tier_available.get(tier, False)
        if available:
            status = click.style("✓", fg="green")
        else:
            status = click.style("✗ (not configured)", fg="yellow")
        click.echo(f"      {tier}: {status}")

    click.echo()
