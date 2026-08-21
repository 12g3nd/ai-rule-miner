import sys
from pathlib import Path
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP

# Add current directory to path so we can import from mine_corrections
sys.path.insert(0, str(Path(__file__).parent))

from mine_corrections import (
    get_provider, 
    read_memory, 
    collect_transcripts, 
    cluster, 
    build_report,
    load_config,
    resolve_config_path,
    get_default_save_path,
    PATTERNS
)

# Initialize FastMCP server
mcp = FastMCP("ai-rule-miner")

def get_config_and_provider():
    """Helper to load config and initialize provider."""
    config_path = resolve_config_path()
            
    half_life = 90.0
    if config_path:
        config_data = load_config(config_path)
        if config_data:
            if "retention_window_days" in config_data:
                half_life = float(config_data["retention_window_days"])
            elif "half_life" in config_data:
                half_life = float(config_data["half_life"])

    provider = get_provider("claude")
    root = provider.get_default_root().expanduser()
    return root, provider, half_life

@mcp.resource("rules://global")
def get_global_rules() -> str:
    """Provides dynamically mined rules across all projects."""
    root, provider, half_life = get_config_and_provider()
    now = datetime.now(timezone.utc)
    
    if not (root / "projects").is_dir():
        return "Error: No projects directory found."

    # Mine transcripts
    items, stats = collect_transcripts(
        provider, 
        root, 
        min_score=1.0, 
        max_chars=1200,
        now=now, 
        half_life=half_life, 
        verbose=False
    )
    
    # Cluster and build report (min 2 projects for global)
    report = build_report(cluster(items, similarity=0.34, min_shared=2), min_projects=2, top_k=25)
    
    # Format into markdown
    lines = ["# Mined Global Rules", "These rules were dynamically extracted from your recent corrections.", ""]
    
    for i, r in enumerate(report, 1):
        lines.append(f"## {i}. {' '.join(r['theme_tokens'])}")
        lines.append(f"**Confidence:** {r['recency_weighted_score']:.2f} (Appears in {r['project_count']} projects)")
        lines.append("### Evidence:")
        for e in r["evidence"][:3]:
            lines.append(f"- > {e['text']}")
        lines.append("")
        
    return "\n".join(lines)

@mcp.tool()
def get_stopwords() -> str:
    """Returns the current list of stop words."""
    from mine_corrections import STOPWORDS
    get_config_and_provider()
    return f"Current Stop Words:\n{', '.join(sorted(list(STOPWORDS)))}"

@mcp.tool()
def add_stopword(word: str) -> str:
    """Adds a new stop word to the configuration."""
    from mine_corrections import add_stopword_to_config
    
    config_path = resolve_config_path() or get_default_save_path()
    word = word.strip().lower()
    success = add_stopword_to_config(config_path, word)
    
    if success:
        return f"Successfully added '{word}' to {config_path}."
    else:
        return f"'{word}' is already in the stop words list."

@mcp.tool()
def remove_stopword(word: str) -> str:
    """Removes a stop word from the configuration."""
    from mine_corrections import remove_stopword_from_config
    
    config_path = resolve_config_path() or get_default_save_path()
    word = word.strip().lower()
    success = remove_stopword_from_config(config_path, word)
    
    if success:
        return f"Successfully removed '{word}' from {config_path}."
    else:
        return f"'{word}' was not found in the stop words list."

@mcp.tool()
def get_patterns() -> str:
    """Returns the current list of scoring patterns/weights."""
    get_config_and_provider()
    lines = ["Current Scoring Patterns (Regex | Weight | Tag):"]
    for p, w, t in PATTERNS:
        lines.append(f"- `{p}` | {w} | {t}")
    return "\n".join(lines)

@mcp.tool()
def add_pattern(pattern: str, weight: float, tag: str) -> str:
    """Adds a new regex pattern with a weight and tag to the miner configuration."""
    from mine_corrections import add_pattern_to_config
    
    config_path = resolve_config_path() or get_default_save_path()
    success = add_pattern_to_config(config_path, pattern, weight, tag)
    
    if success:
        return f"Successfully added pattern '{pattern}' to {config_path}."
    else:
        return f"Pattern '{pattern}' already exists in configuration."

@mcp.tool()
def analyze_recent_corrections(project_name: str) -> str:
    """
    Mine the most recent interactions for a specific project to check for new rules.
    Use this to double-check if the user has recently corrected you in this project.
    """
    root, provider, half_life = get_config_and_provider()
    now = datetime.now(timezone.utc)
    
    items, stats = collect_transcripts(
        provider, root, min_score=1.0, max_chars=1200, now=now, half_life=half_life, verbose=False
    )
    
    # Filter for specific project
    project_items = [item for item in items if project_name.lower() in item['project'].lower()]
    
    if not project_items:
        return f"No recent corrections found for project matching '{project_name}'."
        
    project_items.sort(key=lambda x: x['timestamp'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    
    lines = [f"# Recent Corrections for {project_name}", ""]
    for item in project_items[:10]: # Return top 10 most recent
        date_str = item['timestamp'].strftime("%Y-%m-%d %H:%M") if item['timestamp'] else "Unknown Date"
        lines.append(f"**[{date_str}]** ({item['score']:.1f} weight) - Tag: {item['tag']}")
        lines.append(f"> {item['text']}")
        lines.append("")
        
    return "\n".join(lines)

def main():
    # Run the MCP server
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
