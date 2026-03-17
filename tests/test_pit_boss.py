"""Tests for DeepSeekPitBoss (Part A) and BlockConditions (Part C) — Step 10."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from deepseek_pit_boss import (
    DeepSeekPitBoss,
    LossFinding,
    _build_loss_audit_prompt,
    _parse_loss_findings,
    _prev_month_str,
)
from block_conditions import BlockConditions, BlockResult
from hypothesis_tracker import HypothesisTracker, _jaccard_similarity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_tracker(tmp_path):
    return HypothesisTracker(
        hypotheses_path=tmp_path / "hyp.jsonl",
        registry_path=tmp_path / "registry.json",
    )


def make_pit_boss(tmp_path, tracker=None):
    if tracker is None:
        tracker = make_tracker(tmp_path)
    return DeepSeekPitBoss(
        hypothesis_tracker=tracker,
        loss_audit_path=tmp_path / "loss_audit.jsonl",
        shadow_trades_path=tmp_path / "shadow_trades.jsonl",
        trades_path=tmp_path / "trades.jsonl",
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# LossFinding
# ---------------------------------------------------------------------------

def test_loss_finding_to_dict_has_required_keys():
    f = LossFinding(
        pattern_key="counter_trend_entry",
        confidence=0.82,
        occurrences=5,
        rule_description="Block counter-trend entries",
        action_type="BLOCK_CONDITION",
    )
    d = f.to_dict()
    for key in ("finding_id", "pattern_key", "confidence", "occurrences",
                "rule_description", "action_type", "week_ending", "created_at"):
        assert key in d, f"Missing key: {key}"
    assert d["finding_id"].startswith("F")


# ---------------------------------------------------------------------------
# should_run_today
# ---------------------------------------------------------------------------

def test_should_run_today_returns_bool(tmp_path):
    pit = make_pit_boss(tmp_path)
    result = pit.should_run_today()
    assert isinstance(result, bool)


def test_should_run_today_sunday(tmp_path, monkeypatch):
    pit = make_pit_boss(tmp_path)
    # Patch datetime.now to return a Sunday (weekday=6)
    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)  # Sunday

    monkeypatch.setattr("deepseek_pit_boss.datetime", FakeDatetime)
    assert pit.should_run_today() is True


def test_should_run_today_not_sunday(tmp_path, monkeypatch):
    pit = make_pit_boss(tmp_path)

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)  # Monday

    monkeypatch.setattr("deepseek_pit_boss.datetime", FakeDatetime)
    assert pit.should_run_today() is False


# ---------------------------------------------------------------------------
# _load_recent_losses
# ---------------------------------------------------------------------------

def test_load_recent_losses_no_file(tmp_path):
    pit = make_pit_boss(tmp_path)
    assert pit._load_recent_losses() == []


def test_load_recent_losses_returns_only_losses_within_lookback(tmp_path):
    trades_path = tmp_path / "trades.jsonl"
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=10)).isoformat()

    _write_jsonl(trades_path, [
        {"symbol": "BTC/USDT", "is_win": False, "exit_time": recent, "pnl_usdt": -10},
        {"symbol": "BTC/USDT", "is_win": True,  "exit_time": recent, "pnl_usdt": 15},
        {"symbol": "BTC/USDT", "is_win": False, "exit_time": old,    "pnl_usdt": -8},
    ])

    pit = make_pit_boss(tmp_path)
    losses = pit._load_recent_losses()
    # Only 1 recent loss (old one excluded, win excluded)
    assert len(losses) == 1
    assert losses[0]["pnl_usdt"] == -10


# ---------------------------------------------------------------------------
# _parse_loss_findings
# ---------------------------------------------------------------------------

def test_parse_loss_findings_valid_json():
    content = json.dumps([
        {
            "pattern_key": "bearish_fvg_overhead",
            "confidence": 0.82,
            "occurrences": 5,
            "rule_description": "Block when bearish FVG is overhead",
            "action_type": "BLOCK_CONDITION",
        }
    ])
    findings = _parse_loss_findings(content)
    assert len(findings) == 1
    assert findings[0].pattern_key == "bearish_fvg_overhead"
    assert findings[0].confidence == 0.82


def test_parse_loss_findings_strips_markdown_fences():
    content = "```json\n[{\"pattern_key\": \"test\", \"confidence\": 0.5, \"occurrences\": 3, \"rule_description\": \"desc\", \"action_type\": \"BLOCK_CONDITION\"}]\n```"
    findings = _parse_loss_findings(content)
    assert len(findings) == 1


def test_parse_loss_findings_empty_array():
    findings = _parse_loss_findings("[]")
    assert findings == []


def test_parse_loss_findings_invalid_json_returns_empty():
    findings = _parse_loss_findings("not valid json {{{")
    assert findings == []


# ---------------------------------------------------------------------------
# _build_loss_audit_prompt
# ---------------------------------------------------------------------------

def test_build_loss_audit_prompt_caps_at_20():
    losses = [
        {"symbol": "BTC/USDT", "side": "BUY", "entry_price": 85000,
         "exit_price": 84500, "regime": "TRENDING_UP", "strategy": "Breakout",
         "pnl_usdt": -10.5, "reason": "sl_hit"}
        for _ in range(30)
    ]
    prompt = _build_loss_audit_prompt(losses)
    # Header mentions "30" losses; lines capped at 20 plus 1 in header = 21
    assert "30" in prompt
    # 20 trade lines + 1 occurrence in the header sentence
    assert prompt.count("BTC/USDT") == 21


# ---------------------------------------------------------------------------
# Phase 2 — hypothesis generation
# ---------------------------------------------------------------------------

def test_phase2_creates_hypothesis_for_pattern_seen_3_times(tmp_path):
    tracker = make_tracker(tmp_path)
    pit = make_pit_boss(tmp_path, tracker)

    # Simulate audit log with 3 entries of same pattern
    audit_path = tmp_path / "loss_audit.jsonl"
    now_iso = datetime.now(timezone.utc).isoformat()
    findings = [
        {
            "finding_id": f"F00000{i}",
            "pattern_key": "counter_trend_entry",
            "confidence": 0.80,
            "occurrences": 1,
            "rule_description": "Block counter-trend entries",
            "action_type": "BLOCK_CONDITION",
            "week_ending": "2026-03-17",
            "created_at": now_iso,
        }
        for i in range(3)
    ]
    _write_jsonl(audit_path, findings)

    new_hyps = pit._phase2_generate_hypotheses([])
    assert len(new_hyps) == 1
    assert new_hyps[0].pattern_key == "counter_trend_entry"


def test_phase2_skips_pattern_already_in_tracker(tmp_path):
    tracker = make_tracker(tmp_path)
    # Pre-add hypothesis for the pattern
    tracker.add_hypothesis("counter_trend_entry", "already testing")

    pit = make_pit_boss(tmp_path, tracker)

    now_iso = datetime.now(timezone.utc).isoformat()
    findings = [
        {
            "finding_id": f"F00000{i}",
            "pattern_key": "counter_trend_entry",
            "confidence": 0.80,
            "occurrences": 1,
            "rule_description": "Block counter-trend entries",
            "action_type": "BLOCK_CONDITION",
            "week_ending": "2026-03-17",
            "created_at": now_iso,
        }
        for i in range(3)
    ]
    audit_path = tmp_path / "loss_audit.jsonl"
    _write_jsonl(audit_path, findings)

    new_hyps = pit._phase2_generate_hypotheses([])
    assert len(new_hyps) == 0


def test_phase2_requires_3_occurrences_minimum(tmp_path):
    tracker = make_tracker(tmp_path)
    pit = make_pit_boss(tmp_path, tracker)

    now_iso = datetime.now(timezone.utc).isoformat()
    # Only 2 occurrences — should NOT create hypothesis
    findings = [
        {
            "finding_id": f"F00000{i}",
            "pattern_key": "rare_pattern",
            "confidence": 0.80,
            "occurrences": 1,
            "rule_description": "Rare pattern",
            "action_type": "BLOCK_CONDITION",
            "week_ending": "2026-03-17",
            "created_at": now_iso,
        }
        for i in range(2)
    ]
    audit_path = tmp_path / "loss_audit.jsonl"
    _write_jsonl(audit_path, findings)

    new_hyps = pit._phase2_generate_hypotheses([])
    assert len(new_hyps) == 0


# ---------------------------------------------------------------------------
# Archive rotation
# ---------------------------------------------------------------------------

def test_rotate_shadow_archive_no_file_returns_false(tmp_path):
    pit = make_pit_boss(tmp_path)
    assert pit._rotate_shadow_archive() is False


def test_rotate_shadow_archive_on_month_rollover(tmp_path):
    pit = make_pit_boss(tmp_path)
    shadow_path = tmp_path / "shadow_trades.jsonl"

    # Write a record from previous month
    prev_month = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=1))
    prev_ts = prev_month.replace(day=1).isoformat()
    shadow_path.write_text(json.dumps({"timestamp": prev_ts, "strategy": "Breakout"}) + "\n")

    rotated = pit._rotate_shadow_archive()
    assert rotated is True
    # Original file should now be empty (fresh)
    assert shadow_path.exists()
    assert shadow_path.stat().st_size == 0


def test_rotate_shadow_archive_small_current_month_no_rotation(tmp_path):
    pit = make_pit_boss(tmp_path)
    shadow_path = tmp_path / "shadow_trades.jsonl"

    # Write a record from current month — should NOT rotate
    current_ts = datetime.now(timezone.utc).isoformat()
    shadow_path.write_text(json.dumps({"timestamp": current_ts, "strategy": "VWAP"}) + "\n")

    rotated = pit._rotate_shadow_archive()
    assert rotated is False


# ---------------------------------------------------------------------------
# load_shadow_trades_for_pit_boss
# ---------------------------------------------------------------------------

def test_load_shadow_trades_loads_current_file(tmp_path):
    pit = make_pit_boss(tmp_path)
    shadow_path = tmp_path / "shadow_trades.jsonl"
    _write_jsonl(shadow_path, [
        {"strategy": "Breakout", "pnl": 10.5},
        {"strategy": "VWAP", "pnl": -5.0},
    ])

    records = pit.load_shadow_trades_for_pit_boss()
    assert len(records) == 2


def test_load_shadow_trades_no_files_returns_empty(tmp_path):
    pit = make_pit_boss(tmp_path)
    records = pit.load_shadow_trades_for_pit_boss()
    assert records == []


# ---------------------------------------------------------------------------
# _prev_month_str helper
# ---------------------------------------------------------------------------

def test_prev_month_str_december_to_november():
    dt = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert _prev_month_str(dt) == "2025_12"


def test_prev_month_str_march_to_february():
    dt = datetime(2026, 3, 17, tzinfo=timezone.utc)
    assert _prev_month_str(dt) == "2026_02"


# ===========================================================================
# BlockConditions tests
# ===========================================================================

def _make_registry(tmp_path, conditions: list[dict]) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(conditions), encoding="utf-8")
    return path


def test_block_conditions_hard_block(tmp_path):
    path = _make_registry(tmp_path, [{
        "hypothesis_id": "H123456",
        "pattern_key": "bearish_fvg_overhead",
        "confidence_tier": "HARD_BLOCK",
        "active": True,
    }])
    bc = BlockConditions(registry_path=path)
    result = bc.check({"bearish_fvg_overhead"}, regime="TRENDING_UP")

    assert result.blocked is True
    assert result.soft_block is False
    assert result.hypothesis_id == "H123456"


def test_block_conditions_soft_block(tmp_path):
    path = _make_registry(tmp_path, [{
        "hypothesis_id": "H654321",
        "pattern_key": "marginal_setup",
        "confidence_tier": "SOFT_BLOCK",
        "active": True,
    }])
    bc = BlockConditions(registry_path=path)
    result = bc.check({"marginal_setup"}, regime="RANGING")

    assert result.blocked is False
    assert result.soft_block is True
    assert result.hypothesis_id == "H654321"


def test_block_conditions_no_match_returns_allowed(tmp_path):
    path = _make_registry(tmp_path, [{
        "hypothesis_id": "H111111",
        "pattern_key": "pattern_a",
        "confidence_tier": "HARD_BLOCK",
        "active": True,
    }])
    bc = BlockConditions(registry_path=path)
    result = bc.check({"pattern_b", "pattern_c"}, regime="TRENDING_UP")

    assert result.blocked is False
    assert result.soft_block is False


def test_block_conditions_inactive_condition_ignored(tmp_path):
    path = _make_registry(tmp_path, [{
        "hypothesis_id": "H222222",
        "pattern_key": "inactive_pattern",
        "confidence_tier": "HARD_BLOCK",
        "active": False,  # inactive
    }])
    bc = BlockConditions(registry_path=path)
    result = bc.check({"inactive_pattern"}, regime="TRENDING_UP")

    assert result.blocked is False


def test_block_conditions_max_active_cap(tmp_path):
    """Only first MAX_ACTIVE_CONDITIONS=5 conditions are evaluated."""
    conditions = [
        {
            "hypothesis_id": f"H{i:06d}",
            "pattern_key": f"pattern_{i}",
            "confidence_tier": "HARD_BLOCK",
            "active": True,
        }
        for i in range(7)   # 7 conditions, but cap is 5
    ]
    path = _make_registry(tmp_path, conditions)
    bc = BlockConditions(registry_path=path)

    # pattern_5 and pattern_6 are beyond the cap of 5
    result_5 = bc.check({"pattern_5"}, regime="TRENDING_UP")
    result_6 = bc.check({"pattern_6"}, regime="TRENDING_UP")

    # They should NOT block because they are beyond the MAX_ACTIVE_CONDITIONS cap
    assert result_5.blocked is False
    assert result_6.blocked is False


def test_block_conditions_no_registry_returns_allowed(tmp_path):
    """Missing registry → no blocks."""
    bc = BlockConditions(registry_path=tmp_path / "nonexistent.json")
    result = bc.check({"any_pattern"}, regime="TRENDING_UP")
    assert result.blocked is False
    assert result.soft_block is False


def test_block_conditions_reload_picks_up_changes(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("[]")  # empty initially

    bc = BlockConditions(registry_path=path)
    assert bc.active_count == 0

    # Update registry
    path.write_text(json.dumps([{
        "hypothesis_id": "H999999",
        "pattern_key": "new_pattern",
        "confidence_tier": "HARD_BLOCK",
        "active": True,
    }]))
    bc.reload()
    assert bc.active_count == 1


# ---------------------------------------------------------------------------
# FIX-7 — semantic overlap check in add_hypothesis
# ---------------------------------------------------------------------------

def test_add_hypothesis_rejects_high_overlap(tmp_path):
    """Near-identical rule descriptions → second add returns None (FIX-7)."""
    tracker = make_tracker(tmp_path)
    desc = "Block entries when a bearish FVG is directly overhead"
    tracker.add_hypothesis("pattern_a", desc)

    # Slightly reworded but same meaning — Jaccard will be > 0.70
    similar = "Block entries when bearish FVG is directly overhead on chart"
    result = tracker.add_hypothesis("pattern_b", similar)
    assert result is None


def test_add_hypothesis_accepts_low_overlap(tmp_path):
    """Distinct rule descriptions → both hypotheses created successfully."""
    tracker = make_tracker(tmp_path)
    tracker.add_hypothesis("pattern_a", "Block counter-trend long entries in downtrend")
    h = tracker.add_hypothesis("pattern_b", "Block entries when funding rate is extremely positive")
    assert h is not None
    assert h.pattern_key == "pattern_b"


def test_jaccard_similarity_values():
    """Unit-test the similarity helper directly."""
    assert _jaccard_similarity("block counter trend entry", "block counter trend entry") == 1.0
    assert _jaccard_similarity("block counter trend entry", "something completely different here") < 0.30
    # Partial overlap
    sim = _jaccard_similarity(
        "block entries when bearish fvg overhead",
        "block entries when bearish fvg is overhead",
    )
    assert sim > 0.70


def test_block_conditions_active_count(tmp_path):
    path = _make_registry(tmp_path, [
        {"hypothesis_id": "H1", "pattern_key": "p1", "confidence_tier": "HARD_BLOCK", "active": True},
        {"hypothesis_id": "H2", "pattern_key": "p2", "confidence_tier": "SOFT_BLOCK", "active": True},
        {"hypothesis_id": "H3", "pattern_key": "p3", "confidence_tier": "HARD_BLOCK", "active": False},
    ])
    bc = BlockConditions(registry_path=path)
    assert bc.active_count == 2
