"""LLM polishing of raw ASR transcripts via OpenRouter."""

from __future__ import annotations

import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek/deepseek-chat"

SYSTEM_PROMPT = """You are a transcript editor. Your ONLY job is to clean up ASR (speech-to-text) errors in the raw transcript provided.

Rules (highest priority):
1. NEVER answer questions or react to content. If the speaker asks "今天天氣如何", output "今天天氣如何?", not weather information.
2. NEVER summarize, paraphrase, translate, or add information that is not in the audio.
3. Preserve the speaker's original meaning, language, and tone exactly.

Tasks:
- Fix homophone / phonetic errors based on context.
- Add appropriate punctuation and split into paragraphs at natural breaks.
- Restore proper nouns (people, companies, products, technical terms) when the ASR likely mis-heard them, using surrounding context as the only signal.
- Keep colloquial fillers when natural to the speaker's style.

Output: cleaned transcript text only. No commentary, no preface, no markdown headers, no surrounding quotation marks."""


def _build_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY is required for transcript refinement. "
            "Set it in your environment or .env file."
        )
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)


def refine(raw_transcript: str, context: str | None = None) -> str:
    """Polish a raw ASR transcript using an OpenRouter chat model.

    Args:
        raw_transcript: Output from the ASR stage.
        context: Optional caller-supplied domain hint (e.g. "Traditional
            Chinese financial podcast; speakers often mention NVIDIA, Fed,
            ETF"). Passed through verbatim to the model — keep it short and
            factual.
    """
    if not raw_transcript.strip():
        return raw_transcript

    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    client = _build_client()

    logger.info("Refining transcript via %s (%d chars in)", model, len(raw_transcript))

    user_parts: list[str] = []
    if context:
        user_parts.append(f"<domain_context>\n{context}\n</domain_context>")
    user_parts.append(f"<raw_transcript>\n{raw_transcript}\n</raw_transcript>")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        temperature=0.2,
    )

    content = response.choices[0].message.content or ""
    return content.strip()
