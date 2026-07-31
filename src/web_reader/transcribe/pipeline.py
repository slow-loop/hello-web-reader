"""Audio file → polished transcript pipeline (local ASR + LLM refinement)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import asr, refine

logger = logging.getLogger(__name__)


def transcribe_audio(audio_path: Path | str, vocabulary_terms: list[str] | None = None) -> str:
    """Run SenseVoice ASR followed by OpenRouter LLM polishing.

    Stage 1 runs locally; stage 2 calls OpenRouter (requires OPENROUTER_API_KEY).
    No script normalization: SenseVoice emits Simplified Chinese, and converting
    here (we used OpenCC s2twp) rewrote vocabulary the speaker actually chose
    (软件 → 軟體, 项目 → 專案) irreversibly, before the LLM pass could tell the
    substitution apart from a real ASR error. Convert at the point of
    publication instead, where the target audience is known.

    Args:
        audio_path: Path to a local audio file.
        vocabulary_terms: Optional list of proper nouns / terms ASR often mishears.
    """
    logger.info("Stage 1: local SenseVoice ASR on %s", audio_path)
    raw = asr.transcribe(audio_path)

    if not raw.strip():
        logger.warning("ASR returned empty transcript for %s", audio_path)
        return raw

    if os.environ.get("TRANSCRIBE_SKIP_REFINE", "").strip().lower() in ("1", "true", "yes"):
        logger.info("Stage 2 skipped (TRANSCRIBE_SKIP_REFINE set); returning local ASR output")
        return raw

    logger.info("Stage 2: LLM refinement (%d chars)", len(raw))
    return refine.refine(raw, vocabulary_terms=vocabulary_terms)
