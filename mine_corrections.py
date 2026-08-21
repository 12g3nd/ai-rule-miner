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
    # --- repetition: strongest signal, a rule that failed to persist ----- #
    (r"\bi (?:already )?told you\b",                    3.0, "repetition"),
    (r"\b(?:from now on|going forward)\b",              2.8, "repetition"),
    (r"\bevery (?:time|single time)\b",                 2.2, "repetition"),
    (r"\b(?:i said|as i said|like i said)\b",           2.5, "repetition"),
    (r"\bagain\b",                                      1.0, "repetition"),
    # --- stated preference: how delegators express rules ----------------- #
    (r"\bi don'?t want\b",                              2.4, "preference"),
    (r"\bi (?:just )?want (?:you to |it to |to see )?",  1.4, "preference"),
    (r"\bi (?:like|prefer) (?:it |for it |that )?",      2.2, "preference"),
    (r"\bdon'?t (?:give|show|include|add|send) me\b",    2.2, "preference"),
    (r"\bno (?:next steps|preamble|fluff|filler|summary)\b", 2.8, "preference"),
    (r"\b(?:super |keep it |be )brief\b",               2.0, "preference"),
    (r"\bseems? (?:un+ecc?ess?ary|redundant|excessive|too long|tedious)\b",
                                                        2.0, "preference"),
    (r"\bin (?:simplified|simpler|plain) terms\b",      1.8, "preference"),
    (r"\bkeep it \S+",                                  1.6, "preference"),
    (r"\bmake sure\b",                                  1.2, "preference"),
    (r"\bunless there'?s\b",                            1.4, "preference"),
    # --- prohibition ----------------------------------------------------- #
    (r"\bstop (?:doing|adding|using|trying|it)\b",      2.5, "prohibition"),
    (r"\bstop\b",                                       1.4, "prohibition"),
    (r"\bnever (?:use|do|add|write|touch)\b",           2.5, "prohibition"),
    (r"\b(?:don'?t|do not) (?:use|add|write|create|touch|change|refactor|bother)\b",
                                                        2.0, "prohibition"),
    # --- convention ------------------------------------------------------ #
    (r"\bwe (?:use|prefer|don'?t use|never use|always)\b", 2.0, "convention"),
    (r"\buse \S+ (?:not|instead of|over|rather than) \S+", 2.0, "convention"),
    (r"\b(?:instead of|rather than)\b",                 1.0, "convention"),
    # --- correction ------------------------------------------------------ #
    (r"^(?:no|nope|nah)\b",                             2.0, "correction"),
    (r"\bactually\b",                                   0.8, "correction"),
    (r"\bthat'?s (?:not|wrong|incorrect|still)\b",      1.6, "correction"),
    (r"\bwhy (?:did|are|would|is) (?:you|it)\b",        1.5, "correction"),
    (r"\b(?:revert|undo|roll back)\b",                  1.5, "correction"),
    (r"\bremember\b",                                   1.5, "directive"),
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
# Configuration Loading
# --------------------------------------------------------------------------- #

def resolve_config_path(override: str | None = None) -> Path | None:
    """Find the most appropriate config file (local first, then global fallback)."""
    if override:
        return Path(override)
    for p in [Path("config.yaml"), Path("config.json"), Path.home() / ".claude" / "miner_config.json"]:
        if p.exists():
            return p
    return None

def get_default_save_path() -> Path:
    """Returns the default global location for saving config."""
    global_dir = Path.home() / ".claude"
    global_dir.mkdir(parents=True, exist_ok=True)
    return global_dir / "miner_config.json"

def load_config(config_path: Path) -> dict:
    """Load overrides from a JSON or YAML config file."""
    global PATTERNS, COMPILED, STOPWORDS
    
    if not config_path.is_file():
        return {}
        
    try:
        with config_path.open("r", encoding="utf-8") as f:
            if config_path.suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    data = yaml.safe_load(f)
                except ImportError:
                    print(f"Warning: PyYAML not installed. Cannot parse {config_path.name}.", file=sys.stderr)
                    return {}
            else:
                data = json.load(f)
                
        if not isinstance(data, dict):
            return {}
            
        if "stop_words" in data:
            if isinstance(data["stop_words"], list):
                STOPWORDS = set(data["stop_words"])
            elif isinstance(data["stop_words"], str):
                STOPWORDS = set(data["stop_words"].split())
                
        if "scoring_weights" in data or "patterns" in data:
            new_patterns = data.get("scoring_weights", data.get("patterns"))
            if isinstance(new_patterns, list):
                PATTERNS = []
                for p in new_patterns:
                    if isinstance(p, dict) and "pattern" in p and "weight" in p and "tag" in p:
                        PATTERNS.append((p["pattern"], float(p["weight"]), p["tag"]))
                    elif isinstance(p, list) and len(p) >= 3:
                        PATTERNS.append((p[0], float(p[1]), p[2]))
                COMPILED = [(re.compile(p, re.I), w, tag) for p, w, tag in PATTERNS]
                
        return data
    except Exception as e:
        print(f"Error loading config {config_path}: {e}", file=sys.stderr)
        return {}

def save_config(config_path: Path, data: dict):
    """Save configuration to JSON or YAML."""
    try:
        with config_path.open("w", encoding="utf-8") as f:
            if config_path.suffix in (".yaml", ".yml"):
                import yaml
                yaml.dump(data, f, default_flow_style=False)
            else:
                json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving config {config_path}: {e}", file=sys.stderr)

def add_stopword_to_config(config_path: Path, word: str):
    """Adds a stop word to the config file directly."""
    data = {}
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                if config_path.suffix in (".yaml", ".yml"):
                    import yaml
                    data = yaml.safe_load(f) or {}
                else:
                    data = json.load(f)
        except Exception:
            pass
            
    if not isinstance(data, dict):
        data = {}
        
    current = data.get("stop_words", list(STOPWORDS))
    if isinstance(current, str):
        current = current.split()
    elif isinstance(current, set):
        current = list(current)
        
    if word not in current:
        current.append(word)
        data["stop_words"] = sorted(list(set(current)))
        save_config(config_path, data)
        return True
    return False

def remove_stopword_from_config(config_path: Path, word: str):
    """Removes a stop word from the config file directly."""
    data = {}
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                if config_path.suffix in (".yaml", ".yml"):
                    import yaml
                    data = yaml.safe_load(f) or {}
                else:
                    data = json.load(f)
        except Exception:
            pass
            
    if not isinstance(data, dict):
        data = {}
        
    current = data.get("stop_words", list(STOPWORDS))
    if isinstance(current, str):
        current = current.split()
    elif isinstance(current, set):
        current = list(current)
        
    if word in current:
        current.remove(word)
        data["stop_words"] = sorted(list(set(current)))
        save_config(config_path, data)
        return True
    return False

def add_pattern_to_config(config_path: Path, pattern: str, weight: float, tag: str):
    """Adds a new scoring pattern to the config file directly."""
    data = {}
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                if config_path.suffix in (".yaml", ".yml"):
                    import yaml
                    data = yaml.safe_load(f) or {}
                else:
                    data = json.load(f)
        except Exception:
            pass
            
    if not isinstance(data, dict):
        data = {}
        
    current = data.get("patterns", [{"pattern": p, "weight": w, "tag": t} for p, w, t in PATTERNS])
    
    # Check if pattern already exists
    for p in current:
        if p.get("pattern") == pattern:
            return False
            
    current.append({"pattern": pattern, "weight": weight, "tag": tag})
    data["patterns"] = current
    save_config(config_path, data)
    return True

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


class HistoryProvider:
    """Base class for different AI tool history parsers."""
    
    @classmethod
    def get_default_root(cls) -> Path:
        raise NotImplementedError

    def get_sessions(self, root: Path) -> list[Path]:
        raise NotImplementedError
        
    def iter_user_turns(self, session_path: Path):
        """Yield (line_no, text, timestamp, cwd) for genuine user turns."""
        raise NotImplementedError


class ClaudeProvider(HistoryProvider):
    @classmethod
    def get_default_root(cls) -> Path:
        return Path.home() / ".claude"

    def get_sessions(self, root: Path) -> list[Path]:
        return sorted(root.glob("projects/*/*.jsonl"))

    def iter_user_turns(self, path: Path):
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


class CodexProvider(HistoryProvider):
    @classmethod
    def get_default_root(cls) -> Path:
        return Path.home() / ".codex"

    def get_sessions(self, root: Path) -> list[Path]:
        return sorted(root.glob("sessions/*/*/*/*.jsonl"))

    def iter_user_turns(self, path: Path):
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            return
        
        current_cwd = None
        with fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                    
                rec_type = rec.get("type")
                
                if rec_type == "turn_context":
                    payload = rec.get("payload", {})
                    current_cwd = payload.get("cwd", current_cwd)
                    continue
                    
                if rec_type == "event_msg":
                    payload = rec.get("payload", {})
                    if payload.get("type") == "user_message":
                        text = payload.get("message")
                        if not text or is_noise(text):
                            continue
                        yield lineno, text, parse_ts(rec.get("timestamp")), current_cwd


class CursorProvider(HistoryProvider):
    @classmethod
    def get_default_root(cls) -> Path:
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "workspaceStorage"
        elif sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
        return Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage"

    def get_sessions(self, root: Path) -> list[Path]:
        print("Note: Cursor support is currently a stub. Implementing SQLite parsing soon.", file=sys.stderr)
        return []

    def iter_user_turns(self, path: Path):
        yield from []


class WindsurfProvider(HistoryProvider):
    @classmethod
    def get_default_root(cls) -> Path:
        return Path.home() / ".codeium" / "windsurf"

    def get_sessions(self, root: Path) -> list[Path]:
        print("Note: Windsurf support is currently a stub.", file=sys.stderr)
        return []

    def iter_user_turns(self, path: Path):
        yield from []


def get_provider(name: str) -> HistoryProvider:
    providers = {
        "claude": ClaudeProvider(),
        "codex": CodexProvider(),
        "cursor": CursorProvider(),
        "windsurf": WindsurfProvider()
    }
    return providers.get(name.lower(), ClaudeProvider())


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

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal indent-aware YAML frontmatter reader (nested keys flattened)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip("\"'")
            if val:                 # skip bare parent keys such as "metadata:"
                fields[key] = val
    return fields, body


def read_memory(root: Path, include_project: bool = False) -> list[dict]:
    """Read auto-memory nodes, split by their declared `type`.

    Nodes tagged `type: feedback` are preferences — the only ones that belong in
    a universal CLAUDE.md. Nodes tagged `type: project` are state logs (build
    history, fixes, architecture); excluded by default, since that content is
    derivable from the codebase and would only waste context.
    """
    out = []
    for mem_dir in sorted(root.glob("projects/*/memory")):
        project = mem_dir.parent.name
        for md in sorted(mem_dir.rglob("*.md")):
            try:
                raw = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = parse_frontmatter(raw)
            node_type = (fm.get("type") or "").lower()
            if node_type != "feedback" and not include_project:
                continue
            body_clean = "\n".join(l.rstrip() for l in body.splitlines()
                                   if l.strip()).strip()
            out.append({
                "project": project,
                "file": str(md),
                "name": fm.get("name") or md.stem,
                "description": fm.get("description", ""),
                "node_type": node_type or "untyped",
                "modified": fm.get("modified"),
                "text": body_clean[:800],
                "tokens": content_tokens(
                    (fm.get("name", "") + " " + fm.get("description", "") + " "
                     + body_clean[:800]).replace("-", " ")),
            })
    return out


def verbatim_repeats(items: list[dict], min_sessions: int = 2) -> list[dict]:
    """Identical turns across separate sessions: the strongest possible signal."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        key = re.sub(r"\s+", " ", it["text"].lower()).strip()[:200]
        groups[key].append(it)
    out = []
    for group in groups.values():
        if len({g["session"] for g in group}) < min_sessions:
            continue
        out.append({
            "text": group[0]["text"],
            "times": len(group),
            "sessions": len({g["session"] for g in group}),
            "projects": sorted({g["project"] for g in group}),
            "citations": [g["citation"] for g in group[:4]],
        })
    out.sort(key=lambda r: -r["times"])
    return out


def project_independence_warning(projects) -> list:
    """Flag project pairs that look like the same work under two folder names.

    Cross-project recurrence is only evidence if the projects are independent.
    """
    labels = sorted(projects)
    pairs = []
    for i, a in enumerate(labels):
        ta = content_tokens(a.replace("-", " ").replace("/", " ").lower())
        for b in labels[i + 1:]:
            tb = content_tokens(b.replace("-", " ").replace("/", " ").lower())
            if not ta or not tb:
                continue
            if len(ta & tb) / max(len(ta | tb), 1) >= 0.4:
                pairs.append((a, b))
    return pairs


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def collect_transcripts(provider: HistoryProvider, root: Path, min_score: float, max_chars: int, now: datetime,
                        half_life: float, verbose: bool) -> tuple[list[dict], dict]:
    items: list[dict] = []
    stats = {"sessions": 0, "user_turns": 0, "projects": set(), "hits": 0}

    for session in provider.get_sessions(root):
        stats["sessions"] += 1
        project_dir = session.parent.name
        project_label = None
        for lineno, text, ts, cwd in provider.iter_user_turns(session):
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
    ap.add_argument("--config", help="Path to config.json or config.yaml")
    ap.add_argument("--tool", default="claude", choices=["claude", "codex", "cursor", "windsurf"],
                    help="Which AI assistant tool to mine history for (default: claude)")
    ap.add_argument("--root", default=None,
                    help="Data dir (default: depends on tool, e.g. ~/.claude)")
    ap.add_argument("--out", help="directory to write candidates.json + REVIEW.md")
    ap.add_argument("--min-score", type=float, default=1.0)
    ap.add_argument("--min-projects", type=int, default=2,
                    help="universality bar: distinct projects a rule must appear in")
    ap.add_argument("--max-chars", type=int, default=1200,
                    help="ignore user turns longer than this (specs, pasted logs)")
    ap.add_argument("--half-life", type=float, default=90.0,
                    help="recency half-life in days")
    ap.add_argument("--similarity", type=float, default=0.34)
    ap.add_argument("--min-shared", type=int, default=2)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--memory-only", action="store_true")
    ap.add_argument("--include-project-memory", action="store_true",
                    help="also read type: project memory (state logs)")
    ap.add_argument("--show-stopwords", action="store_true", help="Print the current list of stop words and exit")
    ap.add_argument("--add-stopword", type=str, metavar="WORD", help="Add a custom stop word to the config file and exit")
    ap.add_argument("--remove-stopword", type=str, metavar="WORD", help="Remove a stop word from the config file and exit")
    ap.add_argument("--init-config", action="store_true", help="Generate a default config file (miner_config.json) in ~/.claude/ and exit")
    ap.add_argument("-v", "--verbose", action="store_true")

    args, _ = ap.parse_known_args()
    config_path = resolve_config_path(args.config)
                
    if config_path:
        config_data = load_config(config_path)
        if config_data:
            if "retention_window_days" in config_data:
                ap.set_defaults(half_life=float(config_data["retention_window_days"]))
            elif "half_life" in config_data:
                ap.set_defaults(half_life=float(config_data["half_life"]))
                
    args = ap.parse_args()

    if args.init_config:
        target = get_default_save_path()
        save_config(target, {
            "retention_window_days": 90.0,
            "stop_words": sorted(list(STOPWORDS)),
            "patterns": [{"pattern": p, "weight": w, "tag": t} for p, w, t in PATTERNS]
        })
        print(f"Initialized default configuration at {target}")
        return 0

    # Handle stop word management
    if args.show_stopwords:
        print("Current Stop Words:")
        print(", ".join(sorted(list(STOPWORDS))))
        return 0
        
    if args.add_stopword:
        target_conf = config_path if config_path else get_default_save_path()
        word = args.add_stopword.strip().lower()
        if add_stopword_to_config(target_conf, word):
            print(f"Successfully added '{word}' to stop words in {target_conf}.")
        else:
            print(f"'{word}' is already in the stop words list.")
        return 0
        
    if args.remove_stopword:
        target_conf = config_path if config_path else get_default_save_path()
        word = args.remove_stopword.strip().lower()
        if remove_stopword_from_config(target_conf, word):
            print(f"Successfully removed '{word}' from stop words in {target_conf}.")
        else:
            print(f"'{word}' was not found in the stop words list.")
        return 0

    provider = get_provider(args.tool)
    root_str = args.root if args.root is not None else str(provider.get_default_root())
    root = Path(root_str).expanduser()
    
    if args.tool == "claude" and not (root / "projects").is_dir():
        print(f"error: no projects/ under {root}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)

    # --- auto memory (survives the retention sweep; use as hypotheses) ------- #
    mem = read_memory(root, include_project=args.include_project_memory)
    mem_clusters = cluster(
        [{**m, "weighted": 1.0} for m in mem], args.similarity, args.min_shared
    )
    feedback_nodes = [m for m in mem if m["node_type"] == "feedback"]
    by_name: dict[str, list[dict]] = defaultdict(list)
    for m in feedback_nodes:
        by_name[m["name"]].append(m)

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
        items, stats = collect_transcripts(provider, root, args.min_score, args.max_chars,
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

    if not args.memory_only:
        reps = verbatim_repeats(items)
        if reps:
            print("--- VERBATIM REPEATS across sessions (strongest signal) ---")
            for r in reps[:8]:
                print(f"[{r['times']}x, {r['sessions']} sessions, "
                      f"{len(r['projects'])} project(s)] "
                      f"{' '.join(r['text'].split())[:120]}")
                print(f"   {r['citations'][0]}")
            print()

    for i, r in enumerate(report, 1):
        print(f"{i}. [{r['project_count']} projects, {r['occurrences']}x, "
              f"score {r['recency_weighted_score']}] {' '.join(r['theme_tokens'])}")
        print(f"   tags: {', '.join(r['tags'])}  window: "
              f"{(r['first_seen'] or '?')[:10]} → {(r['last_seen'] or '?')[:10]}")
        for e in r["evidence"][:2]:
            print(f"   · {e['text'][:110]}")
            print(f"     {e['citation']}")
        print()

    if feedback_nodes:
        print("--- PREFERENCE NODES (type: feedback) — verbatim, highest value ---")
        for name, group in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
            repos = sorted({g["project"] for g in group})
            stamp = next((g["modified"] for g in group if g["modified"]), "no stamp")
            print(f"[{len(repos)} repo(s)] {name}   (modified {stamp})")
            if group[0]["description"]:
                print(f"   desc: {group[0]['description'][:140]}")
            for line in group[0]["text"].splitlines()[:4]:
                print(f"   . {line[:130]}")
        print()
    else:
        print("--- no type: feedback memory nodes found ---")
        print("    Preferences you state are saved as feedback nodes. If there")
        print("    are none, say 'remember that ...' when you correct Claude.\n")

    warn = project_independence_warning(stats["projects"])
    if warn:
        print("--- WARNING: these project labels look like the same work ---")
        for a, b in warn[:6]:
            print(f"   {a}\n   {b}")
        print("    Cross-project counts across these are NOT evidence of a")
        print("    universal rule. Treat them as one project.\n")

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
            "verbatim_repeats": (verbatim_repeats(items)
                                 if not args.memory_only else []),
            "preference_nodes": [{k: v for k, v in m.items()
                                  if k != "tokens"}
                                 for m in feedback_nodes],
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
