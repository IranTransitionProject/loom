#!/usr/bin/env python3
r"""Debate Arena — round-robin LLM debate tournament with judge panel.

Runs every model against every other model on a list of topics, scores
each debate via a panel of out-of-family judges, and prints a
leaderboard plus a head-to-head matchup matrix.

Inspired by Lech Mazur's debate benchmark methodology
(https://github.com/lechmazur/debate) — but built entirely on Heddle
Council + ChatBridge primitives.

Usage::

    # Three Ollama models, two Anthropic judges, default topics
    python examples/debate-arena/run.py \\
        configs/councils/debate_arena.yaml \\
        --models llama3.2:3b,qwen2.5:7b,mistral:7b \\
        --topics examples/debate-arena/topics.txt \\
        --judges claude-sonnet-4-20250514,claude-opus-4-20250514 \\
        --output debate_results.json

    # Single-provider (all debaters Anthropic, judges different family)
    python examples/debate-arena/run.py \\
        configs/councils/debate_arena.yaml \\
        --models sonnet,opus,haiku \\
        --topics examples/debate-arena/topics.txt \\
        --judges gpt-4o,gpt-4-turbo

Notes:

- Debater models are LABELS in single-provider mode.  Heddle's
  CouncilRunner picks a backend by tier, so all debaters share the
  same backend; differentiation comes from the system prompt and the
  role each debater plays.
- Judges should be from a different model family than the debaters
  (Mazur's anti-bias rule).  The scorer is unaware of who the
  debaters are — it just sees the transcript.

Requires: pip install heddle-ai[council,chatbridge]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from heddle.contrib.chatbridge.discover import chatbridge_spec, make_chatbridge
from heddle.contrib.council.config import load_council_config
from heddle.contrib.council.runner import CouncilRunner
from heddle.contrib.council.schemas import AgentConfig
from heddle.contrib.council.scorer import JudgePanelScorer
from heddle.contrib.council.tournament import MatchupResult, TournamentRunner
from heddle.core.messages import ModelTier
from heddle.worker.backends import build_backends_from_env

if TYPE_CHECKING:
    from heddle.contrib.chatbridge.base import ChatBridge


# -- Terminal colors --------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"

# Stable palette for model labels (cycled).
MODEL_PALETTE = [GREEN, CYAN, MAGENTA, YELLOW, RED]


def color_for(model: str, models: list[str]) -> str:
    return MODEL_PALETTE[models.index(model) % len(MODEL_PALETTE)]


# -- Default agent_factory --------------------------------------------------


def default_agent_factory(model_key: str, role: str, topic: str) -> AgentConfig:
    """Build a debater AgentConfig backed by a real :class:`ChatBridge`.

    Model differentiation is genuine, not just label-only — each
    debater is dispatched through its own ChatBridge (Anthropic /
    OpenAI / LM Studio / Ollama, picked from the model name by
    :func:`chatbridge_spec`).  The runner overrides ``name`` to match
    the template's ``pro`` / ``con`` slot, so the scorer's winner
    field stays stable across matchups.
    """
    del topic  # default factory does not customize per topic
    bridge_path, bridge_kwargs = chatbridge_spec(model_key)
    return AgentConfig(
        name=f"debater_{model_key}",
        bridge=bridge_path,
        bridge_config=bridge_kwargs,
        tier=ModelTier.STANDARD,  # ignored when ``bridge`` is set
        role=f"You are debater '{model_key}'. {role}",
        max_tokens_per_turn=1000,
    )


# -- Output helpers ---------------------------------------------------------


def print_banner(models: list[str], topics: list[str], judges: list[str]) -> None:
    width = 64
    print()
    print(f"{CYAN}{BOLD}{'=' * width}")
    print("  HEDDLE DEBATE ARENA")
    print(f"{'=' * width}{RESET}")
    print(f"  {BOLD}Models:{RESET} {', '.join(models)}")
    print(f"  {BOLD}Topics:{RESET} {len(topics)}")
    print(f"  {BOLD}Judges:{RESET} {', '.join(judges)}")
    print()


def print_matchup_done(
    result: MatchupResult,
    index: int,
    total: int,
    models: list[str],
) -> None:
    m = result.matchup
    pro_color = color_for(m.pro_model, models)
    con_color = color_for(m.con_model, models)
    head = (
        f"  [{index:>2}/{total}] {pro_color}{m.pro_model}{RESET} (PRO) "
        f"vs {con_color}{m.con_model}{RESET} (CON)  "
        f"{DIM}{m.topic[:40]}{'...' if len(m.topic) > 40 else ''}{RESET}"
    )
    if result.error:
        print(f"{head}\n      {RED}ERROR{RESET} {result.error}")
        return

    scoring = result.scoring
    if scoring is None:
        print(f"{head}\n      {YELLOW}no scoring{RESET}")
        return

    if scoring.winner is None:
        print(f"{head}\n      {YELLOW}draw{RESET}  ({DIM}{result.elapsed_ms} ms{RESET})")
        return

    winner_model = result.winner_model("pro", "con")
    if winner_model is None:
        print(f"{head}\n      {YELLOW}draw{RESET}  ({DIM}{result.elapsed_ms} ms{RESET})")
        return
    color = color_for(winner_model, models)
    print(
        f"{head}\n      {BOLD}{color}{winner_model}{RESET} wins  "
        f"margin={scoring.win_margin:.2f}  ({DIM}{result.elapsed_ms} ms{RESET})"
    )


def print_leaderboard(rows: list[dict], models: list[str]) -> None:
    width = 64
    print(f"\n{CYAN}{BOLD}{'=' * width}")
    print("  LEADERBOARD")
    print(f"{'=' * width}{RESET}\n")

    print(
        f"  {'rank':<4} {'model':<24} {'W':>3} {'L':>3} {'D':>3} {'win_rate':>9} {'avg_margin':>11}"
    )
    print(f"  {DIM}{'-' * (width - 4)}{RESET}")
    for rank, row in enumerate(rows, 1):
        color = color_for(row["model"], models)
        print(
            f"  {rank:<4} {color}{row['model']:<24}{RESET} "
            f"{row['wins']:>3} {row['losses']:>3} {row['draws']:>3} "
            f"{row['win_rate']:>9.3f} {row['avg_margin']:>11.3f}"
        )
    print()


def print_matrix(matrix: dict, models: list[str]) -> None:
    width = 64
    print(f"\n{CYAN}{BOLD}{'=' * width}")
    print("  MATCHUP MATRIX (rows = model, cells = wins-losses-draws)")
    print(f"{'=' * width}{RESET}\n")

    short = {m: m if len(m) <= 12 else m[:11] + "*" for m in models}
    header = " " * 14 + "  ".join(f"{short[m]:>12}" for m in models)
    print(f"  {DIM}{header}{RESET}")
    for m in models:
        row_color = color_for(m, models)
        cells = []
        for n in models:
            if n == m:
                cells.append(f"{DIM}{'—':>12}{RESET}")
                continue
            stats = matrix[m][n]
            cell = f"{stats['wins']}-{stats['losses']}-{stats['draws']}"
            cells.append(f"{cell:>12}")
        print(f"  {row_color}{m[:12]:<12}{RESET}  " + "  ".join(cells))
    print()


# -- Main -------------------------------------------------------------------


async def run_tournament(args: argparse.Namespace) -> None:
    config = load_council_config(args.config)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]

    topics_path = Path(args.topics)
    if topics_path.is_file():
        topics = [line.strip() for line in topics_path.read_text().splitlines() if line.strip()]
    else:
        topics = [t.strip() for t in args.topics.split(",") if t.strip()]

    if len(models) < 2:
        print(f"{RED}Need at least 2 debater models.{RESET}")
        sys.exit(1)
    if not judges:
        print(f"{RED}Need at least 1 judge model.{RESET}")
        sys.exit(1)
    if not topics:
        print(f"{RED}Need at least 1 topic.{RESET}")
        sys.exit(1)

    backends = build_backends_from_env()
    if not backends:
        print(f"{RED}No LLM backends available.{RESET}")
        print("Set OLLAMA_URL or ANTHROPIC_API_KEY (or both) and try again.")
        sys.exit(1)

    print_banner(models, topics, judges)

    council_runner = CouncilRunner(backends)
    judge_bridges = [make_chatbridge(j) for j in judges]
    scorer = JudgePanelScorer(judges=judge_bridges)
    tournament = TournamentRunner(
        runner=council_runner,
        scorer=scorer,
        config_template=config,
        agent_factory=default_agent_factory,
    )

    matchups = TournamentRunner.generate_matchups(
        models=models,
        topics=topics,
        both_sides=True,
    )

    total = len(matchups)
    print(f"  {DIM}Running {total} matchup(s) at concurrency={args.concurrency}...{RESET}\n")

    counter = {"i": 0}

    def _on_done(result: MatchupResult) -> None:
        counter["i"] += 1
        print_matchup_done(result, counter["i"], total, models)

    result = await tournament.run(
        matchups=matchups,
        on_matchup_done=_on_done,
        concurrency=args.concurrency,
    )

    print_leaderboard(result.leaderboard, models)
    print_matrix(result.matchup_matrix, models)

    summary_color = GREEN if result.failed_matchups == 0 else YELLOW
    print(
        f"  {summary_color}Tournament complete.{RESET}  "
        f"{result.completed_matchups}/{result.total_matchups} matchups OK, "
        f"{result.failed_matchups} failed.  "
        f"{DIM}Elapsed: {result.elapsed_ms / 1000:.1f}s{RESET}\n"
    )

    if args.output:
        Path(args.output).write_text(json.dumps(result.model_dump(), indent=2))
        print(f"  Full results written to {args.output}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Heddle Debate Arena — round-robin LLM debate tournament",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", help="Path to debate_arena council YAML config")
    parser.add_argument(
        "--models",
        required=True,
        help="Comma-separated debater model labels (e.g. llama3.2:3b,qwen2.5:7b)",
    )
    parser.add_argument(
        "--topics",
        required=True,
        help="Path to topics file (one per line) OR comma-separated topics",
    )
    parser.add_argument(
        "--judges",
        required=True,
        help="Comma-separated judge model names (e.g. claude-sonnet-4-20250514,gpt-4o)",
    )
    parser.add_argument(
        "--output",
        help="Write full TournamentResult JSON to this path",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of matchups to run in parallel (default: 1)",
    )
    args = parser.parse_args()
    asyncio.run(run_tournament(args))


if __name__ == "__main__":
    main()
