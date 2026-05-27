"""Local speech-to-text via FunASR SenseVoice.

Requires `pip install web-reader[transcribe]`. The first call downloads the
SenseVoiceSmall model (~350MB) into the modelscope cache.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_model_lock = Lock()
_model: Any = None


def _get_model() -> Any:
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is None:
            try:
                from funasr import AutoModel
            except ImportError as exc:
                raise ImportError(
                    "funasr is not installed. Install with: pip install 'web-reader[transcribe]'"
                ) from exc

            logger.info("Loading SenseVoice model (first run downloads ~350MB)...")
            _model = AutoModel(
                model="iic/SenseVoiceSmall",
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                disable_update=True,
            )
    return _model


def transcribe(audio_path: Path | str) -> str:
    """Transcribe a local audio file to plain text."""
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    model = _get_model()
    result = model.generate(
        input=str(audio_path),
        cache={},
        language="auto",
        use_itn=True,
        batch_size_s=60,
        merge_vad=True,
        merge_length_s=15,
    )

    if not result:
        return ""

    return rich_transcription_postprocess(result[0]["text"])
