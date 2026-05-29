"""Audio file → polished transcript pipeline (local ASR + LLM refinement)."""

from __future__ import annotations

import logging
from pathlib import Path

import opencc

from . import asr, refine

logger = logging.getLogger(__name__)

_s2twp = opencc.OpenCC("s2twp")


def transcribe_audio(audio_path: Path | str, vocabulary_terms: list[str] | None = None) -> str:
    """Run SenseVoice ASR followed by OpenRouter LLM polishing.

    Stage 1 runs locally; stage 2 calls OpenRouter (requires OPENROUTER_API_KEY).
    Between stages, OpenCC s2twp converts Simplified Chinese ASR output to
    Traditional Chinese (Taiwan) so the LLM preserves the correct script.

    Args:
        audio_path: Path to a local audio file.
        vocabulary_terms: Optional list of proper nouns / terms ASR often mishears.
    """
    logger.info("Stage 1: local SenseVoice ASR on %s", audio_path)
    raw = asr.transcribe(audio_path)

    if not raw.strip():
        logger.warning("ASR returned empty transcript for %s", audio_path)
        return raw

    logger.info("Stage 1b: OpenCC s2twp conversion (%d chars)", len(raw))
    raw = _s2twp.convert(raw)

    logger.info("Stage 2: LLM refinement (%d chars)", len(raw))
    return refine.refine(raw, vocabulary_terms=vocabulary_terms)
