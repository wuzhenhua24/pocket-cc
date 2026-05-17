"""parse_transcript.py — M1 切片 B 真实数据演示.

输入 Claude Code JSONL transcript 路径, 解析并打印事件流.
不传参数时, 自动挑 ~/.claude/projects 下最大的 jsonl 作为样本.

用法:
    uv run python examples/parse_transcript.py
    uv run python examples/parse_transcript.py /path/to/session.jsonl
    uv run python examples/parse_transcript.py /path/to/session.jsonl --limit 50

每条事件占一行, 格式: [tag] uuid…  body-snippet
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections import Counter
from pathlib import Path

from pocket_cc.claude.transcript import (
    AssistantText,
    AssistantThinking,
    ToolResult,
    ToolUse,
    TranscriptReader,
    UserText,
)

_TAG = {
    UserText: "USER",
    AssistantText: "TEXT",
    AssistantThinking: "THNK",
    ToolUse: "USE ",
    ToolResult: "RES ",
}


def _snippet(s: str, limit: int = 80) -> str:
    one_line = " ".join(s.split())
    return textwrap.shorten(one_line, width=limit, placeholder="…")


def _find_default_transcript() -> Path | None:
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    candidates = list(base.glob("*/*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", nargs="?", type=Path, help="JSONL transcript path")
    parser.add_argument("--limit", type=int, default=80, help="Max events to print (default 80)")
    args = parser.parse_args()

    path = args.path or _find_default_transcript()
    if path is None:
        print("No transcript found. Either pass a path or use Claude Code first.", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"Not found: {path}", file=sys.stderr)
        return 1

    print(f"# transcript: {path}  ({path.stat().st_size:,} bytes)")
    reader = TranscriptReader(path=path)
    events = reader.read_new()
    print(f"# parsed {len(events)} events, byte_offset={reader.byte_offset:,}\n")

    counts: Counter[str] = Counter()
    limit = max(1, args.limit)
    for i, ev in enumerate(events[:limit]):
        tag = _TAG[type(ev)]
        counts[tag] += 1
        uuid = ev.uuid[:8] if ev.uuid else "--------"
        if isinstance(ev, ToolUse):
            line = f"name={ev.tool_name} input={_snippet(str(ev.tool_input), 90)}"
        elif isinstance(ev, ToolResult):
            err = " [ERROR]" if ev.is_error else ""
            line = f"id={ev.tool_use_id[:8]}{err} body={_snippet(ev.content, 80)}"
        else:
            line = _snippet(ev.text, 100)
        print(f"[{tag}] {uuid}  {line}")

    # tally everything (not just the printed subset)
    full_counts: Counter[str] = Counter(_TAG[type(e)] for e in events)
    print(
        f"\n# summary: total={len(events)}  "
        + "  ".join(f"{k}={v}" for k, v in full_counts.items())
    )
    if len(events) > limit:
        print(f"# (printed first {limit}; pass --limit N to see more)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
