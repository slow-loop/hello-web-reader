from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "split_audio_on_silence.py"
spec = importlib.util.spec_from_file_location("split_audio_on_silence", SCRIPT_PATH)
splitter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = splitter
spec.loader.exec_module(splitter)


def test_parse_silencedetect_output_returns_spans():
    stderr = """
[Parsed_silencedetect_0 @ 0x1] silence_start: 605.0
[Parsed_silencedetect_0 @ 0x1] silence_end: 606.2 | silence_duration: 1.2
[Parsed_silencedetect_0 @ 0x1] silence_start: 1210.4
[Parsed_silencedetect_0 @ 0x1] silence_end: 1211.0 | silence_duration: 0.6
"""

    spans = splitter.parse_silencedetect_output(stderr)

    assert spans == [
        splitter.SilenceSpan(start=605.0, end=606.2, duration=1.2),
        splitter.SilenceSpan(start=1210.4, end=1211.0, duration=0.6),
    ]


def test_plan_chunks_prefers_silence_near_target():
    silences = [
        splitter.SilenceSpan(start=605.0, end=606.0, duration=1.0),
        splitter.SilenceSpan(start=1210.0, end=1211.0, duration=1.0),
    ]

    chunks = splitter.plan_chunks(
        duration=1800.0,
        silences=silences,
        target_sec=600.0,
        min_sec=420.0,
        max_sec=900.0,
        overlap_sec=1.0,
    )

    assert [(round(c.start, 1), round(c.end, 1), c.hard_cut) for c in chunks] == [
        (0.0, 605.5, False),
        (605.5, 1210.5, False),
        (1210.5, 1800.0, False),
    ]
    assert chunks[1].extract_start == 604.5
    assert chunks[1].extract_end == 1211.5


def test_plan_chunks_falls_back_to_hard_cut_when_no_silence_in_window():
    chunks = splitter.plan_chunks(
        duration=1900.0,
        silences=[],
        target_sec=600.0,
        min_sec=420.0,
        max_sec=900.0,
        overlap_sec=1.0,
    )

    assert [(c.start, c.end, c.hard_cut) for c in chunks] == [
        (0.0, 900.0, True),
        (900.0, 1900.0, True),
    ]
