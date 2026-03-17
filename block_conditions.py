"""Alpha-Scalp Bot – BlockConditions (GP Step 10 / Part C).

Reads block_conditions_registry.json at runtime and exposes a single
check function. Designed to be called in can_open_trade() AFTER the
6 existing risk gates.

IMPORTANT: This gate is NOT yet wired into can_open_trade().
It will be enabled in Step 10b after 200 live trades prove the
hypothesis learning system is adding value (not over-filtering).

Confidence tiers (from hypothesis edge_ratio at approval):
  SOFT_BLOCK  edge 2.5–3.5 → reduce position size 50%, do not fully block
  HARD_BLOCK  edge  > 3.5  → return blocked=True, entry denied
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


_DEFAULT_REGISTRY = Path("block_conditions_registry.json")

# Cap: never more than 5 active block conditions (prevents over-filtering)
MAX_ACTIVE_CONDITIONS: int = 5


@dataclass
class BlockResult:
    """Result of a block condition check."""
    blocked: bool                  # True = HARD_BLOCK, entry denied
    soft_block: bool = False       # True = SOFT_BLOCK, reduce size 50%
    hypothesis_id: str | None = None
    reason: str | None = None


class BlockConditions:
    """Reads approved hypotheses from registry and checks trade entries.

    Usage
    -----
    bc = BlockConditions()
    result = bc.check(pattern_keys={"bearish_fvg_overhead"}, regime="TRENDING_UP")
    if result.blocked:
        return  # skip trade
    if result.soft_block:
        size *= 0.50  # reduce size
    """

    def __init__(self, registry_path: Path | str | None = None) -> None:
        self._path = Path(registry_path or _DEFAULT_REGISTRY)
        self._conditions: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, pattern_keys: set[str], regime: str) -> BlockResult:
        """Check active block conditions against the current trade setup.

        Parameters
        ----------
        pattern_keys : set[str]
            Patterns detected in the current feature set by PitBoss/HypothesisTracker.
        regime : str
            Current market regime string (e.g. "TRENDING_UP").

        Returns
        -------
        BlockResult
            blocked=False, soft_block=False → trade allowed at full size.
            blocked=False, soft_block=True  → trade allowed at 50% size.
            blocked=True                    → trade denied.
        """
        active = [c for c in self._conditions if c.get("active", False)][:MAX_ACTIVE_CONDITIONS]

        for cond in active:
            pk = cond.get("pattern_key", "")
            if pk not in pattern_keys:
                continue

            tier = cond.get("confidence_tier", "HARD_BLOCK")
            h_id = cond.get("hypothesis_id", "?")

            if tier == "SOFT_BLOCK":
                logger.info("BlockConditions SOFT_BLOCK | {} | pattern={} regime={}", h_id, pk, regime)
                return BlockResult(blocked=False, soft_block=True, hypothesis_id=h_id, reason=f"soft_block:{pk}")

            logger.info("BlockConditions HARD_BLOCK | {} | pattern={} regime={}", h_id, pk, regime)
            return BlockResult(blocked=True, soft_block=False, hypothesis_id=h_id, reason=f"hard_block:{pk}")

        return BlockResult(blocked=False)

    def reload(self) -> None:
        """Reload registry from disk — call after a hypothesis is approved."""
        self._load()

    @property
    def active_count(self) -> int:
        return sum(1 for c in self._conditions if c.get("active", False))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            self._conditions = []
            logger.debug("BlockConditions: registry not found — zero active conditions")
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._conditions = data if isinstance(data, list) else []
            logger.info("BlockConditions loaded: {}/{} active",
                        self.active_count, len(self._conditions))
        except Exception as exc:
            logger.error("BlockConditions load failed: {}", exc)
            self._conditions = []
