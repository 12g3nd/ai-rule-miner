#!/usr/bin/env python3
"""
mine_corrections.py — mine Claude Code history for cross-project correction patterns.

Produces candidate rules for a *universal* ~/.claude/CLAUDE.md, ranked by how many
distinct projects they appear in (not raw frequency), recency-weighted, and each
backed by citations you can spot-check.

Stdlib only. Read-only: never writes to ~/.claude.

Usage:
    python3 mine_corrections.py                        # report to stdout
    python3 mine_corrections.py --out ./mined          # write review sheet + JSON
    python3 mine_corrections.py --min-projects 3       # stricter universality bar
    python3 mine_corrections.py --root /path/to/.claude
    python3 mine_corrections.py --memory-only          # just collate auto memory
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Correction signal
# --------------------------------------------------------------------------- #
# Weight = how strongly this phrasing implies "Claude's default diverged from my
# preference". Repetition markers score highest: they mean CLAUDE.md failed to
# carry the rule, which is precisely what we want to capture.

PATTERNS: list[tuple[str, float, str]] = [
    (r"\bi (?:already )?told you\b",                    3.0, "repetition"),
    (r"\b(?:i said|as i said)\b",                       2.5, "repetition"),
    (r"\bagain[,.!]",                                   2.0, "repetition"),
    (r"\bstop (?:doing|adding|using|trying)\b",         2.5, "prohibition"),
    (r"\b(?:never|do not ever) (?:use|do|add|write)\b",  2.5, "prohibition"),
    (r"\b(?:don'?t|do not) (?:use|add|write|create|touch|change|refactor)\b",
                                                         2.0, "prohibition"),
    (r"\balways (?:use|run|prefer|check|add)\b",         2.0, "directive"),
    (r"\bwe (?:use|prefer|don'?t use|never use)\b",      2.0, "convention"),
    (r"\buse \S+ (?:not|instead of) \S+",               2.0, "convention"),
    (r"\b(?:instead of|rather than)\b",                 1.2, "convention"),
    (r"\bactually,?\b",                                 1.0, "correction"),
    (r"\bthat'?s (?:not|wrong|incorrect)\b",            1.5, "correction"),
    (r"\b(?:no|nope)[,.!]",                             1.2, "correction"),
    (r"\bwhy (?:did|are) you\b",                        1.5, "correction"),
    (r"\b(?:revert|undo) (?:that|this|it)\b",           1.5, "correction"),
    (r"\bremember (?:that|to)?\b",                      1.5, "directive"),
    (r"\b(?:please )?just \S+",                         0.8, "directive"),
]
COMPILED = [(re.compile(p, re.I), w, tag) for p, w, tag in PATTERNS]

# Injected/system text that is not the human speaking.
NOISE_PREFIXES = (
    "<command-name>", "<command-message>", "<local-command-stdout>",
    "<system-reminder>", "<bash-input>", "<bash-stdout>",
    "Caveat: The messages below", "[Request interrupted",
    "This session is being continued", "<user-prompt-submit-hook>",
)

STOPWORDS = set("""
a an the and or but if then than that this these those there here it its is are was were be
been being do does did doing done have has had having i you we they he she me my your our their
to of in on at for with from by about as into over after before again not no nope don't dont
can could should would will just also very really please thanks thank ok okay yeah yes sure
what why how when where which who whom use used using make made get got go going want need
let lets like actually instead rather stop always never remember told said say says
""".split())

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*")


# --------------------------------------------------------------------------- #
# Transcript parsing
# --------------------------------------------------------------------------- #

def extract_text(message) -> str:
    """Pull human-authored text out of a transcript message, whatever the shape."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            # tool_result blocks are machine output, not the user talking.
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def is_noise(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in NOISE_PREFIXES)


def strip_code(text: str) -> str:
    """Drop fenced blocks so pasted code doesn't dominate the token space."""
    return re.sub(r"```.*?```", " ", text, flags=re.S)


def parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_user_turns(path: Path):
    """Yield (line_no, text, timestamp, cwd) for genuine user turns."""
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "user" or rec.get("isMeta"):
                continue
            text = extract_text(rec.get("message"))
            if not text or is_noise(text):
                continue
            yield lineno, text, parse_ts(rec.get("timestamp")), rec.get("cwd")


# --------------------------------------------------------------------------- #
# Scoring / normalisation
# --------------------------------------------------------------------------- #

def score(text: str) -> tuple[float, set[str]]:
    total, tags = 0.0, set()
    for rx, weight, tag in COMPILED:
        if rx.search(text):
            total += weight
            tags.add(tag)
    return total, tags


def content_tokens(text: str) -> set[str]:
    """Tokens that could plausibly name a convention.

    Project-specific identifiers (paths, dotted module names) are dropped: they
    are exactly what stops the same rule in two repos from clustering together.
    """
    out = set()
    for tok in TOKEN_RE.findall(text.lower()):
        if len(tok) < 3 or tok in STOPWORDS:
            continue
        if "/" in tok or "\\" in tok:
            continue
        if tok.count(".") and not tok.endswith((".md", ".json")):
            continue  # foo.bar.baz — almost always project-local
        out.add(tok.strip("._-+"))
    return {t for t in out if len(t) >= 3}


def recency_weight(ts: datetime | None, now: datetime, half_life_days: float) -> float:
    if ts is None:
        return 0.3
    age = max((now - ts).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age / half_life_days)


# --------------------------------------------------------------------------- #
# Clustering (leader / single-pass — stable and explainable)
# --------------------------------------------------------------------------- #

def cluster(items: list[dict], threshold: float, min_shared: int) -> list[dict]:
    clusters: list[dict] = []
    for item in sorted(items, key=lambda i: -i["weighted"]):
        toks = item["tokens"]
        best, best_sim = None, 0.0
        for c in clusters:
            shared = toks & c["centroid"]
            if len(shared) < min_shared:
                continue
            sim = len(shared) / max(len(toks | c["centroid"]), 1)
            if sim >= threshold and sim > best_sim:
                best, best_sim = c, sim
        if best is None:
            clusters.append({"centroid": set(toks), "items": [item]})
        else:
            best["items"].append(item)
    return clusters


# --------------------------------------------------------------------------- #
# Auto memory collation
# --------------------------------------------------------------------------- #

FRONTMATTER_MODIFIED = re.compile(r"^modified:\s*(\S+)", re.M)


def read_memory(root: Path) -> list[dict]:
    """Collect auto-memory lines per project, with the `modified` stamp if present."""
    out = []
    for mem_dir in sorted(root.glob("projects/*/memory")):
        project = mem_dir.parent.name
        for md in sorted(mem_dir.rglob("*.md")):
            try:
                body = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = FRONTMATTER_MODIFIED.search(body[:600])
            modified = m.group(1) if m else None
            for line in body.splitlines():
                line = line.strip().lstrip("-*").strip()
                if len(line) < 12 or line.startswith(("#", "```", "---", "modified:")):
                    continue
                out.append({
                    "project": project,
                    "file": str(md),
                    "modified": modified,
                    "text": line,
                    "tokens": content_tokens(line),
                })
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def collect_transcripts(root: Path, min_score: float, max_chars: int, now: datetime,
                        half_life: float, verbose: bool) -> tuple[list[dict], dict]:
    items: list[dict] = []
    stats = {"sessions": 0, "user_turns": 0, "projects": set(), "hits": 0}

    for session in sorted(root.glob("projects/*/*.jsonl")):
        stats["sessions"] += 1
        project_dir = session.parent.name
        project_label = None
        for lineno, text, ts, cwd in iter_user_turns(session):
            stats["user_turns"] += 1
            if cwd and not project_label:
                project_label = cwd
            body = strip_code(text).strip()
            # Terse turns are corrections; long ones are specs and pasted logs.
            if not (8 <= len(body) <= max_chars):
                continue
            s, tags = score(body)
            if s < min_score:
                continue
            toks = content_tokens(body)
            if len(toks) < 2:
                continue
            stats["hits"] += 1
            items.append({
                "project": project_label or project_dir,
                "project_dir": project_dir,
                "session": session.name,
                "citation": f"{session}:{lineno}",
                "timestamp": ts.isoformat() if ts else None,
                "score": round(s, 2),
                "tags": sorted(tags),
                "text": body if len(body) <= 400 else body[:400] + "…",
                "tokens": toks,
                "weighted": s * recency_weight(ts, now, half_life),
            })
        stats["projects"].add(project_label or project_dir)
        if verbose:
            print(f"  scanned {session}", file=sys.stderr)
    return items, stats


def build_report(clusters: list[dict], min_projects: int, top: int) -> list[dict]:
    ranked = []
    for c in clusters:
        projects = {i["project"] for i in c["items"]}
        if len(projects) < min_projects:
            continue
        stamps = sorted(i["timestamp"] for i in c["items"] if i["timestamp"])
        top_tokens = sorted(
            c["centroid"],
            key=lambda t: -sum(1 for i in c["items"] if t in i["tokens"]),
        )[:6]
        ranked.append({
            "theme_tokens": top_tokens,
            "project_count": len(projects),
            "projects": sorted(projects),
            "occurrences": len(c["items"]),
            "recency_weighted_score": round(sum(i["weighted"] for i in c["items"]), 2),
            "first_seen": stamps[0] if stamps else None,
            "last_seen": stamps[-1] if stamps else None,
            "tags": sorted({t for i in c["items"] for t in i["tags"]}),
            "evidence": [
                {"citation": i["citation"], "project": i["project"],
                 "timestamp": i["timestamp"], "text": i["text"]}
                for i in sorted(c["items"], key=lambda i: -i["weighted"])[:4]
            ],
        })
    ranked.sort(key=lambda r: (-r["project_count"], -r["recency_weighted_score"]))
    return ranked[:top]


REDUCE_PROMPT = """\
# Reduce prompt — paste into Claude Code alongside candidates.json

You are drafting candidate lines for my **universal** `~/.claude/CLAUDE.md`.

Input: clusters of past corrections I gave you, mined from my own transcripts.
Each cluster has a project count, a recency window, and citations.

Rules for your output:

1. Propose at most 10 lines. Context is zero-sum; a longer file lowers adherence.
2. Only propose a rule if `project_count >= 2`. One project means it belongs in
   that repo's own CLAUDE.md, not the global one.
3. Every proposed line must cite the specific `path:line` evidence it came from.
   If you cannot cite it, do not propose it — you are confabulating.
4. Reject anything derivable from a codebase (directory layouts, dependency
   lists, architecture). Keep pitfalls, conventions, and rationale that differ
   from your defaults.
5. If a rule is language- or framework-specific, do NOT put it in CLAUDE.md.
   Instead propose it as `~/.claude/rules/<topic>.md` with `paths:` frontmatter
   so it only loads when I open matching files.
6. If a rule duplicates guidance already in an enabled plugin or skill, drop it.
7. Write each line concretely enough to verify: "run `pnpm test` before
   committing", not "test your changes".
8. Flag any cluster whose `last_seen` is old but whose occurrence count is high —
   that is probably a preference I have since abandoned. Ask me, don't assume.

Output a table: proposed line | destination file | project count | citations |
confidence. Then wait for me to approve line by line.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path.home() / ".claude"),
                    help="Claude Code data dir (default: ~/.claude)")
    ap.add_argument("--out", help="directory to write candidates.json + REVIEW.md")
    ap.add_argument("--min-score", type=float, default=2.0)
    ap.add_argument("--min-projects", type=int, default=2,
                    help="universality bar: distinct projects a rule must appear in")
    ap.add_argument("--max-chars", type=int, default=600,
                    help="ignore user turns longer than this (specs, pasted logs)")
    ap.add_argument("--half-life", type=float, default=90.0,
                    help="recency half-life in days")
    ap.add_argument("--similarity", type=float, default=0.34)
    ap.add_argument("--min-shared", type=int, default=2)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--memory-only", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not (root / "projects").is_dir():
        print(f"error: no projects/ under {root}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)

    # --- auto memory (survives the retention sweep; use as hypotheses) ------- #
    mem = read_memory(root)
    mem_clusters = cluster(
        [{**m, "weighted": 1.0} for m in mem], args.similarity, args.min_shared
    )
    mem_report = []
    for c in mem_clusters:
        projects = {i["project"] for i in c["items"]}
        if len(projects) < args.min_projects:
            continue
        mem_report.append({
            "theme_tokens": sorted(c["centroid"])[:6],
            "project_count": len(projects),
            "lines": [{"project": i["project"], "file": i["file"],
                       "modified": i["modified"], "text": i["text"]}
                      for i in c["items"][:4]],
        })
    mem_report.sort(key=lambda r: -r["project_count"])

    report: list[dict] = []
    stats = {"sessions": 0, "user_turns": 0, "projects": set(), "hits": 0}
    if not args.memory_only:
        items, stats = collect_transcripts(root, args.min_score, args.max_chars,
                                           now, args.half_life, args.verbose)
        report = build_report(cluster(items, args.similarity, args.min_shared),
                              args.min_projects, args.top)

    # --- console summary ---------------------------------------------------- #
    print(f"scanned {stats['sessions']} sessions / {stats['user_turns']} user turns "
          f"across {len(stats['projects'])} projects")
    print(f"correction turns above threshold: {stats['hits']}")
    print(f"cross-project transcript clusters (>= {args.min_projects} projects): "
          f"{len(report)}")
    print(f"cross-project auto-memory clusters: {len(mem_report)}\n")

    for i, r in enumerate(report, 1):
        print(f"{i}. [{r['project_count']} projects, {r['occurrences']}x, "
              f"score {r['recency_weighted_score']}] {' '.join(r['theme_tokens'])}")
        print(f"   tags: {', '.join(r['tags'])}  window: "
              f"{(r['first_seen'] or '?')[:10]} → {(r['last_seen'] or '?')[:10]}")
        for e in r["evidence"][:2]:
            print(f"   · {e['text'][:110]}")
            print(f"     {e['citation']}")
        print()

    if mem_report:
        print("--- auto memory, recurring across repos ---")
        for r in mem_report[:10]:
            print(f"[{r['project_count']} repos] {' '.join(r['theme_tokens'])}")
            for l in r["lines"][:2]:
                print(f"   · {l['text'][:110]}   ({l['modified'] or 'no stamp'})")
        print()

    if args.out:
        out = Path(args.out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated": now.isoformat(),
            "root": str(root),
            "params": {k: v for k, v in vars(args).items() if k != "out"},
            "stats": {**{k: v for k, v in stats.items() if k != "projects"},
                      "projects": sorted(stats["projects"])},
            "transcript_clusters": report,
            "memory_clusters": mem_report,
        }
        (out / "candidates.json").write_text(json.dumps(payload, indent=2),
                                             encoding="utf-8")
        (out / "REDUCE_PROMPT.md").write_text(REDUCE_PROMPT, encoding="utf-8")
        print(f"wrote {out / 'candidates.json'} and {out / 'REDUCE_PROMPT.md'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
