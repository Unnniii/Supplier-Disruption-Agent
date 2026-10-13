"""Settings in one place. Override with environment variables or .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

# Which model backend runs the agents:
#   gemini  free tier via Google AI Studio API key (default, GEMINI_API_KEY)
#   ollama  a local model on your machine, free and offline
#   claude  Anthropic API (paid, ANTHROPIC_API_KEY)
#   fake    rule-based stand-in for tests (also: --fake)
PROVIDER = os.getenv("SDA_PROVIDER", "gemini")

DEFAULT_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "ollama": "qwen3:8b",
    "claude": "claude-haiku-5-5",
    "fake": "fake",
}
MODEL = os.getenv("SDA_MODEL") or DEFAULT_MODELS.get(PROVIDER, "fake")
EMBED_MODEL = os.getenv("SDA_EMBED_MODEL", "gemini-embedding-2")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

MAX_TOKENS = int(os.getenv("SDA_MAX_TOKENS", "8192"))  # Gemini 3 counts thinking against this
RETRIEVER = os.getenv("SDA_RETRIEVER", "auto")  # auto | gemini | chroma | keyword
MAX_AGENT_TURNS = 8      # tool-use loop guard per agent
MAX_REVISIONS = 1        # verifier can send the advisor back once

# --- The cap -----------------------------------------------------------------
# Hard ceiling on model calls in one run. A loop bug or a bad prompt cannot burn
# through a free-tier quota (or money) because the run stops at this number.
# On Gemini, the full 34-headline scan needs about 200 calls; the event
# sequence about 70; the cloud job is capped at 150 (infra/variables.tf).
MAX_LLM_CALLS_PER_RUN = int(os.getenv("SDA_MAX_LLM_CALLS", "300"))
# Free tiers rate-limit per minute; wait and retry this many times on HTTP 429.
RATE_LIMIT_RETRIES = 4

CONTRACTS_DIR = ROOT / "data" / "contracts"
CHROMA_DIR = ROOT / "data" / "chroma"
LOG_PATH = ROOT / "logs" / "runs.jsonl"

# USD per million tokens (input, output). Free-tier and local models cost 0;
# tokens are still logged so the paid-tier cost can be estimated if needed.
# Claude source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-09)
PRICES_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),   # prompts <=100K tokens
    "claude-sonnet-5-5": (2.00, 10.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout) / 1_000_000
