"""Local speech-to-text via FunASR SenseVoice.

Requires `pip install web-reader[transcribe]`. The first call downloads the
SenseVoiceSmall model (~350MB) into the modelscope cache.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_model_lock = Lock()
_model: Any = None

# Loggers funasr / modelscope / torchaudio chatter through. We pin them at
# WARNING so only real problems reach the user. Set WEB_READER_ASR_VERBOSE=1
# to opt back into the noise (useful when debugging model loading).
_NOISY_LOGGERS = ("funasr", "modelscope", "torch", "torchaudio", "root")


def _quiet_loggers() -> None:
    if os.environ.get("WEB_READER_ASR_VERBOSE"):
        return
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@contextlib.contextmanager
def _suppress_progress():
    """Swallow funasr's tqdm progress bars + raw prints during inference.

    funasr writes RTF stats and tqdm bars directly to stderr/stdout; there is
    no public flag to disable them. We redirect both streams to a buffer for
    the duration of the call so callers see clean output.
    """
    if os.environ.get("WEB_READER_ASR_VERBOSE"):
        yield
        return
    buf_out, buf_err = io.StringIO(), io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = buf_out, buf_err
        yield
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


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

            _quiet_loggers()
            logger.info("Loading SenseVoice model (first run downloads ~350MB)...")

            # funasr emits `WARNING:root:trust_remote_code: False` directly via
            # the root logger during AutoModel construction. Temporarily lift
            # the root level so it stays out of the user's stderr.
            root_logger = logging.getLogger()
            prior_level = root_logger.level
            try:
                if not os.environ.get("WEB_READER_ASR_VERBOSE"):
                    root_logger.setLevel(logging.ERROR)
                with _suppress_progress():
                    _model = AutoModel(
                        model="iic/SenseVoiceSmall",
                        vad_model="fsmn-vad",
                        vad_kwargs={"max_single_segment_time": 30000},
                        disable_update=True,
                    )
            finally:
                root_logger.setLevel(prior_level)
    return _model


def transcribe(audio_path: Path | str) -> str:
    """Transcribe a local audio file to plain text."""
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    model = _get_model()
    with _suppress_progress():
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
