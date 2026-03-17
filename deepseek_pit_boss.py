"""Alpha-Scalp Bot – DeepSeekPitBoss (GP Step 10 / Part A).

Weekly audit engine that:
  Phase 1 — Reads recent losing trades, calls DeepSeek to identify loss
             patterns, appends structured LossFindings to loss_audit_log.jsonl
  Phase 2 — Reads ALL loss_audit_log.jsonl entries, finds patterns seen
             in 3+ losses this week, generates ShadowHypotheses for
             HypothesisTracker to begin shadow testing
  Archive  — Rotates shadow_trades.jsonl on month boundary or >50MB

Run schedule: every Sunday (or manually). Does NOT self-schedule.
Caller decides when to invoke run_audit().

Constraints (CRITICAL):
  - NEVER writes to any live trading file
  - NEVER modifies weights.json, config.py, or signal thresholds
  - Only writes to: loss_audit_log.jsonl, shadow_hypotheses.jsonl (via tracker)
  - All hypothesis activation requires HypothesisTracker approval pipeline
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

import config as cfg
from hypothesis_tracker import HypothesisTracker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOSS_AUDIT_FILE       = Path("loss_audit_log.jsonl")
_SHADOW_TRADES_FILE    = Path("shadow_trades.jsonl")
_TRADES_FILE           = Path("trades.jsonl")

ARCHIVE_MAX_BYTES: int = 50 * 1024 * 1024    # 50 MB
ARCHIVE_READ_MONTHS: int = 2                  # PitBoss reads current + last N months
MIN_OCCURRENCES_FOR_HYPOTHESIS: int = 3       # pattern must appear in 3+ losses
AUDIT_LOOKBACK_DAYS: int = 7                  # read losses from last 7 days


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class LossFinding:
    """Structured output from DeepSeek loss analysis."""
    __slots__ = (
        "finding_id", "pattern_key", "confidence",
        "occurrences", "rule_description", "action_type",
        "week_ending", "created_at",
    )

    def __init__(
        self,
        pattern_key: str,
        confidence: float,
        occurrences: int,
        rule_description: str,
        action_type: str = "BLOCK_CONDITION",
    ) -> None:
        self.finding_id      = f"F{str(uuid.uuid4())[:6].upper()}"
        self.pattern_key     = pattern_key
        self.confidence      = confidence
        self.occurrences     = occurrences
        self.rule_description = rule_description
        self.action_type     = action_type
        self.week_ending     = _today_iso()
        self.created_at      = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


# ---------------------------------------------------------------------------
# DeepSeekPitBoss
# ---------------------------------------------------------------------------

class DeepSeekPitBoss:
    """Weekly audit engine — audit-only, no live trading writes.

    Parameters
    ----------
    hypothesis_tracker : HypothesisTracker
        Shared tracker instance for hypothesis lifecycle management.
    loss_audit_path : Path, optional
    shadow_trades_path : Path, optional
    trades_path : Path, optional
    """

    def __init__(
        self,
        hypothesis_tracker: HypothesisTracker,
        loss_audit_path:    Path | str | None = None,
        shadow_trades_path: Path | str | None = None,
        trades_path:        Path | str | None = None,
    ) -> None:
        self._tracker       = hypothesis_tracker
        self._audit_path    = Path(loss_audit_path    or _LOSS_AUDIT_FILE)
        self._shadow_path   = Path(shadow_trades_path or _SHADOW_TRADES_FILE)
        self._trades_path   = Path(trades_path        or _TRADES_FILE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_audit(self) -> dict[str, Any]:
        """Full Sunday audit — Phase 1 + Phase 2 + archive rotation.

        Returns summary dict for logging/Telegram.
        """
        logger.info("DeepSeekPitBoss: starting weekly audit")

        # Archive rotation first (housekeeping before heavy reads)
        archived = self._rotate_shadow_archive()

        # Phase 1 — analyse losses, generate LossFindings
        findings = await self._phase1_loss_audit()

        # Phase 2 — convert recurring findings into hypotheses
        new_hypotheses = self._phase2_generate_hypotheses(findings)

        summary = {
            "audit_date":      _today_iso(),
            "findings":        len(findings),
            "new_hypotheses":  len(new_hypotheses),
            "archive_rotated": archived,
            "active_hypotheses": len(self._tracker.get_active_hypotheses()),
            "approved_conditions": len(self._tracker.get_approved()),
        }
        logger.info("DeepSeekPitBoss audit complete: {}", summary)
        return summary

    def should_run_today(self) -> bool:
        """True if today is Sunday (scheduled audit day)."""
        return datetime.now(timezone.utc).weekday() == 6  # 6 = Sunday

    # ------------------------------------------------------------------
    # Phase 1 — Loss Audit
    # ------------------------------------------------------------------

    async def _phase1_loss_audit(self) -> list[LossFinding]:
        """Read recent losses, call DeepSeek, return LossFindings."""
        losses = self._load_recent_losses()
        if not losses:
            logger.info("DeepSeekPitBoss Phase 1: no recent losses to audit")
            return []

        logger.info("DeepSeekPitBoss Phase 1: auditing {} recent losses", len(losses))

        try:
            findings = await self._call_deepseek_loss_audit(losses)
        except Exception as exc:
            logger.error("DeepSeekPitBoss Phase 1: LLM call failed — {}", exc)
            return []

        # Persist findings
        for f in findings:
            self._append_loss_finding(f)

        logger.info("DeepSeekPitBoss Phase 1: {} findings generated", len(findings))
        return findings

    async def _call_deepseek_loss_audit(self, losses: list[dict]) -> list[LossFinding]:
        """Call DeepSeek API with recent losses. Returns structured findings."""
        prompt = _build_loss_audit_prompt(losses)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                cfg.LLM_API_URL,
                headers={"Authorization": f"Bearer {cfg.LLM_API_KEY}"},
                json={
                    "model": cfg.LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 800,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        return _parse_loss_findings(content)

    # ------------------------------------------------------------------
    # Phase 2 — Hypothesis Generation
    # ------------------------------------------------------------------

    def _phase2_generate_hypotheses(self, new_findings: list[LossFinding]) -> list[Any]:
        """Read all loss findings, create hypotheses for patterns with 3+ hits."""
        all_findings = self._load_all_findings()
        all_findings.extend(f.to_dict() for f in new_findings)

        # Count weekly occurrences per pattern
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        pattern_counts: dict[str, list[dict]] = {}

        for f in all_findings:
            if f.get("created_at", "") >= week_start:
                pk = f.get("pattern_key", "")
                pattern_counts.setdefault(pk, []).append(f)

        new_hypotheses = []
        existing_patterns = {
            h.pattern_key for h in self._tracker.get_active_hypotheses()
        }

        for pattern_key, occurrences in pattern_counts.items():
            if len(occurrences) < MIN_OCCURRENCES_FOR_HYPOTHESIS:
                continue
            if pattern_key in existing_patterns:
                continue   # already being tested

            # Use the most recent finding's description
            latest = max(occurrences, key=lambda x: x.get("created_at", ""))
            h = self._tracker.add_hypothesis(
                pattern_key=pattern_key,
                rule_description=latest.get("rule_description", pattern_key),
                finding_id=latest.get("finding_id", ""),
            )
            new_hypotheses.append(h)
            logger.info(
                "DeepSeekPitBoss Phase 2: new hypothesis {} for pattern '{}' ({} occurrences)",
                h.hypothesis_id, pattern_key, len(occurrences),
            )

        return new_hypotheses

    # ------------------------------------------------------------------
    # Archive Rotation
    # ------------------------------------------------------------------

    def _rotate_shadow_archive(self) -> bool:
        """Rotate shadow_trades.jsonl if month changed or file > 50MB.

        Returns True if rotation occurred.
        """
        if not self._shadow_path.exists():
            return False

        size = self._shadow_path.stat().st_size
        now = datetime.now(timezone.utc)

        # Check if month boundary has passed since last line's timestamp
        month_rollover = self._detect_month_rollover()
        size_limit     = size > ARCHIVE_MAX_BYTES

        if not (month_rollover or size_limit):
            return False

        # Determine archive month from last entry (or previous month)
        archive_ym = _prev_month_str(now) if month_rollover else now.strftime("%Y_%m")
        archive_path = self._shadow_path.parent / f"shadow_trades_{archive_ym}.jsonl"

        try:
            self._shadow_path.rename(archive_path)
            self._shadow_path.touch()   # create fresh empty file
            reason = "month_boundary" if month_rollover else f"size>{size//1024//1024}MB"
            logger.info(
                "DeepSeekPitBoss: rotated shadow archive → {} (reason={})",
                archive_path.name, reason,
            )
            return True
        except Exception as exc:
            logger.error("DeepSeekPitBoss: archive rotation failed — {}", exc)
            return False

    def _detect_month_rollover(self) -> bool:
        """Check if the shadow file contains entries from a previous month."""
        if not self._shadow_path.exists():
            return False
        try:
            with open(self._shadow_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            if not first_line:
                return False
            entry = json.loads(first_line)
            entry_ts = entry.get("timestamp", entry.get("created_at", ""))
            if not entry_ts:
                return False
            entry_month = str(entry_ts)[:7]           # "2026-02"
            current_month = datetime.now(timezone.utc).strftime("%Y-%m")
            return entry_month < current_month
        except Exception:
            return False

    def load_shadow_trades_for_pit_boss(self) -> list[dict[str, Any]]:
        """Load shadow trades from current + last N archive months for audit."""
        records: list[dict] = []
        files = [self._shadow_path]

        # Add last N monthly archives
        now = datetime.now(timezone.utc)
        for i in range(1, ARCHIVE_READ_MONTHS + 1):
            month = (now.replace(day=1) - timedelta(days=i * 28))
            ym = month.strftime("%Y_%m")
            archive = self._shadow_path.parent / f"shadow_trades_{ym}.jsonl"
            if archive.exists():
                files.append(archive)

        for fpath in files:
            if not fpath.exists():
                continue
            try:
                for line in fpath.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            except Exception as exc:
                logger.warning("DeepSeekPitBoss: could not read {} — {}", fpath.name, exc)

        logger.debug("DeepSeekPitBoss: loaded {} shadow trade records", len(records))
        return records

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_recent_losses(self) -> list[dict[str, Any]]:
        """Load live losing trades from the last AUDIT_LOOKBACK_DAYS days."""
        if not self._trades_path.exists():
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIT_LOOKBACK_DAYS)).isoformat()
        losses = []
        try:
            for line in self._trades_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                if not t.get("is_win", True) and t.get("exit_time", "") >= cutoff:
                    losses.append(t)
        except Exception as exc:
            logger.error("DeepSeekPitBoss: failed to load trades — {}", exc)
        return losses

    def _load_all_findings(self) -> list[dict[str, Any]]:
        """Load all historical loss findings from loss_audit_log.jsonl."""
        if not self._audit_path.exists():
            return []
        findings = []
        try:
            for line in self._audit_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    findings.append(json.loads(line))
        except Exception as exc:
            logger.error("DeepSeekPitBoss: failed to load audit log — {}", exc)
        return findings

    def _append_loss_finding(self, finding: LossFinding) -> None:
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(finding.to_dict()) + "\n")
        except Exception as exc:
            logger.error("DeepSeekPitBoss: failed to append finding — {}", exc)


# ---------------------------------------------------------------------------
# LLM prompt helpers
# ---------------------------------------------------------------------------

def _build_loss_audit_prompt(losses: list[dict]) -> str:
    """Build a structured prompt for DeepSeek loss pattern analysis."""
    loss_summaries = []
    for t in losses[:20]:   # cap at 20 to control token usage
        loss_summaries.append(
            f"- {t.get('symbol','?')} {t.get('side','?')} | "
            f"entry={t.get('entry_price','?')} exit={t.get('exit_price','?')} | "
            f"regime={t.get('regime','?')} strategy={t.get('strategy','?')} | "
            f"pnl={t.get('pnl_usdt','?')} reason={t.get('reason','?')}"
        )

    return f"""You are a trading loss pattern analyst for a BTC/USDT futures scalping bot.

Analyse these {len(losses)} recent losing trades and identify recurring patterns that could be blocked:

{chr(10).join(loss_summaries)}

Respond with a JSON array of findings. Each finding must have:
- pattern_key: snake_case identifier (e.g. "bearish_fvg_overhead")
- confidence: float 0.0-1.0
- occurrences: int (how many losses match this pattern)
- rule_description: one sentence describing the block condition
- action_type: always "BLOCK_CONDITION"

Respond ONLY with valid JSON array. Example:
[{{"pattern_key": "counter_trend_entry", "confidence": 0.82, "occurrences": 5, "rule_description": "Block entries that go counter to the 4h trend direction", "action_type": "BLOCK_CONDITION"}}]

If no clear patterns exist, respond with empty array: []"""


def _parse_loss_findings(content: str) -> list[LossFinding]:
    """Parse DeepSeek JSON response into LossFinding objects."""
    try:
        # Strip markdown code blocks if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        items = json.loads(content)
        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            findings.append(LossFinding(
                pattern_key=item.get("pattern_key", "unknown"),
                confidence=float(item.get("confidence", 0.5)),
                occurrences=int(item.get("occurrences", 1)),
                rule_description=item.get("rule_description", ""),
                action_type=item.get("action_type", "BLOCK_CONDITION"),
            ))
        return findings
    except Exception as exc:
        logger.error("DeepSeekPitBoss: failed to parse LLM response — {}", exc)
        return []


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _prev_month_str(dt: datetime) -> str:
    first = dt.replace(day=1)
    prev  = first - timedelta(days=1)
    return prev.strftime("%Y_%m")
