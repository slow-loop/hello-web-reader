"""
Generic MCP server for web-reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import ReadCache, read_url as read_url_impl
from .formatting import flatten_results_map, format_results
from .runner import run_config


OutputFormat = Literal["md", "json"]


def _parse_tags(tags: str | None) -> list[str] | None:
    if tags is None:
        return None
    parsed = [tag.strip() for tag in tags.split(",") if tag.strip()]
    return parsed or None


def _build_info() -> dict[str, object]:
    return {
        "package": "web-reader",
        "tools": [
            "read_url",
            "read_config",
            "search_substack",
            "explore_substack",
            "web_reader_server_info",
        ],
        "notes": [
            "This MCP server wraps the Python API directly.",
            "Use read_url for a single URL or pseudo-URL such as gnews://...",
            "Use read_config for YAML feed configs with optional tag filtering.",
        ],
    }


def build_server():
    mcp = FastMCP(
        "web-reader",
        instructions=(
            "Use these tools to read URLs or feed configs with the web-reader Python API. "
            "Prefer read_url for a single URL and read_config for YAML source bundles."
        ),
    )

    async def _read_single_url(
        url: str,
        output_format: OutputFormat = "md",
        no_cache: bool = False,
    ) -> str:
        cache = None if no_cache else ReadCache()
        result = await read_url_impl(url, cache=cache, cache_ttl=3600)
        return format_results([result], output_format)

    @mcp.tool(
        description="Read a single URL or pseudo-URL such as gnews://... and return markdown or JSON output.",
    )
    async def read_url(
        url: str,
        output_format: OutputFormat = "md",
        no_cache: bool = False,
    ) -> str:
        return await _read_single_url(url, output_format=output_format, no_cache=no_cache)

    @mcp.tool(
        description="Run a web-reader YAML config file, optionally filter by comma-separated tags, and return markdown or JSON output.",
    )
    async def read_config(
        config_path: str,
        tags: str | None = None,
        output_format: OutputFormat = "md",
        no_cache: bool = False,
    ) -> str:
        resolved = Path(config_path).resolve()
        cache = None if no_cache else ReadCache()
        results_map = await run_config(
            str(resolved),
            tags=_parse_tags(tags),
            no_cache=no_cache,
            cache=cache,
        )
        return format_results(flatten_results_map(results_map), output_format)

    @mcp.tool(
        description="Search Substack posts across all publications. Returns titles, snippets, authors, and links.",
    )
    async def search_substack(
        query: str,
        page: int = 0,
        filter_type: str = "all",
        output_format: OutputFormat = "md",
        no_cache: bool = False,
    ) -> str:
        from .readers.substack import search_substack as _search

        cache = None if no_cache else ReadCache()
        result = await _search(query, page=page, filter_type=filter_type)
        if cache and result.success:
            cache.save(result)
        return format_results([result], output_format)

    @mcp.tool(
        description="Browse curated Substack content by category (finance, technology, politics, culture, business, science, health).",
    )
    async def explore_substack(
        tab: str = "finance",
        output_format: OutputFormat = "md",
        no_cache: bool = False,
    ) -> str:
        from .readers.substack import explore_substack as _explore

        cache = None if no_cache else ReadCache()
        result = await _explore(tab=tab)
        if cache and result.success:
            cache.save(result)
        return format_results([result], output_format)

    @mcp.tool(
        description="Return metadata about the web-reader MCP server and its generic tools.",
    )
    def web_reader_server_info() -> str:
        return json.dumps(_build_info(), indent=2, ensure_ascii=False)

    return mcp


def main() -> None:
    server = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
