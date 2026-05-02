"""
CLI for web-reader.

Usage:
    uv run web-reader <target> [OPTIONS]

Target is either a .yaml/.yml config file or a URL.

Examples:
    uv run web-reader feeds.yaml
    uv run web-reader feeds.yaml --tags=finance,tech
    uv run web-reader feeds.yaml --tags=finance --no-cache
    uv run web-reader "https://hnrss.org/frontpage"
    uv run web-reader "https://reddit.com/r/investing/hot"
"""

import argparse
import asyncio
import logging
from urllib.parse import parse_qs

from .formatting import flatten_results_map, format_results
from .store import ReadStore


def _is_config_file(target: str) -> bool:
    return target.endswith((".yaml", ".yml"))


def _print_results(results, output_format: str) -> None:
    print(format_results(results, output_format))


async def _run_url(url: str, no_cache: bool, output_format: str) -> None:
    """Fetch a single URL."""
    from ._detect import detect_source_type

    source_type = detect_source_type(url)
    store = ReadStore() if not no_cache else None

    if store and source_type != "rss":
        cached = store.get_cached(url, ttl_seconds=3600, source_type=source_type)
        if cached:
            _print_results([cached], output_format)
            return

    if source_type == "gnews":
        from .readers.gnews import read_gnews
        # Parse: gnews://NVDA stock?period=7d&max=5
        raw = url[len("gnews://"):]
        parts = raw.split("?", 1)
        query = parts[0]
        qs = parse_qs(parts[1]) if len(parts) > 1 else {}
        period = qs.get("period", [None])[0]
        max_results = int(qs.get("max", [10])[0])
        results = await read_gnews(query, period=period, max_results=max_results)
        if store:
            for r in results:
                if r.success:
                    store.save(r)
        _print_results(results, output_format)
        return
    elif source_type == "ptt":
        from .readers.ptt import read_ptt
        result = await read_ptt(url)
    elif source_type == "reddit":
        from .readers.reddit import read_reddit
        result = await read_reddit(url)
    elif source_type == "stocktwits":
        from .readers.stocktwits import read_stocktwits
        result = await read_stocktwits(url)
    elif source_type == "substack" and url.startswith("substack://search"):
        from .readers.substack import search_substack
        raw = url[len("substack://search"):]
        qs = parse_qs(raw.lstrip("?"))
        query = qs.get("q", [""])[0]
        page = int(qs.get("page", [0])[0])
        result = await search_substack(query, page=page)
    elif source_type == "substack" and url.startswith("substack://explore"):
        from .readers.substack import explore_substack
        raw = url[len("substack://explore"):]
        qs = parse_qs(raw.lstrip("?"))
        tab = qs.get("tab", ["finance"])[0]
        result = await explore_substack(tab=tab)
    elif source_type == "substack":
        from .readers.substack import read_substack
        result = await read_substack(url)
    elif source_type == "youtube":
        from .readers.youtube import read_youtube
        result = await read_youtube(url)
    elif source_type == "rss":
        from .readers.rss import read_rss
        result = await read_rss(url)
    elif source_type == "json":
        from .readers.json_api import read_json
        result = await read_json(url)
    else:
        from .readers.web import read_web
        result = await read_web(url)

    if store and result.success and source_type != "rss":
        store.save(result)

    _print_results([result], output_format)


async def _run_config(config_path: str, tags: list[str] | None, no_cache: bool, output_format: str) -> None:
    """Run a YAML config file."""
    from .runner import run_config

    store = ReadStore() if not no_cache else None
    results_map = await run_config(config_path, tags=tags, no_cache=no_cache, store=store)
    _print_results(flatten_results_map(results_map), output_format)


def main():
    parser = argparse.ArgumentParser(
        prog="web-reader",
        description="Lightweight web reader — fetch structured content from URLs and feeds.",
        usage="web-reader <target> [options]",
    )
    parser.add_argument("target", help="URL or .yaml/.yml config file path")
    parser.add_argument("--tags", help="Filter config sources by tags (comma-separated)", default=None)
    parser.add_argument("--no-cache", action="store_true", help="Skip cache, always re-fetch")
    parser.add_argument("--format", choices=["json", "md"], default="md", help="Output format (default: md)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None

    if _is_config_file(args.target):
        asyncio.run(_run_config(args.target, tags, args.no_cache, args.format))
    else:
        asyncio.run(_run_url(args.target, args.no_cache, args.format))


if __name__ == "__main__":
    main()
