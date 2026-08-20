#!/usr/bin/env python3
"""
diagnose.py — figure out why mine_corrections.py found nothing.

Read-only. Writes no files. Prints:
  1. how far back your surviving transcripts actually go
  2. a filter funnel: where your user turns are being dropped
  3. a sample of your real user turns, with the score each got
  4. the actual structure of one auto-memory file

Usage:  python3 diagnose.py
        python3 diagnose.py --sample 40 --show-memory 60
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from mine_corrections import (COMPILED, content_tokens, extract_text,
                                  is_noise, parse_ts, score, strip_code)
except ImportError:
    print("Put diagnose.py in the same folder as mine_corrections.py.")
    raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path.home() / ".claude"))
    ap.add_argument("--sample", type=int, default=30,
                    help="how many real user turns to print")
    ap.add_argument("--show-memory", type=int, default=45,
                    help="lines of one memory file to print verbatim")
    ap.add_argument("--max-chars", type=int, default=600)
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not (root / "projects").is_dir():
        print(f"no projects/ under {root}")
        return 1

    # ---------------- 1. what survived ---------------- #
    main_files = sorted(root.glob("projects/*/*.jsonl"))
    sub_files = sorted(root.glob("projects/*/*/subagents/*.jsonl"))
    print("=" * 66)
    print("1. WHAT SURVIVED THE RETENTION SWEEP")
    print("=" * 66)
    print(f"main transcripts:     {len(main_files)}")
    print(f"subagent transcripts: {len(sub_files)}  (not scanned by the miner)")
    if main_files:
        mtimes = sorted(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
                        for f in main_files)
        span = (mtimes[-1] - mtimes[0]).days
        print(f"oldest transcript:    {mtimes[0].date()}")
        print(f"newest transcript:    {mtimes[-1].date()}")
        print(f"window:               {span} days")
        if span < 35:
            print("  -> consistent with the 30-day sweep having already run.")
    total_bytes = sum(f.stat().st_size for f in main_files)
    print(f"total size:           {total_bytes / 1e6:.1f} MB")

    # ---------------- 2. the funnel ---------------- #
    print()
    print("=" * 66)
    print("2. FILTER FUNNEL — where user turns are being dropped")
    print("=" * 66)
    f = Counter()
    types = Counter()
    samples: list[tuple[float, str, str]] = []
    lengths: list[int] = []

    for path in main_files:
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    f["unparseable line"] += 1
                    continue
                f["total records"] += 1
                types[str(rec.get("type"))] += 1
                if rec.get("type") != "user":
                    continue
                f["type == user"] += 1
                if rec.get("isMeta"):
                    f["  dropped: isMeta"] += 1
                    continue
                text = extract_text(rec.get("message"))
                if not text:
                    f["  dropped: no text block (tool_result)"] += 1
                    continue
                if is_noise(text):
                    f["  dropped: injected/system prefix"] += 1
                    continue
                f["REAL user turns"] += 1
                body = strip_code(text).strip()
                lengths.append(len(body))
                if len(body) < 8:
                    f["  dropped: shorter than 8 chars"] += 1
                    continue
                if len(body) > args.max_chars:
                    f[f"  dropped: longer than {args.max_chars} chars"] += 1
                    continue
                s, _tags = score(body)
                toks = content_tokens(body)
                if len(toks) < 2:
                    f["  dropped: fewer than 2 content tokens"] += 1
                    continue
                samples.append((s, f"{path.name}:{lineno}", body))
                if s < 1.0:
                    f["  dropped: score 0 (no pattern matched)"] += 1
                elif s < 2.0:
                    f["  would pass at --min-score 1.0"] += 1
                else:
                    f["PASSES at default --min-score 2.0"] += 1

    order = ["total records", "type == user", "  dropped: isMeta",
             "  dropped: no text block (tool_result)",
             "  dropped: injected/system prefix", "REAL user turns",
             "  dropped: shorter than 8 chars",
             f"  dropped: longer than {args.max_chars} chars",
             "  dropped: fewer than 2 content tokens",
             "  dropped: score 0 (no pattern matched)",
             "  would pass at --min-score 1.0",
             "PASSES at default --min-score 2.0"]
    for k in order:
        if f[k]:
            print(f"{f[k]:>7}  {k}")
    print(f"\nrecord types seen: {dict(types)}")
    if lengths:
        lengths.sort()
        print(f"user turn length: median {lengths[len(lengths)//2]} chars, "
              f"max {lengths[-1]}")

    # ---------------- 3. your real turns ---------------- #
    print()
    print("=" * 66)
    print(f"3. YOUR REAL USER TURNS (top {args.sample} by score)")
    print("   Skim before sharing — this is your own text.")
    print("=" * 66)
    samples.sort(key=lambda t: -t[0])
    for s, cite, body in samples[:args.sample]:
        one_line = " ".join(body.split())[:150]
        print(f"[score {s:>4.1f}] {one_line}")
    print()
    print("Patterns that matched nothing across your whole history:")
    unused = []
    for rx, w, tag in COMPILED:
        if not any(rx.search(b) for _s, _c, b in samples):
            unused.append(f"{rx.pattern}  ({tag})")
    print("  " + ("\n  ".join(unused) if unused else "(none — all fired)"))

    # ---------------- 4. memory file shape ---------------- #
    print()
    print("=" * 66)
    print("4. AUTO MEMORY — actual file structure")
    print("=" * 66)
    mem = sorted(root.glob("projects/*/memory/**/*.md"),
                 key=lambda p: -p.stat().st_size)
    print(f"memory files found: {len(mem)}")
    for p in mem[:8]:
        print(f"  {p.stat().st_size:>7} bytes  {p.parent.parent.name}/{p.name}")
    if mem:
        print(f"\n--- first {args.show_memory} lines of {mem[0].name} verbatim ---")
        try:
            for line in mem[0].read_text(encoding="utf-8",
                                         errors="replace").splitlines()[:args.show_memory]:
                print("  " + line)
        except OSError as e:
            print(f"  (unreadable: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
