#!/usr/bin/env python3
r"""Town Hall Debate — interactive council demo with audience participation.

Run a structured debate between LLM agents while you heckle, question,
and challenge them from the audience.  Your interjections appear in
the agents' context as "[AUDIENCE REACTIONS]" — they may engage with
your points or ignore them.

Usage::

    # Basic — watch the debate
    python examples/town-hall/run.py \\
        configs/councils/town_hall_debate.yaml \\
        --topic "Remote work is better than office work"

    # Interactive — type messages while the debate runs
    python examples/town-hall/run.py \\
        configs/councils/town_hall_debate.yaml \\
        --topic "AI will replace most knowledge workers within 10 years" \\
        --interactive

    # Save the full transcript
    python examples/town-hall/run.py \\
        configs/councils/town_hall_debate.yaml \\
        --topic "Open source beats proprietary software" \\
        --interactive --output debate_result.json

Requires: pip install heddle-ai[council]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

from heddle.contrib.council.config import load_council_config
from heddle.contrib.council.runner import CouncilRunner
from heddle.contrib.council.schemas import TranscriptEntry
from heddle.worker.backends import build_backends_from_env

# -- Terminal colors --------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"

AGENT_COLORS = {
    "advocate": GREEN,
    "opponent": RED,
    "moderator": CYAN,
}


def print_banner(topic: str, agents: list[str]) -> None:
    """Print the debate header."""
    width = 60
    print()
    print(f"{CYAN}{BOLD}{'═' * width}")
    print("  TOWN HALL DEBATE")
    print(f"{'═' * width}{RESET}")
    print()
    print(f"  {BOLD}Topic:{RESET} {topic}")
    print(f"  {BOLD}Panel:{RESET} {', '.join(agents)}")
    print()
    print(f"{DIM}  {'─' * (width - 4)}{RESET}")
    print()


def print_turn(entry: TranscriptEntry) -> None:
    """Print a single agent turn with color coding."""
    color = AGENT_COLORS.get(entry.agent_name, YELLOW)
    label = entry.agent_name.upper()

    print(f"\n  {color}{BOLD}[{label}]{RESET} {DIM}(round {entry.round_num}){RESET}")
    print()

    # Word-wrap content at ~72 chars, indented.
    content = entry.content.strip()
    for paragraph in content.split("\n"):
        words = paragraph.split()
        line = "    "
        for word in words:
            if len(line) + len(word) + 1 > 76:
                print(line)
                line = "    " + word
            else:
                line += (" " if len(line) > 4 else "") + word
        if line.strip():
            print(line)
    print()


def start_audience_input(runner: CouncilRunner, speaker_name: str) -> None:
    """Start a background thread that reads stdin for audience interjections.

    Each line typed is injected into the running council as an audience
    interjection.  The agents will see it in their next turn's context
    under "[AUDIENCE REACTIONS]".
    """

    def _input_loop() -> None:
        print(
            f"  {MAGENTA}{BOLD}You are in the audience!{RESET}\n"
            f"  {DIM}Type a message and press Enter to shout at the panel.{RESET}\n"
            f"  {DIM}Your message appears in the agents' next turn "
            f"as an audience reaction.{RESET}\n"
            f"  {DIM}Press Ctrl+C or wait for the debate to end.{RESET}\n"
        )
        while True:
            try:
                line = input(f"  {MAGENTA}[{speaker_name}] > {RESET}")
                if line.strip():
                    try:
                        runner.inject(speaker_name, line.strip())
                        print(f"  {DIM}(interjection sent){RESET}")
                    except RuntimeError:
                        # Council finished before we could inject.
                        break
            except (EOFError, KeyboardInterrupt):
                break

    thread = threading.Thread(target=_input_loop, daemon=True)
    thread.start()
    return thread


async def run_debate(
    config_path: str,
    topic: str,
    interactive: bool = False,
    speaker_name: str = "audience_member",
    output_path: str | None = None,
) -> None:
    """Run the town hall debate."""
    config = load_council_config(config_path)
    backends = build_backends_from_env()

    if not backends:
        print(f"\n  {RED}No LLM backends available.{RESET}")
        print("  Set OLLAMA_URL or ANTHROPIC_API_KEY and try again.\n")
        sys.exit(1)

    agent_names = [a.name for a in config.agents]
    print_banner(topic, agent_names)

    runner = CouncilRunner(backends, config=config)

    # Start audience input thread if interactive.
    if interactive:
        start_audience_input(runner, speaker_name)

    # Run the council with live output.
    result = await runner.run(topic, on_turn=print_turn)

    # Print synthesis.
    width = 60
    print(f"\n  {CYAN}{BOLD}{'═' * width}")
    print("  FACILITATOR'S VERDICT")
    print(f"{'═' * width}{RESET}\n")

    for line in result.synthesis.strip().splitlines():
        print(f"    {line}")

    # Print summary stats.
    converged = f"{GREEN}yes{RESET}" if result.converged else f"{YELLOW}no{RESET}"
    score = f"{result.convergence_score:.2f}" if result.convergence_score else "n/a"
    tokens = sum(result.total_token_usage.values())
    elapsed = result.elapsed_ms / 1000

    print(f"\n  {DIM}{'─' * (width - 4)}{RESET}")
    print(f"  Rounds: {result.rounds_completed}  |  Converged: {converged}  |  Score: {score}")
    print(f"  Tokens: {tokens:,}  |  Time: {elapsed:.1f}s")

    # Count interjections in transcript.
    interjection_count = sum(
        1 for r in result.transcript for e in r.entries if e.entry_type == "interjection"
    )
    if interjection_count:
        print(f"  Audience interjections: {MAGENTA}{interjection_count}{RESET}")
    print()

    # Write JSON output if requested.
    if output_path:
        Path(output_path).write_text(result.model_dump_json(indent=2))
        print(f"  Full result written to {output_path}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Town Hall Debate — interactive council demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run.py council.yaml --topic 'AI will replace most jobs'\n"
            "  python run.py council.yaml --topic 'Open source wins' --interactive\n"
            "  python run.py council.yaml --topic topic.txt --interactive --name Hooman\n"
        ),
    )
    parser.add_argument("config", help="Path to council YAML config")
    parser.add_argument("--topic", "-t", required=True, help="Debate topic (string or file path)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Enable audience input")
    parser.add_argument("--name", "-n", default="audience_member", help="Your speaker name")
    parser.add_argument("--output", "-o", help="Write full JSON result to file")
    args = parser.parse_args()

    # Resolve topic from file if it's a path.
    topic = args.topic
    if Path(topic).is_file():
        topic = Path(topic).read_text().strip()

    asyncio.run(
        run_debate(
            config_path=args.config,
            topic=topic,
            interactive=args.interactive,
            speaker_name=args.name,
            output_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
