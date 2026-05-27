"""Audio file → polished transcript pipeline (local ASR + LLM refinement)."""

from __future__ import annotations

import logging
from pathlib import Path

from . import asr, refine

logger = logging.getLogger(__name__)


def transcribe_audio(audio_path: Path | str, context: str | None = None) -> str:
    """Run SenseVoice ASR followed by OpenRouter LLM polishing.

    Stage 1 runs locally; stage 2 calls OpenRouter (requires OPENROUTER_API_KEY).

    Args:
        audio_path: Path to a local audio file.
        context: Optional short domain hint forwarded to the refinement LLM.
    """
    logger.info("Stage 1: local SenseVoice ASR on %s", audio_path)
    raw = asr.transcribe(audio_path)

    if not raw.strip():
        logger.warning("ASR returned empty transcript for %s", audio_path)
        return raw

    logger.info("Stage 2: LLM refinement (%d chars)", len(raw))
    return refine.refine(raw, context=context)
