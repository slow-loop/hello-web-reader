#!/usr/bin/env python3
"""Export local Claude conversation history (Claude Code CLI + Claude Desktop Cowork).

Background — where these live on disk (macOS only):
    Claude Code CLI sessions, one JSONL file per session:
        ~/.claude/projects/<encoded-project-path>/<session-id>.jsonl
    Claude Desktop Cowork sessions, one directory per session, transcript in `audit.jsonl`
    (same shape as Claude Code's JSONL, plus audit signature fields):
        ~/Library/Application Support/Claude/local-agent-mode-sessions/<workspace-id>/<space-id>/local_<session-id>/audit.jsonl
    A sibling `local_<session-id>.json` holds Cowork session metadata (title, timestamps).

Both sources may contain other projects' code, credentials, or otherwise sensitive
content — run with --dry-run first to see what would be exported, especially when
not narrowing with --project or --keyword.

Usage:
    # Preview (no copying) Claude Code sessions from the last 7 days for one project
    uv run scripts/export_claude_conversations.py --source code --project hello-trader-skill --dry-run

    # Export both sources from the last 3 days
    uv run scripts/export_claude_conversations.py --days 3 --out ./output/claude-conversations

    # Only sessions mentioning a keyword, also emit a readable .md next to each .jsonl
    uv run scripts/export_claude_conversations.py --keyword serenity --clean
"""

import argparse
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
COWORK_SESSIONS_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
)

SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
COMMAND_TAG_RE = re.compile(r"<command-[a-z-]+>.*?</command-[a-z-]+>", re.DOTALL)


def find_code_sessions(days: int, project_filter: str | None) -> list[tuple[str, Path]]:
    cutoff = time.time() - days * 86400
    results = []
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir() if CLAUDE_PROJECTS_DIR.exists() else []:
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in project_dir.name.lower():
            continue
        for jsonl in project_dir.glob("*.jsonl"):  # top-level only — skips the subagents/ subdir
            if jsonl.stat().st_mtime >= cutoff:
                results.append((project_dir.name, jsonl))
    return results


def find_cowork_sessions(days: int) -> list[tuple[str, int | None, Path]]:
    cutoff = time.time() - days * 86400
    results = []
    for audit in COWORK_SESSIONS_DIR.rglob("audit.jsonl") if COWORK_SESSIONS_DIR.exists() else []:
        if audit.stat().st_mtime < cutoff:
            continue
        meta_path = audit.parent.with_suffix(".json")
        title, last_activity = "untitled", None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                title = meta.get("title") or title
                last_activity = meta.get("lastActivityAt")
            except Exception:
                pass
        results.append((title, last_activity, audit))
    return results


def matches_keyword(path: Path, keyword: str) -> bool:
    try:
        return keyword.lower() in path.read_text(errors="ignore").lower()
    except Exception:
        return False


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def summarize_tool_use(name: str, tool_input) -> str:
    if not isinstance(tool_input, dict):
        return f"[used tool: {name}]"
    for key in ("file_path", "path", "command", "query", "pattern", "url", "prompt"):
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > 150:
                val = val[:150] + "…"
            return f"[used tool: {name}] {key}={val}"
    return f"[used tool: {name}]"


def clean_transcript(jsonl_path: Path) -> str:
    """Strip a raw session JSONL down to human dialogue: user/assistant text plus
    one-line tool-use summaries. Tool results and internal thinking blocks are dropped."""
    lines_out = []
    with jsonl_path.open(errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant"):
                continue

            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if not isinstance(blocks, list):
                continue

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = SYSTEM_REMINDER_RE.sub("", block.get("text", ""))
                    text = COMMAND_TAG_RE.sub("", text).strip()
                    if text:
                        lines_out.append(f"**{role}:** {text}\n")
                elif btype == "tool_use":
                    summary = summarize_tool_use(block.get("name", "?"), block.get("input"))
                    lines_out.append(f"_{summary}_\n")
                # tool_result / thinking blocks intentionally omitted
    return "\n".join(lines_out)


def export(dest_name: str, src: Path, out_dir: Path, clean: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{dest_name}.jsonl"
    shutil.copy2(src, dest)
    print(f"  {dest}")
    if clean:
        md_dest = out_dir / f"{dest_name}.md"
        md_dest.write_text(clean_transcript(src))
        print(f"  {md_dest}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", choices=["code", "cowork", "both"], default="both")
    parser.add_argument("--days", type=int, default=7, help="Only sessions active in the last N days (default: 7)")
    parser.add_argument("--project", help="Claude Code only — substring filter on the project's encoded directory name")
    parser.add_argument("--keyword", help="Only keep sessions whose raw content contains this text (case-insensitive)")
    parser.add_argument("--out", type=Path, default=Path("output/claude-conversations"), help="Destination directory (default: output/claude-conversations)")
    parser.add_argument("--clean", action="store_true", help="Also write a readable .md transcript (dialogue only, tool noise stripped) next to each .jsonl")
    parser.add_argument("--dry-run", action="store_true", help="List matching sessions without copying anything")
    args = parser.parse_args()

    if args.source in ("code", "both"):
        sessions = find_code_sessions(args.days, args.project)
        if args.keyword:
            sessions = [(p, f) for p, f in sessions if matches_keyword(f, args.keyword)]
        print(f"Claude Code: {len(sessions)} session(s)")
        for project_name, jsonl in sessions:
            if args.dry_run:
                print(f"  [dry-run] {project_name}/{jsonl.name}")
                continue
            export(jsonl.stem, jsonl, args.out / "code" / project_name, args.clean)

    if args.source in ("cowork", "both"):
        sessions = find_cowork_sessions(args.days)
        if args.keyword:
            sessions = [(t, ts, f) for t, ts, f in sessions if matches_keyword(f, args.keyword)]
        print(f"Cowork: {len(sessions)} session(s)")
        for title, last_activity, audit in sessions:
            date = (
                datetime.fromtimestamp(last_activity / 1000).strftime("%Y-%m-%d")
                if last_activity
                else "unknown-date"
            )
            session_id = audit.parent.name.removeprefix("local_")[:8]
            dest_name = f"{date}_{slugify(title)}_{session_id}"
            if args.dry_run:
                print(f"  [dry-run] {dest_name}")
                continue
            export(dest_name, audit, args.out / "cowork", args.clean)


if __name__ == "__main__":
    main()
