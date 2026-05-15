#!/usr/bin/env python3
r"""Blind Taste Test — anonymous LLM evaluation across many prompts.

Multiple LLMs answer the same question, independently and in parallel.
A panel of out-of-family judges grades every response on every rubric
dimension — but the transcript they see has names replaced with
``Participant A`` / ``B`` / ``C``.  After all prompts run, the script
maps the anonymous labels back to model identities and reveals the
winner.

This is the post-hoc-judging cousin of debate-arena: the models do not
compete with each other directly, they answer the same prompt
independently and are graded on raw answer quality.

Usage::

    # Three real models (Claude + GPT + a local LM Studio model),
    # judged by two Anthropic models, 10 prompts from the file:
    python examples/blind-taste-test/run.py \\
        configs/councils/blind_taste_test.yaml \\
        --models claude-sonnet-4-20250514,gpt-4o,lmstudio/google/gemma-3-4b \\
        --prompts examples/blind-taste-test/prompts.txt \\
        --judges claude-opus-4-20250514,gpt-4o-mini \\
        --output blind_taste_results.json

    # Single ad-hoc prompt:
    python examples/blind-taste-test/run.py \\
        configs/councils/blind_taste_test.yaml \\
        --models claude-sonnet-4-20250514,gpt-4o \\
        --prompts "Explain SQL injection to a five-year-old" \\
        --judges claude-opus-4-20250514

The model strings flow through :func:`heddle.contrib.chatbridge.chatbridge_spec`,
which routes ``claude*`` → Anthropic, ``gpt*`` → OpenAI,
``lmstudio/<id>`` → LM Studio, anything else → Ollama.

Requires: pip install heddle-ai[council,chatbridge]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from heddle.contrib.chatbridge.discover import chatbridge_spec, make_chatbridge
from heddle.contrib.council.config import load_council_config
from heddle.contrib.council.runner import CouncilRunner
from heddle.contrib.council.schemas import AgentConfig
from heddle.contrib.council.scorer import RubricScorer
from heddle.core.messages import ModelTier

if TYPE_CHECKING:
    from heddle.contrib.council.config import CouncilConfig
    from heddle.contrib.council.schemas import CouncilResult


# -- Terminal colors --------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"

PARTICIPANT_PALETTE = [GREEN, CYAN, MAGENTA, YELLOW, RED]


def participant_color(idx: int) -> str:
    return PARTICIPANT_PALETTE[idx % len(PARTICIPANT_PALETTE)]


@dataclass
class _ResolvedArgs:
    models: list[str]
    judges: list[str]
    rubric_fields: list[str] | None
    prompts: list[str]


# -- Agent factory ---------------------------------------------------------


def build_agent(model_key: str, slot_name: str, role: str) -> AgentConfig:
    """Wrap a model id in an :class:`AgentConfig` bound to its ChatBridge."""
    bridge_path, bridge_kwargs = chatbridge_spec(model_key)
    return AgentConfig(
        name=slot_name,
        bridge=bridge_path,
        bridge_config=bridge_kwargs,
        tier=ModelTier.STANDARD,  # ignored when ``bridge`` is set
        role=role,
        sees_transcript_from=[],
        max_tokens_per_turn=1500,
    )


# -- Output helpers --------------------------------------------------------


def print_banner(models: list[str], judges: list[str], prompt_count: int) -> None:
    width = 64
    print()
    print(f"{CYAN}{BOLD}{'=' * width}")
    print("  HEDDLE BLIND TASTE TEST")
    print(f"{'=' * width}{RESET}")
    print(f"  {BOLD}Models:{RESET}  {', '.join(models)}")
    print(f"  {BOLD}Judges:{RESET}  {', '.join(judges)}")
    print(f"  {BOLD}Prompts:{RESET} {prompt_count}")
    print()


def print_round_header(prompt: str, idx: int, total: int) -> None:
    width = 64
    print(f"{CYAN}{BOLD}{'=' * width}")
    print(f"  ROUND {idx} of {total}")
    print(f"{'=' * width}{RESET}")
    short = prompt if len(prompt) <= 88 else prompt[:85] + "..."
    print(f'  {BOLD}Prompt:{RESET} "{short}"')
    print()


def print_anonymous_rankings(
    per_prompt: list[dict[str, Any]],
    aliases: list[str],
) -> None:
    width = 64
    print(f"\n{CYAN}{BOLD}{'=' * width}")
    print("  ANONYMOUS RANKINGS")
    print(f"{'=' * width}{RESET}\n")

    # Aggregate per (alias, dimension) across prompts.
    dim_totals: dict[str, dict[str, list[float]]] = {a: {} for a in aliases}
    for entry in per_prompt:
        for alias, dims in entry["per_alias_rubric"].items():
            for dim, val in dims.items():
                dim_totals[alias].setdefault(dim, []).append(val)

    overall: list[tuple[str, float]] = []
    for alias in aliases:
        means = [sum(vals) / len(vals) for vals in dim_totals[alias].values() if vals]
        overall.append((alias, sum(means) / len(means) if means else 0.0))
    overall.sort(key=lambda x: x[1], reverse=True)

    for rank, (alias, score) in enumerate(overall, 1):
        color = participant_color(aliases.index(alias))
        print(f"  #{rank}  {color}{alias:<14}{RESET}  avg: {score:.3f}")

    # Per-dimension breakdown table
    all_dims = sorted({d for a in aliases for d in dim_totals[a]})
    if all_dims:
        print()
        print(f"  {DIM}Per-dimension means:{RESET}")
        header = "       " + "  ".join(f"{d[:10]:>10}" for d in all_dims)
        print(f"  {DIM}{header}{RESET}")
        for alias in aliases:
            color = participant_color(aliases.index(alias))
            short = alias.replace("Participant ", "P-")
            row_vals = []
            for d in all_dims:
                vals = dim_totals[alias].get(d, [])
                row_vals.append(f"{(sum(vals) / len(vals) if vals else 0.0):>10.3f}")
            print(f"  {color}{short:<5}{RESET}  " + "  ".join(row_vals))


def print_reveal(
    per_prompt: list[dict[str, Any]],
    aliases: list[str],
    alias_to_model: dict[str, str],
) -> dict[str, float]:
    """Reveal which model is which and print the final leaderboard."""
    width = 64
    print(f"\n{CYAN}{BOLD}{'=' * width}")
    print("  THE REVEAL")
    print(f"{'=' * width}{RESET}\n")

    # Compute final per-model overall.
    final: dict[str, float] = {}
    dim_totals: dict[str, dict[str, list[float]]] = {a: {} for a in aliases}
    for entry in per_prompt:
        for alias, dims in entry["per_alias_rubric"].items():
            for dim, val in dims.items():
                dim_totals[alias].setdefault(dim, []).append(val)
    for alias in aliases:
        means = [sum(vals) / len(vals) for vals in dim_totals[alias].values() if vals]
        final[alias_to_model[alias]] = sum(means) / len(means) if means else 0.0

    ranked = sorted(final.items(), key=lambda x: x[1], reverse=True)
    winner_model = ranked[0][0] if ranked else None

    for alias in aliases:
        model = alias_to_model[alias]
        color = participant_color(aliases.index(alias))
        crown = f"  {BOLD}{YELLOW}★ WINNER{RESET}" if model == winner_model else ""
        print(f"  {color}{alias}{RESET}  →  {BOLD}{model}{RESET}{crown}")

    print()
    print(f"  {DIM}Final leaderboard{RESET}\n")
    max_score = max((s for _, s in ranked), default=1.0) or 1.0
    for rank, (model, score) in enumerate(ranked, 1):
        bar_width = 20
        filled = round(score / max_score * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        marker = f" {BOLD}{YELLOW}★{RESET}" if rank == 1 else ""
        print(f"  #{rank}  {BOLD}{model:<32}{RESET}  {score:.3f}  {bar}{marker}")
    print()
    return final


# -- Main flow -------------------------------------------------------------


async def run_one_prompt(
    config_template: CouncilConfig,
    models: list[str],
    prompt_text: str,
    aliases: list[str],
    alias_to_model: dict[str, str],
    judges: list[str],
    rubric_fields: list[str] | None,
) -> dict[str, Any]:
    """Run one council + scoring round for a single prompt.

    Returns a dict with keys:
      - ``per_alias_rubric``: ``{alias: {dim: score}}`` averaged
        across judges
      - ``transcript``: dict with raw responses keyed by alias (for
        the JSON output)
      - ``raw_verdicts``: list of judge verdict dicts
      - ``elapsed_ms``: wall time
    """
    config = config_template.model_copy(deep=True)
    slot_names = [a.name for a in config.agents]
    if len(models) > len(slot_names):
        msg = (
            f"Council template has {len(slot_names)} agent slots but "
            f"{len(models)} models were requested"
        )
        raise ValueError(msg)

    # Replace placeholder agents with real bridge-backed ones.  Roles
    # come from the template so the YAML stays the source of truth
    # for blind-evaluation framing.
    new_agents = []
    for slot, model_key in zip(slot_names, models, strict=False):
        # Find the original role text from the template.
        original = next(a for a in config.agents if a.name == slot)
        new_agents.append(build_agent(model_key, slot_name=slot, role=original.role))
    # Trim to len(models) so the slot count matches model count.
    config.agents = new_agents[: len(models)]

    runner = CouncilRunner(config=config)
    try:
        start = time.perf_counter()
        result: CouncilResult = await runner.run(prompt_text, config=config)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
    finally:
        await runner.aclose()

    # Build judge bridges and a stable alias-map keyed by slot order
    # so ``Participant A`` always tracks ``models[0]`` etc.
    judge_bridges = [make_chatbridge(j) for j in judges]
    fixed_alias_map = dict(zip(slot_names[: len(models)], aliases, strict=False))

    scorer = _FixedAliasRubricScorer(
        judges=judge_bridges,
        alias_map=fixed_alias_map,
        rubric_fields=rubric_fields,
    )
    scoring = await scorer.score(result)

    per_alias_rubric: dict[str, dict[str, float]] = {}
    for agent_score in scoring.agent_scores:
        alias = fixed_alias_map.get(agent_score.agent_name, agent_score.agent_name)
        per_alias_rubric[alias] = dict(agent_score.rubric)

    transcript = {
        fixed_alias_map.get(e.agent_name, e.agent_name): e.content
        for r in result.transcript
        for e in r.entries
        if e.entry_type == "turn"
    }

    return {
        "prompt": prompt_text,
        "alias_map": fixed_alias_map,
        "alias_to_model": alias_to_model,
        "per_alias_rubric": per_alias_rubric,
        "transcript": transcript,
        "synthesis": result.synthesis,
        "raw_verdicts": scoring.metadata.get("rubric_verdicts", []),
        "best_response_votes": scoring.metadata.get("best_response_votes", {}),
        "elapsed_ms": elapsed_ms,
    }


class _FixedAliasRubricScorer(RubricScorer):
    """RubricScorer variant that uses a caller-supplied alias map.

    The base class derives aliases by sorting agent names; the
    blind-taste-test wants ``Participant A`` to track ``--models[0]``
    (the first model the user named on the CLI), regardless of how
    the slot names sort alphabetically.
    """

    def __init__(self, *, alias_map: dict[str, str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixed_alias_map = dict(alias_map)

    async def score(self, result):  # type: ignore[override]
        agent_names = [
            name
            for name in self._fixed_alias_map
            if any(e.agent_name == name for r in result.transcript for e in r.entries)
        ]
        # Fall back to base behaviour if no overlap (defensive).
        if not agent_names:
            return await super().score(result)
        alias_map = {n: self._fixed_alias_map[n] for n in agent_names}

        transcript_text = self._format_anonymized(result.transcript, alias_map)
        prompt = self.scoring_prompt.format(
            transcript=transcript_text,
            topic=result.topic,
            participants=", ".join(alias_map.values()),
            rubric_fields=", ".join(self.rubric_fields),
        )
        verdicts = await self._collect_verdicts(prompt, set(alias_map.values()))
        agent_scores = self._aggregate(verdicts, agent_names, alias_map)
        winner, win_margin = self._pick_winner(agent_scores)

        from heddle.contrib.council.scorer import ScoringResult

        reverse = {a: n for n, a in alias_map.items()}
        best_votes: dict[str, int] = {}
        for v in verdicts:
            agent = reverse.get(v.best_response)
            if agent:
                best_votes[agent] = best_votes.get(agent, 0) + 1

        return ScoringResult(
            council_topic=result.topic,
            agent_scores=agent_scores,
            verdicts=[],
            winner=winner,
            win_margin=win_margin,
            metadata={
                "scoring_mode": "per_participant_rubric",
                "alias_map": alias_map,
                "judge_count": len(self.judges),
                "verdict_count": len(verdicts),
                "rubric_fields": list(self.rubric_fields),
                "best_response_votes": best_votes,
                "rubric_verdicts": [v.model_dump() for v in verdicts],
            },
        )


def _parse_args(args: argparse.Namespace, config_template: CouncilConfig) -> _ResolvedArgs:
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if len(models) < 2:
        print(f"{RED}Need at least 2 models.{RESET}")
        sys.exit(1)
    if len(models) > len(config_template.agents):
        print(
            f"{RED}Council template has only {len(config_template.agents)} "
            f"agent slots — cannot run {len(models)} models.{RESET}"
        )
        sys.exit(1)

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    if not judges:
        print(f"{RED}Need at least 1 judge.{RESET}")
        sys.exit(1)

    rubric_fields = (
        [s.strip() for s in args.rubric.split(",") if s.strip()] if args.rubric else None
    )

    prompt_path = Path(args.prompts)
    if prompt_path.is_file():
        prompts = [
            line.strip()
            for line in prompt_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        prompts = [args.prompts]
    if not prompts:
        print(f"{RED}No prompts found.{RESET}")
        sys.exit(1)

    return _ResolvedArgs(
        models=models,
        judges=judges,
        rubric_fields=rubric_fields,
        prompts=prompts,
    )


async def main_async(args: argparse.Namespace) -> None:
    config_template = load_council_config(args.config)
    resolved = _parse_args(args, config_template)
    models, judges, prompts = resolved.models, resolved.judges, resolved.prompts

    aliases = [f"Participant {chr(65 + i)}" for i in range(len(models))]
    alias_to_model = dict(zip(aliases, models, strict=False))

    print_banner(models, judges, len(prompts))

    per_prompt_results: list[dict[str, Any]] = []
    for idx, prompt in enumerate(prompts, 1):
        print_round_header(prompt, idx, len(prompts))
        for i, alias in enumerate(aliases):
            print(f"  {participant_color(i)}[{alias}]{RESET} {DIM}(answering...){RESET}")
        try:
            entry = await run_one_prompt(
                config_template=config_template,
                models=models,
                prompt_text=prompt,
                aliases=aliases,
                alias_to_model=alias_to_model,
                judges=judges,
                rubric_fields=resolved.rubric_fields,
            )
        except Exception as e:
            print(f"  {RED}Prompt failed: {e}{RESET}")
            continue
        n_judges = len(entry["raw_verdicts"])
        ticks = "✓ " * n_judges
        print(
            f"  {DIM}Judging anonymously...{RESET} "
            f"{GREEN}{ticks.strip()}{RESET}  "
            f"{DIM}({entry['elapsed_ms']} ms){RESET}"
        )
        per_prompt_results.append(entry)
        print()

    if not per_prompt_results:
        print(f"{RED}No prompts completed successfully.{RESET}")
        sys.exit(1)

    print_anonymous_rankings(per_prompt_results, aliases)

    if args.reveal:
        # A small visual beat before the reveal — looks better in screenshots.
        print(f"\n  {DIM}...drumroll...{RESET}")
        final = print_reveal(per_prompt_results, aliases, alias_to_model)
    else:
        final = {}

    if args.output:
        payload = {
            "models": models,
            "judges": judges,
            "rubric_fields": (resolved.rubric_fields or list(RubricScorer.DEFAULT_RUBRIC_FIELDS)),
            "alias_to_model": alias_to_model,
            "per_prompt_results": per_prompt_results,
            "final_scores_by_model": final,
        }
        Path(args.output).write_text(json.dumps(payload, indent=2))
        print(f"  Full results written to {args.output}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Heddle Blind Taste Test — anonymous LLM evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", help="Path to blind_taste_test council YAML config")
    parser.add_argument(
        "--models",
        "-m",
        required=True,
        help=(
            "Comma-separated debater model ids "
            "(e.g. claude-sonnet-4-20250514,gpt-4o,lmstudio/google/gemma-3-4b)"
        ),
    )
    parser.add_argument(
        "--prompts",
        "-p",
        required=True,
        help="Path to prompts file (one per line) OR a single inline prompt",
    )
    parser.add_argument(
        "--judges",
        "-j",
        required=True,
        help="Comma-separated judge model ids (out-of-family preferred)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Write full per-prompt results as JSON to this path",
    )
    parser.add_argument(
        "--rubric",
        help=(
            "Comma-separated rubric dimensions to score on "
            "(default: accuracy,depth,clarity,creativity,conciseness)"
        ),
    )
    parser.add_argument(
        "--reveal/--no-reveal",
        dest="reveal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show the identity reveal at the end (default: yes)",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
