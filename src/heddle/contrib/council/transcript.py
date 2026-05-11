"""Transcript management for council discussions.

Stores round-by-round discussion history and provides filtered views
for agents with selective visibility.  Token-budget-aware truncation
keeps injected context within model limits.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heddle.contrib.council.schemas import AgentConfig

from heddle.contrib.council.schemas import RoundEntry, TranscriptEntry

# Default per-agent character budget — matches ResultSynthesizer._MAX_OUTPUT_CHARS.
_DEFAULT_MAX_CHARS = 6_000


class TranscriptStore:
    """Manages the growing discussion transcript across rounds.

    Each round contains zero or more :class:`TranscriptEntry` objects,
    one per agent contribution.  The store supports filtered views based
    on agent visibility rules and token-budget-aware truncation.
    """

    def __init__(self, max_chars_per_agent: int = _DEFAULT_MAX_CHARS) -> None:
        self._rounds: list[RoundEntry] = []
        self._max_chars = max_chars_per_agent
        # ``inject_interjection`` is documented as thread-safe so the
        # spectator UI can poke entries into a running discussion
        # from a non-event-loop thread.  ``list.append`` is GIL-atomic
        # but the start_round-if-empty + append sequence is two
        # operations, so a real lock is needed to honour the
        # documented contract.
        self._inject_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def start_round(self, round_num: int) -> None:
        """Begin a new discussion round."""
        self._rounds.append(RoundEntry(round_num=round_num))

    def add_entry(self, entry: TranscriptEntry) -> None:
        """Append an agent's contribution to the current round.

        Raises :class:`RuntimeError` if no round has been started.
        """
        if not self._rounds:
            msg = "No round started — call start_round() first"
            raise RuntimeError(msg)
        self._rounds[-1].entries.append(entry)

    def inject_interjection(
        self,
        agent_name: str,
        content: str,
        role: str = "audience",
    ) -> None:
        """Inject a spectator interjection into the current round.

        Thread-safe for use from external callers while the council is
        running — the start_round-if-empty + append pair is wrapped
        in ``self._inject_lock``.  If no round has started yet,
        starts round 0.
        """
        with self._inject_lock:
            if not self._rounds:
                self.start_round(0)
            entry = TranscriptEntry(
                round_num=self._rounds[-1].round_num,
                agent_name=agent_name,
                role=role,
                content=content,
                entry_type="interjection",
            )
            self._rounds[-1].entries.append(entry)

    def set_convergence_score(self, round_num: int, score: float) -> None:
        """Attach a convergence score to a completed round."""
        for r in self._rounds:
            if r.round_num == round_num:
                r.convergence_score = score
                return

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------

    @property
    def rounds(self) -> list[RoundEntry]:
        """All rounds in the transcript."""
        return list(self._rounds)

    @property
    def total_entries(self) -> int:
        """Total number of entries across all rounds."""
        return sum(len(r.entries) for r in self._rounds)

    def get_full_transcript(self) -> list[RoundEntry]:
        """Return the complete transcript (for facilitator use)."""
        return list(self._rounds)

    def get_full_transcript_entries(self) -> list[TranscriptEntry]:
        """Return a flat list of all entries across all rounds."""
        return [e for r in self._rounds for e in r.entries]

    def get_visible_transcript(
        self,
        agent: AgentConfig,
        up_to_round: int | None = None,
    ) -> list[TranscriptEntry]:
        """Return entries visible to *agent* based on ``sees_transcript_from``.

        Args:
            agent: The agent requesting visibility.
            up_to_round: If set, only include entries from rounds <= this value.
        """
        sees = agent.sees_transcript_from
        see_all = "all" in sees

        entries: list[TranscriptEntry] = []
        for r in self._rounds:
            if up_to_round is not None and r.round_num > up_to_round:
                break
            entries.extend(e for e in r.entries if see_all or e.agent_name in sees)
        return entries

    def get_visible_turns(
        self,
        agent: AgentConfig,
        up_to_round: int | None = None,
    ) -> list[TranscriptEntry]:
        """Like :meth:`get_visible_transcript` but only panelist turns."""
        return [
            e for e in self.get_visible_transcript(agent, up_to_round) if e.entry_type == "turn"
        ]

    def get_interjections(
        self,
        since_round: int | None = None,
    ) -> list[TranscriptEntry]:
        """Return audience interjections, optionally from a specific round onward.

        Interjections are always visible to all panelists — they are
        public audience contributions.
        """
        entries: list[TranscriptEntry] = []
        for r in self._rounds:
            if since_round is not None and r.round_num < since_round:
                continue
            entries.extend(e for e in r.entries if e.entry_type == "interjection")
        return entries

    def get_latest_positions(self) -> dict[str, str]:
        """Return each agent's most recent contribution content.

        Used by :class:`ConvergenceDetector` to compare positions.
        """
        positions: dict[str, str] = {}
        for r in self._rounds:
            for e in r.entries:
                positions[e.agent_name] = e.content
        return positions

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_for_payload(
        entries: list[TranscriptEntry],
        max_chars: int | None = None,
    ) -> str:
        """Serialize entries as structured text suitable for LLM payloads.

        If *max_chars* is set and the total exceeds it, the **oldest**
        entries are dropped first (preserving the most recent context).
        """
        if not entries:
            return ""

        blocks: list[str] = []
        for e in entries:
            header = f"[Round {e.round_num}] {e.agent_name}"
            if e.role:
                header += f" ({e.role})"
            blocks.append(f"{header}:\n{e.content}")

        text = "\n\n".join(blocks)

        if max_chars is not None and len(text) > max_chars:
            # Drop oldest blocks until within budget.
            while len(blocks) > 1:
                blocks.pop(0)
                text = "\n\n".join(blocks)
                if len(text) <= max_chars:
                    break
            # If a single block still exceeds, truncate it.
            if len(text) > max_chars:
                text = text[:max_chars] + "... [truncated]"

        return text
