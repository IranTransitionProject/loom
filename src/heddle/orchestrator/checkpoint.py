"""
Self-summarization checkpoint system for orchestrators.

The orchestrator's context is precious. This module compresses
conversation history into structured state snapshots at defined intervals,
allowing the orchestrator to "reboot" with a clean, compact understanding
of where things stand.

Checkpoint trigger: when estimated token count exceeds threshold.

Storage: Pluggable via CheckpointStore (see orchestrator/store.py).
Keys follow the pattern::

    heddle:checkpoint:{goal_id}:{checkpoint_number}  — versioned checkpoint
    heddle:checkpoint:{goal_id}:latest                — pointer to most recent

The orchestrator workflow with checkpoints::

    1. Process goal, accumulate conversation_history
    2. After each worker result: should_checkpoint(conversation_history)
    3. If True: create_checkpoint() → compress state → persist to store
    4. Orchestrator "reboots" with: system_prompt + format_for_injection(checkpoint)
       + last N interactions (recent_window_size)

This is conceptually similar to how Claude Code itself handles context
compression — the key insight is the same: keep a structured summary +
recent window rather than the full history.

NOTE: This module is used by OrchestratorActor (runner.py).
      PipelineOrchestrator does NOT use checkpoints because its sequential
      stage execution doesn't accumulate unbounded context.

Note: Token counting uses tiktoken with cl100k_base encoding (OpenAI's tokenizer).
For Anthropic models, token counts are approximate (~10-15% estimation error).
This is acceptable for checkpoint threshold decisions where exact counts are
not critical.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
import tiktoken

from heddle.core.messages import CheckpointState

if TYPE_CHECKING:
    from heddle.orchestrator.store import CheckpointStore

logger = structlog.get_logger()


class CheckpointManager:
    """Manages orchestrator state compression.

    Workflow:

    1. After each worker result, estimate_tokens() checks context size
    2. If threshold exceeded, create_checkpoint() asks a summarizer
       to compress the current state
    3. The orchestrator restarts with: system_prompt + checkpoint + recent_window
    """

    def __init__(
        self,
        store: CheckpointStore,
        token_threshold: int = 50_000,  # Trigger checkpoint at this count
        recent_window_size: int = 5,  # Keep last N interactions in detail
        encoding_name: str = "cl100k_base",
        ttl_seconds: int = 86400,  # Key expiry (default: 24h)
    ) -> None:
        self.store = store
        self.token_threshold = token_threshold
        self.recent_window_size = recent_window_size
        self.encoder = tiktoken.get_encoding(encoding_name)
        self.ttl_seconds = ttl_seconds

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for a string."""
        return len(self.encoder.encode(text))

    def should_checkpoint(self, conversation_history: list[dict[str, Any]]) -> bool:
        """Check if context has grown enough to trigger compression."""
        total = sum(self.estimate_tokens(json.dumps(msg)) for msg in conversation_history)
        return total > self.token_threshold

    async def create_checkpoint(
        self,
        goal_id: str,
        original_instruction: str,
        completed_tasks: list[dict[str, Any]],
        pending_tasks: list[dict[str, Any]],
        open_issues: list[str],
        decisions_made: list[str],
        checkpoint_number: int,
    ) -> CheckpointState:
        """Build a checkpoint.

        The orchestrator or a dedicated summarizer compresses current state
        into this structure.
        """
        # Build executive summary from completed task outcomes.
        # Only the last 20 tasks are included to keep the summary concise.
        # Of those, only the last 10 are rendered into the summary text.
        outcomes = []
        for t in completed_tasks[-20:]:
            status = t.get("status", "unknown")
            summary = t.get("summary", t.get("worker_type", "task"))
            outcomes.append(f"- [{status}] {summary}")

        executive_summary = (
            f"Goal: {original_instruction}\n"
            f"Progress: {len(completed_tasks)} completed, {len(pending_tasks)} pending.\n"
            f"Recent outcomes:\n" + "\n".join(outcomes[-10:])
        )

        total_tokens = self.estimate_tokens(executive_summary)

        checkpoint = CheckpointState(
            goal_id=goal_id,
            original_instruction=original_instruction,
            executive_summary=executive_summary,
            completed_tasks=[
                {
                    "task_id": t["task_id"],
                    "worker_type": t.get("worker_type"),
                    "summary": t.get("summary", ""),
                }
                for t in completed_tasks
            ],
            pending_tasks=pending_tasks,
            open_issues=open_issues,
            decisions_made=decisions_made,
            context_token_count=total_tokens,
            checkpoint_number=checkpoint_number,
        )

        # Persist to store with configurable TTL (default 24h).
        # Long-running goals can increase ttl_seconds at construction time.
        key = f"heddle:checkpoint:{goal_id}:{checkpoint_number}"
        await self.store.set(key, checkpoint.model_dump_json(), self.ttl_seconds)

        # Maintain a "latest" pointer so load_latest() doesn't need to scan.
        await self.store.set(f"heddle:checkpoint:{goal_id}:latest", key, self.ttl_seconds)

        logger.info(
            "checkpoint.created",
            goal_id=goal_id,
            checkpoint_number=checkpoint_number,
            token_count=total_tokens,
        )
        return checkpoint

    async def load_latest(self, goal_id: str) -> CheckpointState | None:
        """Load the most recent checkpoint for a goal."""
        latest_key = await self.store.get(f"heddle:checkpoint:{goal_id}:latest")
        if not latest_key:
            return None
        data = await self.store.get(latest_key)
        if not data:
            return None
        return CheckpointState.model_validate_json(data)

    def format_for_injection(self, checkpoint: CheckpointState) -> str:
        """Format checkpoint as context to inject into a fresh orchestrator session.

        This is what the orchestrator sees when it "wakes up" after a checkpoint.
        """
        sections = [
            f"=== CHECKPOINT #{checkpoint.checkpoint_number} ===",
            f"Original Goal: {checkpoint.original_instruction}",
            "",
            "--- Executive Summary ---",
            checkpoint.executive_summary,
            "",
            f"--- Decisions Made ({len(checkpoint.decisions_made)}) ---",
        ]
        sections.extend(f"  * {d}" for d in checkpoint.decisions_made)

        if checkpoint.open_issues:
            sections.append(f"\n--- Open Issues ({len(checkpoint.open_issues)}) ---")
            sections.extend(f"  ! {issue}" for issue in checkpoint.open_issues)

        sections.append(f"\n--- Pending Tasks ({len(checkpoint.pending_tasks)}) ---")
        sections.extend(f"  -> {t}" for t in checkpoint.pending_tasks)

        sections.append("\n=== END CHECKPOINT ===")
        return "\n".join(sections)
