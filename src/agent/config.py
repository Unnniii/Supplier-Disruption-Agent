"""Settings in one place. Override with environment variables or .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

MODEL = os.getenv("SDA_MODEL", "claude-haiku-5-5")
MAX_TOKENS = int(os.getenv("SDA_MAX_TOKENS", "1500"))
RETRIEVER = os.getenv("SDA_RETRIEVER", "auto")  # auto | chroma | keyword
MAX_AGENT_TURNS = 8      # tool-use loop guard per agent
MAX_REVISIONS = 1        # verifier can send the advisor back once

CONTRACTS_DIR = ROOT / "data" / "contracts"
CHROMA_DIR = ROOT / "data" / "chroma"
LOG_PATH = ROOT / "logs" / "runs.jsonl"

# USD per million tokens (input, output).
# Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-09)
PRICES_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),   # prompts <=100K tokens
    "claude-sonnet-5-5": (2.00, 10.00),
    "fake": (0.0, 0.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout) / 1_000_000
