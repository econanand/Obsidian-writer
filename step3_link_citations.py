"""
Step 3 — Citation Linking (OpenAlex)

Queries OpenAlex for references of all papers in your library. Matches returned
papers against the library. Updates the "Related Papers" section of each note
with bidirectional links.

No API key needed — OpenAlex is free. A polite-pool email is included in
every request (required for higher rate limits).

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
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Config / logging
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "obsidian_writer.log"
CACHE_FILE = PROJECT_DIR / "s2_cache.json"   # kept same name for continuity

OPENALEX_BASE = "https://api.openalex.org/works"
OPENALEX_BATCH_SIZE = 50          # max DOIs per filter query
OPENALEX_MAILTO = "anand.guitarist@gmail.com"
OPENALEX_SLEEP = 0.15             # ~7 req/sec — well within polite-pool 10/sec limit
OPENALEX_SELECT = "id,doi,title,referenced_works"

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


def _normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def build_library_index(output_folder: Path, log: logging.Logger) -> tuple[dict, dict]:
    """Parse all .md notes and build lookup indexes.

    Returns:
        doi_index   — {lowercase_doi: note_stem}
        title_index — {normalised_title: note_stem}
    """
    doi_index: dict[str, str] = {}
    title_index: dict[str, str] = {}

    for md_file in output_folder.glob("*.md"):
        stem = md_file.stem
        title_index[_normalise_title(stem)] = stem
        content = md_file.read_text(encoding="utf-8")
        m = _DOI_LINE_RE.search(content)
        if m:
            doi = m.group(1).strip().lower()
            if doi:
                doi_index[doi] = stem

    log.info("Library index: %d notes, %d with DOI", len(title_index), len(doi_index))
    return doi_index, title_index


# ---------------------------------------------------------------------------
# Cache (reused across runs for incremental queries)
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
# OpenAlex API calls
# ---------------------------------------------------------------------------

def _oa_get(url: str, log: logging.Logger) -> dict | None:
    """GET from OpenAlex with simple retry on transient errors."""
    time.sleep(OPENALEX_SLEEP)
    headers = {
        "Accept": "application/json",
        "User-Agent": f"ObsidianWriter/1.0 (mailto:{OPENALEX_MAILTO})",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("OpenAlex HTTP %d for %s", e.code, url[:80])
        return None
    except Exception as exc:
        log.warning("OpenAlex request error: %s", exc)
        return None


def _strip_doi_prefix(doi_url: str) -> str:
    """'https://doi.org/10.xxx/yyy' → '10.xxx/yyy' (lowercase)."""
    if not doi_url:
        return ""
    doi = doi_url.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
        if doi.startswith(prefix):
            return doi[len(prefix):]
    return doi


def _strip_oa_prefix(oa_id: str) -> str:
    """'https://openalex.org/W123' → 'W123'."""
    return oa_id.split("/")[-1] if oa_id else ""


def batch_query_openalex(dois: list[str], log: logging.Logger) -> dict:
    """Batch-fetch works from OpenAlex by DOI.

    Returns {lowercase_doi: {oa_id, doi, title, referenced_work_ids}}.
    Uses filter=doi:10.xxx|10.yyy — up to OPENALEX_BATCH_SIZE per request.
    """
    results: dict[str, dict] = {}

    for chunk_start in range(0, len(dois), OPENALEX_BATCH_SIZE):
        chunk = dois[chunk_start: chunk_start + OPENALEX_BATCH_SIZE]
        doi_filter = "|".join(chunk)
        params = urllib.parse.urlencode({
            "filter": f"doi:{doi_filter}",
            "select": OPENALEX_SELECT,
            "per_page": OPENALEX_BATCH_SIZE,
            "mailto": OPENALEX_MAILTO,
        })
        url = f"{OPENALEX_BASE}?{params}"

        log.info("OpenAlex batch query: %d DOIs (offset %d)...", len(chunk), chunk_start)
        data = _oa_get(url, log)
        if not data:
            log.warning("OpenAlex: no response for chunk at offset %d", chunk_start)
            continue

        for work in data.get("results", []):
            raw_doi = work.get("doi", "")
            doi = _strip_doi_prefix(raw_doi)
            if not doi:
                continue
            oa_id = _strip_oa_prefix(work.get("id", ""))
            ref_ids = [_strip_oa_prefix(r) for r in (work.get("referenced_works") or [])]
            results[doi] = {
                "oa_id": oa_id,
                "doi": doi,
                "title": work.get("title") or "",
                "referenced_work_ids": ref_ids,
            }

        log.debug("OpenAlex: got %d results for chunk (cumulative: %d)",
                  len(data.get("results", [])), len(results))

    return results


def search_openalex_by_title(title: str, log: logging.Logger) -> dict | None:
    """Fallback: search OpenAlex by title for papers without DOI."""
    params = urllib.parse.urlencode({
        "search": title,
        "select": OPENALEX_SELECT,
        "per_page": 3,
        "mailto": OPENALEX_MAILTO,
    })
    url = f"{OPENALEX_BASE}?{params}"
    data = _oa_get(url, log)
    if not data:
        return None

    results = data.get("results", [])
    if not results:
        return None

    norm_q = _normalise_title(title)
    best_ratio = 0.0
    best_work = None
    for work in results:
        oa_title = work.get("title") or ""
        ratio = difflib.SequenceMatcher(None, norm_q, _normalise_title(oa_title)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_work = work

    if best_ratio >= 0.85 and best_work:
        oa_id = _strip_oa_prefix(best_work.get("id", ""))
        ref_ids = [_strip_oa_prefix(r) for r in (best_work.get("referenced_works") or [])]
        log.debug("OpenAlex title match: ratio=%.2f  %r", best_ratio, title[:60])
        return {
            "oa_id": oa_id,
            "doi": _strip_doi_prefix(best_work.get("doi", "")),
            "title": best_work.get("title") or "",
            "referenced_work_ids": ref_ids,
        }

    log.debug("OpenAlex title search: best ratio %.2f below threshold for %r",
              best_ratio, title[:60])
    return None


# ---------------------------------------------------------------------------
# Note update logic
# ---------------------------------------------------------------------------

_RELATED_SECTION_RE = re.compile(r'^## Related Papers\s*$', re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r'^## ', re.MULTILINE)

AUTO_SUFFIX = " <!--auto-->"


def _parse_related_section(content: str) -> tuple[list[str], list[str], int, int]:
    """Parse Related Papers section.

    Returns: (user_links, auto_links, section_start, section_end)
    """
    m = _RELATED_SECTION_RE.search(content)
    if not m:
        return [], [], -1, -1

    section_start = m.start()
    body_start = m.end()
    rest = content[body_start:]
    next_m = _NEXT_SECTION_RE.search(rest)
    body_end = body_start + next_m.start() if next_m else len(content)
    body = content[body_start:body_end]

    user_links: list[str] = []
    auto_links: list[str] = []
    for line in body.splitlines():
        wl = re.search(r'\[\[([^\]]+)\]\]', line)
        if not wl:
            continue
        stem = wl.group(1)
        if AUTO_SUFFIX in line:
            auto_links.append(stem)
        else:
            user_links.append(stem)

    return user_links, auto_links, section_start, body_end


def _build_related_section(user_links: list[str], auto_links: list[str]) -> str:
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
    """Update Related Papers section. Returns True if file was written."""
    content = filepath.read_text(encoding="utf-8")
    user_links, current_auto, section_start, section_end = _parse_related_section(content)

    if section_start == -1:
        log.warning("No '## Related Papers' section in %s — skipping", filepath.name)
        return False

    if set(current_auto) == new_auto_links:
        log.debug("No change to auto-links for %s", filepath.stem[:60])
        return False

    new_section = _build_related_section(user_links, sorted(new_auto_links))
    new_content = content[:section_start] + new_section + content[section_end:]

    backup_note(filepath, output_folder, log)
    filepath.write_text(new_content, encoding="utf-8")
    log.info("Updated Related Papers in %s (%d auto-links)", filepath.stem[:60], len(new_auto_links))
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Step 3 — Citation linking via OpenAlex")
    parser.add_argument("--full", action="store_true", help="Re-query all papers (ignore cache)")
    parser.add_argument("--verbose", action="store_true", help="Show debug output on console")
    args = parser.parse_args()

    log = setup_logging(args.verbose)
    config = load_config(log)
    output_folder = Path(config["output_folder"])

    if not output_folder.exists():
        log.error("output_folder does not exist: %s", output_folder)
        sys.exit(1)

    # --- Build library index ---
    doi_index, title_index = build_library_index(output_folder, log)

    # --- Load cache ---
    cache = {} if args.full else load_cache()

    # --- Determine what to query ---
    dois_to_query: list[str] = []
    no_doi_stems: list[str] = []
    doi_stems = set(doi_index.values())

    for doi in doi_index:
        if args.full or doi not in cache:
            dois_to_query.append(doi)
        else:
            log.debug("Cache hit: %s", doi)

    for norm, stem in title_index.items():
        if stem not in doi_stems:
            no_doi_stems.append(stem)

    log.info("To query: %d DOI papers, %d no-DOI papers (title fallback)",
             len(dois_to_query), len(no_doi_stems))

    # --- Batch query OpenAlex for DOI papers ---
    # oa_results: {doi: {oa_id, referenced_work_ids, ...}}
    oa_results: dict[str, dict] = {}
    if dois_to_query:
        oa_results = batch_query_openalex(dois_to_query, log)
        now_iso = datetime.now().isoformat()
        for doi in dois_to_query:
            cache[doi] = now_iso
        save_cache(cache)
        log.info("OpenAlex returned data for %d / %d queried DOIs",
                 len(oa_results), len(dois_to_query))

    # --- Per-paper fallback for no-DOI papers ---
    no_doi_results: dict[str, dict] = {}
    for stem in no_doi_stems:
        result = search_openalex_by_title(stem, log)
        if result:
            no_doi_results[stem] = result
        else:
            log.debug("OpenAlex: no result for %r", stem[:60])

    # --- Build OpenAlex ID → note_stem reverse index ---
    # This lets us match referenced_work_ids back to library notes.
    oa_id_to_stem: dict[str, str] = {}
    for doi, work in oa_results.items():
        oa_id = work.get("oa_id", "")
        stem = doi_index.get(doi)
        if oa_id and stem:
            oa_id_to_stem[oa_id] = stem
    for stem, work in no_doi_results.items():
        oa_id = work.get("oa_id", "")
        if oa_id:
            oa_id_to_stem[oa_id] = stem

    # --- Build bidirectional link map ---
    links: dict[str, set[str]] = {stem: set() for stem in title_index.values()}

    def _register_link(source_stem: str, target_stem: str) -> None:
        if source_stem == target_stem:
            return
        if source_stem in links:
            links[source_stem].add(target_stem)
        if target_stem in links:
            links[target_stem].add(source_stem)

    for doi, work in oa_results.items():
        source_stem = doi_index.get(doi)
        if not source_stem:
            continue
        for ref_id in work.get("referenced_work_ids", []):
            target_stem = oa_id_to_stem.get(ref_id)
            if target_stem:
                _register_link(source_stem, target_stem)

    for source_stem, work in no_doi_results.items():
        for ref_id in work.get("referenced_work_ids", []):
            target_stem = oa_id_to_stem.get(ref_id)
            if target_stem:
                _register_link(source_stem, target_stem)

    total_links = sum(len(v) for v in links.values())
    log.info("Total bidirectional links found: %d", total_links)

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
    print(f"  Links found:     {total_links:4}")
    print("=" * 50)


if __name__ == "__main__":
    main()
