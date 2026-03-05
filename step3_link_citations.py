"""
Step 3 — Citation Linking

Queries Semantic Scholar in batch for references and citations of all papers
in your library. Matches returned papers against the library. Updates the
"Related Papers" section of each note with bidirectional links.

Auto-generated links are tagged <!--auto--> so they can be distinguished from
user-written links and updated/removed on subsequent runs.

Usage:
    python step3_link_citations.py [--full] [--verbose]

    --full      Re-query all papers (ignore s2_cache.json)
    --verbose   Show debug output on console

Backs up each note before writing to {output_folder}/.backup/
"""

import argparse
import difflib
import json
import logging
import random
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
CACHE_FILE = PROJECT_DIR / "s2_cache.json"

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_BATCH_SIZE = 500

STEP3_REQUIRED_KEYS = [
    "zotero_user_id",
    "zotero_api_key",
    "zotero_library_type",
    "output_folder",
]


def setup_logging(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("step3")
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
    missing = [k for k in STEP3_REQUIRED_KEYS if not config.get(k)]
    if missing:
        log.error("config.json missing required field(s): %s", ", ".join(missing))
        sys.exit(1)
    return config


# ---------------------------------------------------------------------------
# Library index: parse DOIs and titles from existing notes
# ---------------------------------------------------------------------------

_DOI_LINE_RE = re.compile(r'\*\*DOI\*\*:.*\[([^\]]+)\]')
_DATE_ADDED_RE = re.compile(r'\*\*Date Added\*\*:\s*(\S+)')


def _normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def build_library_index(output_folder: Path, log: logging.Logger) -> tuple[dict, dict, dict]:
    """Parse all .md notes and build lookup indexes.

    Returns:
        doi_index   — {lowercase_doi: note_stem (title without .md)}
        title_index — {normalised_title: note_stem}
        date_index  — {note_stem: date_added_str}
    """
    doi_index: dict[str, str] = {}
    title_index: dict[str, str] = {}
    date_index: dict[str, str] = {}

    for md_file in output_folder.glob("*.md"):
        stem = md_file.stem
        title_index[_normalise_title(stem)] = stem
        content = md_file.read_text(encoding="utf-8")

        m = _DOI_LINE_RE.search(content)
        if m:
            doi = m.group(1).strip().lower()
            if doi:
                doi_index[doi] = stem

        m = _DATE_ADDED_RE.search(content)
        if m:
            date_index[stem] = m.group(1)

    log.info("Library index: %d notes, %d with DOI", len(title_index), len(doi_index))
    return doi_index, title_index, date_index


# ---------------------------------------------------------------------------
# S2 cache
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Semantic Scholar API calls
# ---------------------------------------------------------------------------

def _s2_headers(api_key: str) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def _s2_request_with_backoff(
    url: str,
    data: bytes | None,
    headers: dict,
    log: logging.Logger,
    max_attempts: int = 5,
) -> dict | None:
    """POST (or GET if data is None) with exponential backoff on 429."""
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(60 * (2 ** attempt) + random.uniform(0, 1), 600)
                log.warning("S2 rate limit (429). Waiting %.0f s (attempt %d/%d)...",
                            wait, attempt + 1, max_attempts)
                time.sleep(wait)
                continue
            log.warning("S2 HTTP error %d for %s", e.code, url[:80])
            return None
        except Exception as exc:
            log.warning("S2 request error: %s", exc)
            return None
    log.warning("S2: max attempts exceeded for %s", url[:80])
    return None


def batch_query_s2(dois: list[str], api_key: str, log: logging.Logger) -> dict:
    """POST batch request to S2. Returns {doi_lower: {references, citations}}."""
    results: dict[str, dict] = {}
    fields = "title,externalIds,references.title,references.externalIds,citations.title,citations.externalIds"

    for chunk_start in range(0, len(dois), S2_BATCH_SIZE):
        chunk = dois[chunk_start: chunk_start + S2_BATCH_SIZE]
        ids = [f"DOI:{doi}" for doi in chunk]
        body = json.dumps({"ids": ids}).encode("utf-8")
        url = f"{S2_BATCH_URL}?fields={fields}"
        headers = _s2_headers(api_key)

        log.info("S2 batch query: %d DOIs (offset %d)...", len(chunk), chunk_start)
        response = _s2_request_with_backoff(url, body, headers, log)
        if response is None:
            log.warning("S2 batch failed for chunk at offset %d — skipping", chunk_start)
            continue

        # Response is a list parallel to ids
        if not isinstance(response, list):
            log.warning("S2 batch: unexpected response type %s", type(response))
            continue

        for doi, item in zip(chunk, response):
            if item is None:
                log.debug("S2: no result for DOI %s", doi)
                continue
            results[doi.lower()] = item

        # Brief pause between batch chunks
        if chunk_start + S2_BATCH_SIZE < len(dois):
            time.sleep(1)

    return results


def search_s2_by_title(
    title: str,
    api_key: str,
    sleep_seconds: float,
    log: logging.Logger,
) -> dict | None:
    """Fallback: search S2 by title for papers without DOI."""
    import urllib.parse
    params = urllib.parse.urlencode({"query": title, "limit": 3,
                                    "fields": "title,externalIds,references.title,references.externalIds,citations.title,citations.externalIds"})
    url = f"{S2_SEARCH_URL}?{params}"
    headers = _s2_headers(api_key)
    headers.pop("Content-Type", None)

    time.sleep(sleep_seconds)
    response = _s2_request_with_backoff(url, None, headers, log)
    if not response:
        return None

    items = response.get("data", [])
    if not items:
        return None

    norm_q = _normalise_title(title)
    best_ratio = 0.0
    best_item = None
    for item in items:
        s2_title = item.get("title") or ""
        ratio = difflib.SequenceMatcher(None, norm_q, _normalise_title(s2_title)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_item = item

    if best_ratio >= 0.85:
        log.debug("S2 title match: ratio=%.2f  %r", best_ratio, title[:60])
        return best_item

    log.debug("S2 title search: best ratio %.2f below threshold for %r", best_ratio, title[:60])
    return None


# ---------------------------------------------------------------------------
# Match S2 paper against library
# ---------------------------------------------------------------------------

def _doi_from_s2_paper(paper: dict) -> str | None:
    ext = paper.get("externalIds") or {}
    doi = ext.get("DOI", "")
    return doi.lower() if doi else None


def match_s2_paper_to_library(
    paper: dict,
    doi_index: dict,
    title_index: dict,
) -> str | None:
    """Return the note_stem if this S2 paper matches a library note, else None."""
    doi = _doi_from_s2_paper(paper)
    if doi and doi in doi_index:
        return doi_index[doi]

    s2_title = paper.get("title") or ""
    if s2_title:
        norm = _normalise_title(s2_title)
        if norm in title_index:
            return title_index[norm]
        # Fuzzy fallback (cheaper than full scan — only try if no exact match)
        best_ratio = 0.0
        best_stem = None
        for lib_norm, stem in title_index.items():
            ratio = difflib.SequenceMatcher(None, norm, lib_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_stem = stem
        if best_ratio >= 0.85:
            return best_stem

    return None


# ---------------------------------------------------------------------------
# Note update logic
# ---------------------------------------------------------------------------

_RELATED_SECTION_RE = re.compile(r'^## Related Papers\s*$', re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r'^## ', re.MULTILINE)

AUTO_SUFFIX = " <!--auto-->"


def _parse_related_section(content: str) -> tuple[list[str], list[str], int, int]:
    """Parse Related Papers section from note content.

    Returns:
        user_links  — list of note_stems written by hand (no <!--auto-->)
        auto_links  — list of note_stems with <!--auto-->
        section_start — character offset of start of Related Papers block
        section_end   — character offset of end of Related Papers block
    """
    m = _RELATED_SECTION_RE.search(content)
    if not m:
        return [], [], -1, -1

    section_start = m.start()
    body_start = m.end()

    # Find next ## heading after Related Papers
    rest = content[body_start:]
    next_m = _NEXT_SECTION_RE.search(rest)
    if next_m:
        body_end = body_start + next_m.start()
    else:
        body_end = len(content)

    body = content[body_start:body_end]

    user_links: list[str] = []
    auto_links: list[str] = []

    for line in body.splitlines():
        wikilink_m = re.search(r'\[\[([^\]]+)\]\]', line)
        if not wikilink_m:
            continue
        stem = wikilink_m.group(1)
        if AUTO_SUFFIX in line:
            auto_links.append(stem)
        else:
            user_links.append(stem)

    return user_links, auto_links, section_start, body_end


def _build_related_section(user_links: list[str], auto_links: list[str]) -> str:
    """Build the ## Related Papers section text."""
    lines = ["## Related Papers", ""]
    if not user_links and not auto_links:
        lines.append("- ")
    else:
        for stem in user_links:
            lines.append(f"- [[{stem}]]")
        for stem in sorted(auto_links):
            lines.append(f"- [[{stem}]]{AUTO_SUFFIX}")
    lines.append("")
    return "\n".join(lines)


def backup_note(filepath: Path, output_folder: Path, log: logging.Logger) -> None:
    backup_dir = output_folder / ".backup"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{filepath.stem}.{ts}.md"
    shutil.copy2(filepath, backup_path)
    log.debug("Backed up %s → %s", filepath.name, backup_path.name)


def update_note_related_papers(
    filepath: Path,
    new_auto_links: set[str],
    output_folder: Path,
    log: logging.Logger,
) -> bool:
    """Update Related Papers section of a note. Returns True if file was written."""
    content = filepath.read_text(encoding="utf-8")
    user_links, current_auto, section_start, section_end = _parse_related_section(content)

    if section_start == -1:
        log.warning("No '## Related Papers' section in %s — skipping", filepath.name)
        return False

    current_auto_set = set(current_auto)
    if current_auto_set == new_auto_links:
        log.debug("No change to auto-links for %s", filepath.stem[:60])
        return False

    new_section = _build_related_section(user_links, sorted(new_auto_links))

    # Reconstruct content: everything before section + new section + everything after
    new_content = content[:section_start] + new_section + content[section_end:]

    backup_note(filepath, output_folder, log)
    filepath.write_text(new_content, encoding="utf-8")
    log.info("Updated Related Papers in %s (%d auto-links)", filepath.stem[:60], len(new_auto_links))
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Step 3 — Citation linking")
    parser.add_argument("--full", action="store_true", help="Re-query all papers (ignore cache)")
    parser.add_argument("--verbose", action="store_true", help="Show debug output on console")
    args = parser.parse_args()

    log = setup_logging(args.verbose)
    config = load_config(log)
    output_folder = Path(config["output_folder"])

    if not output_folder.exists():
        log.error("output_folder does not exist: %s", output_folder)
        sys.exit(1)

    api_key = config.get("semantic_scholar_api_key", "")
    sleep_seconds = float(config.get("s2_sleep_seconds", 1.0))

    # --- Build library index ---
    doi_index, title_index, date_index = build_library_index(output_folder, log)

    # --- Load cache ---
    cache = {} if args.full else load_cache()

    # --- Determine what to query ---
    # dois_to_query: {doi: note_stem}
    dois_to_query: dict[str, str] = {}
    no_doi_stems: list[str] = []   # stems without DOI for title-search fallback

    for doi, stem in doi_index.items():
        if not args.full and doi in cache:
            log.debug("Cache hit for DOI %s (%s)", doi, stem[:40])
        else:
            dois_to_query[doi] = stem

    for norm_title, stem in title_index.items():
        if stem not in {s for s in doi_index.values()}:
            no_doi_stems.append(stem)

    log.info("To query: %d DOI papers, %d no-DOI papers (title fallback)",
             len(dois_to_query), len(no_doi_stems))

    # --- Batch query S2 for DOI papers ---
    # s2_results: {doi: s2_item}
    s2_results: dict[str, dict] = {}
    if dois_to_query:
        batch = list(dois_to_query.keys())
        s2_results = batch_query_s2(batch, api_key, log)
        now_iso = datetime.now().isoformat()
        for doi in batch:
            cache[doi] = now_iso
        save_cache(cache)

    # --- Per-paper fallback for no-DOI papers ---
    no_doi_results: dict[str, dict] = {}  # stem: s2_item
    for stem in no_doi_stems:
        result = search_s2_by_title(stem, api_key, sleep_seconds, log)
        if result:
            no_doi_results[stem] = result
        else:
            log.debug("S2: no result for no-DOI paper %r", stem[:60])

    # --- Build bidirectional link map: {note_stem: set of linked stems} ---
    links: dict[str, set[str]] = {stem: set() for stem in title_index.values()}

    def _register_link(source_stem: str, target_stem: str) -> None:
        if source_stem == target_stem:
            return
        if source_stem in links:
            links[source_stem].add(target_stem)
        if target_stem in links:
            links[target_stem].add(source_stem)

    # Process DOI query results
    for doi, item in s2_results.items():
        source_stem = doi_index.get(doi)
        if not source_stem:
            continue
        for rel_paper in (item.get("references") or []) + (item.get("citations") or []):
            if not rel_paper:
                continue
            target_stem = match_s2_paper_to_library(rel_paper, doi_index, title_index)
            if target_stem:
                _register_link(source_stem, target_stem)

    # Process no-DOI results
    for source_stem, item in no_doi_results.items():
        for rel_paper in (item.get("references") or []) + (item.get("citations") or []):
            if not rel_paper:
                continue
            target_stem = match_s2_paper_to_library(rel_paper, doi_index, title_index)
            if target_stem:
                _register_link(source_stem, target_stem)

    # --- Update notes ---
    updated = 0
    unchanged = 0

    for stem, new_auto_links in links.items():
        filepath = output_folder / f"{stem}.md"
        if not filepath.exists():
            continue
        wrote = update_note_related_papers(filepath, new_auto_links, output_folder, log)
        if wrote:
            updated += 1
        else:
            unchanged += 1

    print()
    print("=" * 50)
    print("  Step 3 done.")
    print(f"  Notes updated:   {updated:4}")
    print(f"  Notes unchanged: {unchanged:4}")
    print("=" * 50)


if __name__ == "__main__":
    main()
