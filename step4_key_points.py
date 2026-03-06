"""
Step 4 — Key Points Generation

For each note where the Key Points section is empty, calls the Claude API to
generate 3-5 concise bullet points. Abstract source priority:
  1. Zotero abstractNote field
  2. OpenAlex (fetched by DOI from the note) — fallback when Zotero has none

Notes without an abstract in either source are skipped.

Usage:
    python step4_key_points.py [--dry-run] [--verbose]

    --dry-run   Show which notes would be updated without calling the API
    --verbose   Show debug output on console

Backs up each note before writing to {output_folder}/.backup/
Notes that already have user-written Key Points are never touched.
"""

import argparse
import json
import logging
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Config / logging
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "obsidian_writer.log"

STEP4_REQUIRED_KEYS = [
    "zotero_user_id",
    "zotero_api_key",
    "zotero_library_type",
    "output_folder",
    "anthropic_api_key",
]

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

OPENALEX_BASE = "https://api.openalex.org/works"
OPENALEX_MAILTO = "anand.guitarist@gmail.com"
OPENALEX_SLEEP = 0.15


def setup_logging(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("step4")
    log.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))

    log.addHandler(console)
    log.addHandler(fh)
    return log


def load_config(log: logging.Logger) -> dict:
    config_path = PROJECT_DIR / "config.json"
    if not config_path.exists():
        log.error("config.json not found. Copy config.json.template and fill in credentials.")
        sys.exit(1)
    with open(config_path) as f:
        config = json.load(f)
    missing = [k for k in STEP4_REQUIRED_KEYS if not config.get(k)]
    if missing:
        log.error("config.json missing required field(s): %s", ", ".join(missing))
        sys.exit(1)
    return config


def connect_zotero(config: dict, log: logging.Logger):
    try:
        from pyzotero import zotero
    except ImportError:
        log.error("pyzotero is not installed. Run: pip install pyzotero")
        sys.exit(1)
    return zotero.Zotero(
        library_id=config["zotero_user_id"],
        library_type=config["zotero_library_type"],
        api_key=config["zotero_api_key"],
    )


def connect_claude(config: dict, log: logging.Logger):
    try:
        import anthropic
    except ImportError:
        log.error("anthropic is not installed. Run: pip install anthropic")
        sys.exit(1)
    return anthropic.Anthropic(api_key=config["anthropic_api_key"])


# ---------------------------------------------------------------------------
# OpenAlex abstract fallback
# ---------------------------------------------------------------------------

_DOI_LINE_RE = re.compile(r'\*\*DOI\*\*:.*\[([^\]]+)\]')


def _reconstruct_abstract(inverted_index: dict) -> str:
    """Reconstruct plain text from OpenAlex abstract_inverted_index format.

    The inverted index maps {word: [position, ...]}. We reverse it to get
    ordered words, then join them.
    """
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions.append((pos, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def fetch_abstract_from_openalex(doi: str, log: logging.Logger) -> str | None:
    """Fetch abstract from OpenAlex by DOI. Returns plain text or None."""
    import urllib.parse
    encoded_doi = urllib.parse.quote(f"https://doi.org/{doi}", safe=":/.")
    url = (f"{OPENALEX_BASE}/{encoded_doi}"
           f"?select=abstract_inverted_index&mailto={OPENALEX_MAILTO}")
    headers = {
        "Accept": "application/json",
        "User-Agent": f"ObsidianWriter/1.0 (mailto:{OPENALEX_MAILTO})",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.debug("OpenAlex: no record for DOI %s", doi)
        else:
            log.warning("OpenAlex HTTP %d for DOI %s", e.code, doi)
        return None
    except Exception as exc:
        log.warning("OpenAlex request error for DOI %s: %s", doi, exc)
        return None

    abstract = _reconstruct_abstract(data.get("abstract_inverted_index") or {})
    return abstract if abstract else None


# ---------------------------------------------------------------------------
# Zotero data helpers
# ---------------------------------------------------------------------------

def fetch_all_items(zot, log: logging.Logger) -> tuple[dict, dict]:
    """Return (items_by_title_norm, items_by_key). Fetches all content items."""
    log.info("Fetching all items from Zotero...")
    try:
        all_items = zot.everything(zot.items())
    except Exception as exc:
        log.error("Failed to fetch items from Zotero: %s", exc)
        sys.exit(1)

    items_by_title: dict[str, dict] = {}
    items_by_key: dict[str, dict] = {}

    for item in all_items:
        data = item.get("data", {})
        if data.get("itemType") in ("attachment", "note"):
            continue
        title = data.get("title", "").strip()
        if title:
            norm = _normalise_title(title)
            items_by_title[norm] = data
        key = item.get("key", "")
        if key:
            items_by_key[key] = data

    log.info("Loaded %d content items from Zotero", len(items_by_title))
    return items_by_title, items_by_key


def _normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def _format_authors(creators: list) -> str:
    parts = []
    for c in creators:
        if c.get("name"):
            parts.append(c["name"])
        else:
            first = c.get("firstName", "").strip()
            last = c.get("lastName", "").strip()
            if first and last:
                parts.append(f"{first} {last}")
            elif last:
                parts.append(last)
    return ", ".join(parts)


def build_research_context(items_by_title: dict) -> str:
    """Build a short context string from the user's own papers."""
    own_journals: list[str] = []
    own_keywords: list[str] = []

    for data in items_by_title.values():
        creators = data.get("creators", [])
        is_own = any(
            c.get("lastName", "").lower() == "shrivastava"
            and c.get("firstName", "").lower().startswith("anand")
            for c in creators
        )
        if not is_own:
            continue
        journal = data.get("publicationTitle", "").strip()
        if journal:
            own_journals.append(journal)
        title = data.get("title", "")
        # Extract rough keywords from title (just show title fragments)
        own_keywords.append(title[:60])

    if not own_journals and not own_keywords:
        return ""

    journals_str = ", ".join(dict.fromkeys(own_journals))  # deduplicated, order preserved
    return (
        f"The researcher works in labour economics, economic history, networks in economics, "
        f"and experimental economics. Their own papers appear in: {journals_str}."
        if journals_str
        else "The researcher works in labour economics, economic history, networks in economics, and experimental economics."
    )


# ---------------------------------------------------------------------------
# Note parsing
# ---------------------------------------------------------------------------

_KEY_POINTS_RE = re.compile(r'^## Key Points\s*$', re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r'^## ', re.MULTILINE)


def _is_key_points_empty(content: str) -> tuple[bool, int, int]:
    """Check whether Key Points section is empty (only '- ' placeholder or blank).

    Returns:
        (is_empty, section_start, body_end)
        section_start / body_end are character offsets in content.
    """
    m = _KEY_POINTS_RE.search(content)
    if not m:
        return False, -1, -1

    section_start = m.start()
    body_start = m.end()

    rest = content[body_start:]
    next_m = _NEXT_SECTION_RE.search(rest)
    if next_m:
        body_end = body_start + next_m.start()
    else:
        body_end = len(content)

    body = content[body_start:body_end].strip()

    # Empty if nothing there, or only a bare "- " placeholder
    is_empty = (body == "" or body == "-" or body == "- ")
    return is_empty, section_start, body_end


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_note(filepath: Path, output_folder: Path, log: logging.Logger) -> None:
    backup_dir = output_folder / ".backup"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{filepath.stem}.{ts}.md"
    shutil.copy2(filepath, backup_path)
    log.debug("Backed up %s → %s", filepath.name, backup_path.name)


# ---------------------------------------------------------------------------
# Claude prompt and API call
# ---------------------------------------------------------------------------

def _build_prompt(data: dict, abstract: str, research_context: str, cites: list[str], cited_by: list[str]) -> str:
    title = data.get("title", "Unknown")
    year = data.get("date", "")[:4] or "?"
    journal = data.get("publicationTitle", "") or data.get("bookTitle", "") or ""
    authors = _format_authors(data.get("creators", []))

    cites_str = "\n".join(f"  - {t}" for t in cites) if cites else "  none identified"
    cited_by_str = "\n".join(f"  - {t}" for t in cited_by) if cited_by else "  none identified"

    context_line = f"\n{research_context}\n" if research_context else ""

    return f"""You are helping an economist organize research notes. Write from the perspective of what matters for someone working in labour economics, economic history, networks in economics, and experimental economics.
{context_line}
Paper: {title} ({year}){f', {journal}' if journal else ''}
Authors: {authors}
Abstract: {abstract}

Papers in the researcher's library that this paper cites:
{cites_str}

Papers in the researcher's library that cite this paper:
{cited_by_str}

Write exactly 3-5 bullet points summarizing the key contributions and findings of this paper. Each bullet is one sentence starting with a capital letter. No preamble, no sub-bullets, no headers. Format as a simple list with each point on its own line starting with "- ".
"""


def generate_key_points(
    client,
    data: dict,
    abstract: str,
    research_context: str,
    model: str,
    log: logging.Logger,
    cites: list[str] | None = None,
    cited_by: list[str] | None = None,
) -> str | None:
    """Call Claude and return key points text (the bullet lines), or None on error."""
    prompt = _build_prompt(data, abstract, research_context, cites or [], cited_by or [])

    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.error("Claude API error: %s", exc)
        return None

    if response.stop_reason == "max_tokens":
        title = data.get("title", "?")
        log.warning("Response truncated (max_tokens) for %r — consider raising max_tokens", title[:60])

    text = response.content[0].text.strip()
    return text


# ---------------------------------------------------------------------------
# Detect existing Related Papers links (for context in prompt)
# ---------------------------------------------------------------------------

_RELATED_RE = re.compile(r'^## Related Papers\s*$', re.MULTILINE)
_WIKILINK_RE = re.compile(r'\[\[([^\]]+)\]\]')


def _extract_related_papers(content: str) -> tuple[list[str], list[str]]:
    """Return (cites, cited_by) — currently we just return all related links as cites.

    We can't distinguish direction from the note alone, so we return all
    related links as context for the prompt.
    """
    m = _RELATED_RE.search(content)
    if not m:
        return [], []

    body_start = m.end()
    rest = content[body_start:]
    next_m = re.compile(r'^## ', re.MULTILINE).search(rest)
    body = rest[:next_m.start()] if next_m else rest

    links = _WIKILINK_RE.findall(body)
    # Remove <!--auto--> suffix from display (already stripped since it's outside [[]])
    return links, []


# ---------------------------------------------------------------------------
# Note update
# ---------------------------------------------------------------------------

def update_note_key_points(
    filepath: Path,
    key_points_text: str,
    output_folder: Path,
    log: logging.Logger,
) -> bool:
    """Replace empty Key Points section with generated content. Returns True if written."""
    content = filepath.read_text(encoding="utf-8")
    is_empty, section_start, body_end = _is_key_points_empty(content)

    if not is_empty:
        log.warning("Key Points not empty in %s — skipping", filepath.name)
        return False

    # Find end of "## Key Points\n\n" header to insert after
    m = _KEY_POINTS_RE.search(content)
    header_end = m.end()

    # Build new section content (lines between header and next ##)
    new_body = "\n" + key_points_text + "\n\n"
    new_content = content[:header_end] + new_body + content[body_end:]

    backup_note(filepath, output_folder, log)
    filepath.write_text(new_content, encoding="utf-8")
    log.info("Key Points written for %s", filepath.stem[:60])
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Step 4 — Key points generation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without calling API")
    parser.add_argument("--verbose", action="store_true", help="Show debug output on console")
    args = parser.parse_args()

    log = setup_logging(args.verbose)
    config = load_config(log)
    output_folder = Path(config["output_folder"])
    model = config.get("claude_model", DEFAULT_MODEL)

    if not output_folder.exists():
        log.error("output_folder does not exist: %s", output_folder)
        sys.exit(1)

    zot = connect_zotero(config, log)
    client = None if args.dry_run else connect_claude(config, log)

    items_by_title, _ = fetch_all_items(zot, log)
    research_context = build_research_context(items_by_title)

    # --- Find eligible notes ---
    # eligible entries: (md_file, data, abstract, abstract_source)
    eligible: list[tuple[Path, dict, str, str]] = []
    skipped_no_abstract = 0
    skipped_has_content = 0

    for md_file in sorted(output_folder.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        is_empty, _, _ = _is_key_points_empty(content)
        if not is_empty:
            skipped_has_content += 1
            continue

        # Find matching Zotero item
        norm = _normalise_title(md_file.stem)
        data = items_by_title.get(norm)
        if data is None:
            log.debug("No Zotero match for note %r — skipping", md_file.stem[:60])
            continue

        # Abstract source 1: Zotero
        abstract = data.get("abstractNote", "").strip()
        abstract_source = "Zotero"

        # Abstract source 2: OpenAlex fallback
        if not abstract:
            doi_m = _DOI_LINE_RE.search(content)
            if doi_m:
                doi = doi_m.group(1).strip().lower()
                log.info("No Zotero abstract for %r — trying OpenAlex...", md_file.stem[:50])
                abstract = fetch_abstract_from_openalex(doi, log) or ""
                if abstract:
                    abstract_source = "OpenAlex"

        if not abstract:
            log.info("Skipping %r: no abstract in Zotero or OpenAlex", md_file.stem[:60])
            skipped_no_abstract += 1
            continue

        eligible.append((md_file, data, abstract, abstract_source))

    from_zotero = sum(1 for _, _, _, src in eligible if src == "Zotero")
    from_openalex = sum(1 for _, _, _, src in eligible if src == "OpenAlex")
    log.info("Eligible: %d  (Zotero abstract: %d, OpenAlex abstract: %d, no abstract: %d, has content: %d)",
             len(eligible), from_zotero, from_openalex, skipped_no_abstract, skipped_has_content)

    if args.dry_run:
        print()
        print(f"DRY RUN — {len(eligible)} note(s) would be updated:")
        for md_file, _, _, src in eligible:
            print(f"  - {md_file.stem[:65]}  [{src}]")
        print()
        print(f"  Abstract from Zotero:     {from_zotero}")
        print(f"  Abstract from OpenAlex:   {from_openalex}")
        print(f"  Skipped (no abstract):    {skipped_no_abstract}")
        print(f"  Skipped (has content):    {skipped_has_content}")
        return

    # --- Generate and write ---
    generated = 0
    failed = 0

    for md_file, data, abstract, abstract_source in eligible:
        content = md_file.read_text(encoding="utf-8")
        cites, cited_by = _extract_related_papers(content)

        key_points = generate_key_points(client, data, abstract, research_context, model, log,
                                         cites=cites, cited_by=cited_by)
        if key_points is None:
            log.error("Failed to generate key points for %r", md_file.stem[:60])
            failed += 1
            continue

        wrote = update_note_key_points(md_file, key_points, output_folder, log)
        if wrote:
            log.info("  [%s abstract]", abstract_source)
            generated += 1
        else:
            failed += 1

    print()
    print("=" * 50)
    print("  Step 4 done.")
    print(f"  Key Points generated: {generated:4}")
    print(f"  Failed:               {failed:4}")
    print(f"  Skipped (no abstract):{skipped_no_abstract:4}")
    print(f"  Skipped (has content):{skipped_has_content:4}")
    print("=" * 50)


if __name__ == "__main__":
    main()
