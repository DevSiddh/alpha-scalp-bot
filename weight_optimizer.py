"""Alpha-Scalp Bot – LLM Weight Optimizer (Phase 2).

Acts as an agentic feedback loop. Analyzes past trade performance 
and asks an LLM to adjust signal weights to maximize EV.
Includes strict safety bounds to prevent algorithmic runaway.
"""

from __future__ import annotations

import json
import asyncio
import aiohttp
from pathlib import Path
from loguru import logger

import config as cfg
from trade_tracker_v2 import TradeTrackerV2
from signal_scoring import DEFAULT_WEIGHTS


class WeightOptimizer:
    def __init__(self, tracker: TradeTrackerV2, weights_file: str = "weights.json"):
        self.tracker = tracker
        self.weights_file = Path(weights_file)
        self.api_url = cfg.LLM_API_URL
        self.api_key = cfg.LLM_API_KEY
        self.model = cfg.LLM_MODEL

    def _build_prompt(self, performance_data: dict) -> str:
        """Constructs a strict prompt for the LLM."""
        example_weights = json.dumps(
            {k: round(v, 1) for k, v in DEFAULT_WEIGHTS.items()}, indent=12
        )
        return f"""
        You are a quantitative trading risk manager. 
        Your task is to optimize the weighting of {len(DEFAULT_WEIGHTS)} technical trading signals to maximize Expected Value (EV).

        Current Signal Performance (Win Rates and EV):
        {json.dumps(performance_data, indent=2)}

        Rules for adjustment:
        1. Increase weights for signals with positive EV and high win rates.
        2. Decrease weights for signals with negative EV (minimum weight is {cfg.MIN_WEIGHT}).
        3. Maximum allowed weight is {cfg.MAX_WEIGHT}.
        4. Maintain the baseline for signals with insufficient data.

        Output ONLY a valid JSON object matching this exact structure, with no markdown formatting or extra text:
        {example_weights}
        """

    async def fetch_optimized_weights(self) -> dict[str, float] | None:
        """Calls the LLM API to get new weights."""
        stats = self.tracker.get_cumulative_stats()
        signal_perf = stats.get("signal_performance", {})
        
        if not signal_perf:
            logger.warning("Not enough trade data to optimize weights.")
            return None

        prompt = self._build_prompt(signal_perf)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1  # Low temp for deterministic, analytical output
        }

        logger.info("Requesting weight optimization from LLM...")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=30) as response:
                    if response.status != 200:
                        logger.error(f"LLM API Error: {response.status} - {await response.text()}")
                        return None
                    
                    data = await response.json()
                    raw_content = data['choices'][0]['message']['content']
                    
                    # Clean up common LLM markdown formatting (just in case)
                    raw_content = raw_content.replace('`json', '').replace('```', '').strip()
                    return json.loads(raw_content)

        except json.JSONDecodeError:
            logger.error("LLM returned malformed JSON. Aborting optimization.")
            return None
        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            return None

    def _validate_and_sanitize(self, new_weights: dict) -> dict[str, float]:
        """The Safety Rail: Ensures LLM outputs are within logical bounds."""
        sanitized = {}
        for signal_name in DEFAULT_WEIGHTS.keys():
            # Fallback to default if LLM forgot a signal
            val = new_weights.get(signal_name, DEFAULT_WEIGHTS[signal_name])
            
            # Ensure it's a float
            try:
                val = float(val)
            except ValueError:
                val = DEFAULT_WEIGHTS[signal_name]
                
            # Clamp between MIN and MAX config values
            sanitized[signal_name] = max(cfg.MIN_WEIGHT, min(cfg.MAX_WEIGHT, round(val, 2)))
            
        return sanitized

    async def run_optimization_cycle(self) -> bool:
        """Main execution flow to update the weights file."""
        new_weights_raw = await self.fetch_optimized_weights()
        
        if not new_weights_raw:
            return False
            
        safe_weights = self._validate_and_sanitize(new_weights_raw)
        
        # Load existing file to preserve regime formats if they exist
        current_data = {"default": DEFAULT_WEIGHTS}
        if self.weights_file.exists():
            with open(self.weights_file, "r") as f:
                current_data = json.load(f)
                
        # Update default weights
        if "default" in current_data:
            current_data["default"] = safe_weights
        else:
            current_data = safe_weights

        # Save back to file safely
        temp_file = self.weights_file.with_suffix('.tmp')
        with open(temp_file, "w") as f:
            json.dump(current_data, f, indent=2)
        temp_file.replace(self.weights_file)
        
        logger.success("Successfully applied safe, LLM-optimized weights.")
        return True
