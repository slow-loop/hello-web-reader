"""
Shared output formatting helpers for CLI and MCP adapters.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

from .models import ReadResult


def format_results_json(results: list[ReadResult]) -> str:
    data = [result.model_dump(mode="json", exclude_none=True) for result in results]
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_results_markdown(results: list[ReadResult]) -> str:
    parts: list[str] = []
    for result in results:
        if not result.success:
            parts.append(f"## ERROR: {unquote(result.url)}\n{result.error}\n")
            continue

        header = f"## {result.title}" if result.title else f"## {unquote(result.url)}"
        meta_parts: list[str] = []
        if result.source_type != "unknown":
            meta_parts.append(f"source: {result.source_type}")
        if result.author:
            meta_parts.append(f"author: {result.author}")
        if result.published_at:
            meta_parts.append(f"date: {result.published_at.strftime('%Y-%m-%d')}")
        if result.cached:
            meta_parts.append("(cached)")

        parts.append(header)
        if meta_parts:
            parts.append(" | ".join(meta_parts))
        if result.url:
            parts.append(f"link: {unquote(result.url)}")
        parts.append("")
        parts.append(result.text)
        parts.append("\n---\n")

    return "\n".join(parts)


def format_results(results: list[ReadResult], output_format: str) -> str:
    if output_format == "md":
        return format_results_markdown(results)
    return format_results_json(results)


def flatten_results_map(results_map: dict[str, list[ReadResult]]) -> list[ReadResult]:
    all_results: list[ReadResult] = []
    for source_name, results in results_map.items():
        for result in results:
            if not result.title:
                result.title = source_name
            all_results.append(result)
    return all_results
