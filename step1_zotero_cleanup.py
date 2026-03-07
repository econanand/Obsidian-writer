"""
Step 1 — Zotero Cleanup

Reads harvest_progress.md from Zotero-sweep to find bad/low-confidence imports,
matches them against the Zotero library, and lets you interactively review and
delete unwanted items.

Usage:
    python step1_zotero_cleanup.py [--verbose]

Keys during review:
    d — delete this item from Zotero
    k — keep this item (skip)
    u — update Zotero item (from OpenAlex or from the PDF itself)
    s — skip the rest of this session

Before any deletion the full item JSON is appended to cleanup_deletions.log —
this is your only undo path (Zotero deletions propagate on next sync).
"""

import argparse
import difflib
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Config / logging helpers (mirrored from main.py)
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent
LOG_FILE = PROJECT_DIR / "obsidian_writer.log"

STEP1_REQUIRED_KEYS = [
    "zotero_user_id",
    "zotero_api_key",
    "zotero_library_type",
    "harvest_progress_path",
]


def setup_logging(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("step1")
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
        log.error("config.json not found at %s", config_path)
        log.error("Copy config.json.template to config.json and fill in your credentials.")
        sys.exit(1)
    with open(config_path) as f:
        config = json.load(f)
    missing = [k for k in STEP1_REQUIRED_KEYS if not config.get(k)]
    if missing:
        log.error("config.json is missing required field(s): %s", ", ".join(missing))
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


# ---------------------------------------------------------------------------
# Parsing harvest_progress.md
# ---------------------------------------------------------------------------

def _normalise_title(title: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    normalised = re.sub(r"[^\w\s]", "", title.lower())
    return re.sub(r"\s+", " ", normalised).strip()


def parse_harvest_progress(filepath: Path, log: logging.Logger) -> tuple[list[str], list[tuple[float, str]]]:
    """Parse harvest_progress.md.

    Returns:
        delete_titles  — list of wrong Zotero titles from DELETE blocks
        low_conf_items — list of (conf_score, title) from low-confidence blocks
    """
    if not filepath.exists():
        log.error("harvest_progress_path not found: %s", filepath)
        sys.exit(1)

    text = filepath.read_text(encoding="utf-8")
    lines = text.splitlines()

    delete_titles: list[str] = []
    low_conf_items: list[tuple[float, str]] = []

    # Track which block we're in
    in_delete_block = False
    in_low_conf_block = False

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if "**Bad imports to DELETE" in stripped:
            in_delete_block = True
            in_low_conf_block = False
            continue
        if "**Low-confidence imports" in stripped:
            in_low_conf_block = True
            in_delete_block = False
            continue
        # A new ### section or ** section header resets context
        if stripped.startswith("###") or (stripped.startswith("**") and ":" in stripped):
            if not ("Bad imports to DELETE" in stripped or "Low-confidence imports" in stripped):
                in_delete_block = False
                in_low_conf_block = False

        if in_delete_block:
            # Pattern: → imported as *Title* or → imported as *Title* (...)
            m = re.search(r'→ imported as \*([^*]+)\*', line)
            if m:
                title = m.group(1).strip()
                delete_titles.append(title)
                log.debug("DELETE candidate: %r", title)

        if in_low_conf_block:
            # Pattern: - conf 7.0: *Title*
            m = re.match(r'-\s+conf\s+([\d.]+):\s+\*([^*]+)\*', stripped)
            if m:
                conf = float(m.group(1))
                title = m.group(2).strip()
                low_conf_items.append((conf, title))
                log.debug("Low-conf candidate: conf=%.1f  %r", conf, title)

    log.info("Parsed %d DELETE candidates, %d low-confidence candidates",
             len(delete_titles), len(low_conf_items))
    return delete_titles, low_conf_items


# ---------------------------------------------------------------------------
# Matching against Zotero library
# ---------------------------------------------------------------------------

def fetch_zotero_items(zot, log: logging.Logger) -> tuple[list[dict], dict]:
    """Fetch all content items and build attachment map.

    Returns:
        items               — list of non-attachment, non-note item dicts
        parent_attachments  — {parent_key: [attachment data dicts]}
    """
    log.info("Fetching all items from Zotero...")
    try:
        all_items = zot.everything(zot.items())
    except Exception as exc:
        log.error("Failed to fetch items from Zotero: %s", exc)
        sys.exit(1)

    items = []
    parent_attachments: dict[str, list[dict]] = {}

    for item in all_items:
        data = item.get("data", {})
        item_type = data.get("itemType", "")
        if item_type == "attachment":
            parent_key = data.get("parentItem")
            if parent_key:
                parent_attachments.setdefault(parent_key, []).append(data)
        elif item_type != "note":
            items.append(item)

    log.info("Fetched %d content items", len(items))
    return items, parent_attachments


def _resolve_pdf_path(attachments: list[dict], storage_path: str):
    """Return path to first existing PDF attachment, or None."""
    from pathlib import Path as _Path
    for att in attachments:
        if att.get("contentType") != "application/pdf":
            continue
        link_mode = att.get("linkMode", "")
        if link_mode == "linked_file":
            p = _Path(att.get("path", ""))
            if p.exists():
                return p
        elif link_mode == "imported_file":
            key = att.get("key", "")
            filename = att.get("filename", "")
            if key and filename:
                p = _Path(storage_path) / key / filename
                if p.exists():
                    return p
    return None


def extract_pdf_snippet(pdf_path, log: logging.Logger) -> dict:
    """Extract title guess and abstract snippet from first 3 pages of PDF.

    Returns dict with 'title' and 'abstract' keys (empty strings if not found).
    """
    result = {"title": "", "abstract": ""}
    try:
        import PyPDF2
    except ImportError:
        return result

    full_text = ""
    try:
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f, strict=False)
            pages_to_check = min(3, len(reader.pages))
            for i in range(pages_to_check):
                try:
                    full_text += (reader.pages[i].extract_text() or "") + "\n"
                except Exception:
                    continue
    except Exception as exc:
        log.debug("PDF read error for %s: %s", pdf_path.name, exc)
        return result

    if not full_text:
        return result

    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    # Title: first non-trivial line (>10 chars, not a code/number pattern)
    for line in lines[:10]:
        if len(line) > 10 and not re.match(r'^(10\.\d{4}|PII|doi)', line, re.IGNORECASE):
            result["title"] = line[:120]
            break

    # Abstract: text between "Abstract" keyword and next section boundary
    abstract_m = re.search(r'\bAbstract\b', full_text, re.IGNORECASE)
    if abstract_m:
        text_after = full_text[abstract_m.end():]
        boundary_m = re.search(
            r'\n\s*(?:\d[\.\s]|Introduction|Keywords?|JEL|I\.\s)',
            text_after, re.IGNORECASE,
        )
        if boundary_m:
            text_after = text_after[:boundary_m.start()]
        abstract = re.sub(r'\s+', ' ', text_after).strip().lstrip(':').strip()
        if len(abstract) >= 50:
            result["abstract"] = abstract[:500]  # show snippet in review

    return result


OPENALEX_BASE = "https://api.openalex.org/works"
OPENALEX_MAILTO = "anand.guitarist@gmail.com"
OPENALEX_SLEEP = 0.15


def _oa_search_for_metadata(title: str, log: logging.Logger) -> dict | None:
    """Search OpenAlex by title; return parsed metadata dict or None.

    Requires similarity >= 0.75 between query and best result title.
    """
    params = urllib.parse.urlencode({
        "search": title,
        "select": "id,title,doi,authorships,publication_year,primary_location,biblio",
        "per_page": 3,
        "mailto": OPENALEX_MAILTO,
    })
    url = f"{OPENALEX_BASE}?{params}"
    headers = {
        "Accept": "application/json",
        "User-Agent": f"ObsidianWriter/1.0 (mailto:{OPENALEX_MAILTO})",
    }
    time.sleep(OPENALEX_SLEEP)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("OpenAlex search error: %s", exc)
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

    if best_ratio < 0.75 or best_work is None:
        log.debug("OpenAlex: best ratio %.2f below threshold for %r", best_ratio, title[:60])
        return None

    doi = (best_work.get("doi") or "").replace("https://doi.org/", "")
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (best_work.get("authorships") or [])
        if (a.get("author") or {}).get("display_name")
    ]
    year = str(best_work.get("publication_year") or "")
    journal = ((best_work.get("primary_location") or {})
               .get("source") or {}).get("display_name", "") or ""
    biblio = best_work.get("biblio") or {}
    first_page = biblio.get("first_page") or ""
    last_page = biblio.get("last_page") or ""
    pages = f"{first_page}-{last_page}" if first_page and last_page else first_page

    return {
        "title": best_work.get("title") or "",
        "doi": doi,
        "authors": authors,
        "year": year,
        "journal": journal,
        "volume": biblio.get("volume") or "",
        "issue": biblio.get("issue") or "",
        "pages": pages,
        "match_ratio": best_ratio,
    }


def update_item_metadata(zot, item: dict, metadata: dict, log: logging.Logger) -> bool:
    """Patch Zotero item with OpenAlex metadata. Returns True on success.

    Only fills fields that are currently empty in Zotero (except title,
    which is always updated when it differs).
    """
    key = item["key"]
    try:
        fresh = zot.item(key)
    except Exception as exc:
        log.error("Failed to re-fetch item %s for update: %s", key, exc)
        return False

    data = fresh["data"]
    changes: list[str] = []

    def _set(field: str, value: str, overwrite: bool = False) -> None:
        if value and (overwrite or not data.get(field, "").strip()):
            data[field] = value
            changes.append(field)

    _set("title", metadata.get("title", ""), overwrite=True)
    _set("DOI", metadata.get("doi", ""))
    _set("date", metadata.get("year", ""))
    _set("publicationTitle", metadata.get("journal", ""))
    _set("volume", metadata.get("volume", ""))
    _set("issue", metadata.get("issue", ""))
    _set("pages", metadata.get("pages", ""))

    if metadata.get("authors") and not data.get("creators"):
        creators = []
        for name in metadata["authors"]:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author",
                                  "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "name": name})
        data["creators"] = creators
        changes.append("creators")

    if not changes:
        log.info("No new fields to update for item %s", key)
        return False

    try:
        zot.update_item(fresh)
        log.info("Updated item %s — changed: %s", key, changes)
        return True
    except Exception as exc:
        log.error("Failed to update item %s: %s", key, exc)
        return False


def update_item_from_pdf(zot, item: dict, pdf_snippet: dict, log: logging.Logger) -> bool:
    """Patch Zotero item title and abstractNote from PDF-extracted text.

    Title is always overwritten; abstractNote is only filled if currently empty.
    Returns True if any changes were written.
    """
    key = item["key"]
    try:
        fresh = zot.item(key)
    except Exception as exc:
        log.error("Failed to re-fetch item %s for update: %s", key, exc)
        return False

    data = fresh["data"]
    changes: list[str] = []

    pdf_title = (pdf_snippet.get("title") or "").strip()
    pdf_abstract = (pdf_snippet.get("abstract") or "").strip()

    if pdf_title:
        data["title"] = pdf_title
        changes.append("title")
    if pdf_abstract and not data.get("abstractNote", "").strip():
        data["abstractNote"] = pdf_abstract
        changes.append("abstractNote")

    if not changes:
        log.info("No new fields to update from PDF for item %s", key)
        return False

    try:
        zot.update_item(fresh)
        log.info("Updated item %s from PDF — changed: %s", key, changes)
        return True
    except Exception as exc:
        log.error("Failed to update item %s: %s", key, exc)
        return False


def match_title(candidate: str, zotero_items: list[dict], threshold: float) -> list[dict]:
    """Return Zotero items whose title fuzzy-matches candidate at >= threshold."""
    norm_candidate = _normalise_title(candidate)
    if not norm_candidate:
        return []
    matches = []
    for item in zotero_items:
        title = item.get("data", {}).get("title", "").strip()
        if not title:
            continue
        ratio = difflib.SequenceMatcher(None, norm_candidate, _normalise_title(title)).ratio()
        if ratio >= threshold:
            matches.append((ratio, item))
    # Return best matches first
    matches.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in matches]


# ---------------------------------------------------------------------------
# Backup / deletion helpers
# ---------------------------------------------------------------------------

DELETIONS_LOG = PROJECT_DIR / "cleanup_deletions.log"


def backup_item_json(item: dict, log: logging.Logger) -> None:
    """Append full item JSON to cleanup_deletions.log before deletion."""
    with open(DELETIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, indent=2))
        f.write("\n---\n")
    log.debug("Item JSON backed up to %s", DELETIONS_LOG)


def delete_item_with_retry(zot, item: dict, log: logging.Logger) -> bool:
    """Re-fetch item for current version, then delete. Retry once on HTTP 412."""
    key = item["key"]
    for attempt in range(2):
        try:
            fresh = zot.item(key)
        except Exception as exc:
            log.error("Failed to re-fetch item %s: %s", key, exc)
            return False
        try:
            zot.delete_item(fresh)
            log.info("Deleted item %s: %r", key, fresh.get("data", {}).get("title", "")[:60])
            return True
        except Exception as exc:
            # pyzotero raises a generic exception; check message for 412
            if "412" in str(exc) and attempt == 0:
                log.warning("HTTP 412 (stale version) for %s — retrying", key)
                continue
            log.error("Failed to delete item %s: %s", key, exc)
            return False
    return False


# ---------------------------------------------------------------------------
# Interactive review loop
# ---------------------------------------------------------------------------

def _item_summary(item: dict, pdf_snippet: dict | None = None) -> str:
    data = item.get("data", {})
    title = data.get("title", "(no title)")[:70]
    year = data.get("date", "")[:4] or "?"
    item_type = data.get("itemType", "?")
    doi = data.get("DOI", "") or "(no DOI)"
    date_added = data.get("dateAdded", "")[:10] or "?"
    lines = [
        f"  Title:      {title}",
        f"  Year:       {year}",
        f"  Type:       {item_type}",
        f"  DOI:        {doi}",
        f"  Added:      {date_added}",
    ]
    if pdf_snippet:
        if pdf_snippet.get("title"):
            lines.append(f"  PDF title:  {pdf_snippet['title'][:70]}")
        if pdf_snippet.get("abstract"):
            lines.append(f"  PDF abstr:  {pdf_snippet['abstract'][:120]}…")
    return "\n".join(lines)


def review_candidates(
    candidates: list[dict],       # list of {label, zotero_item}
    zot,
    log: logging.Logger,
    parent_attachments: dict | None = None,
    storage_path: str = "",
) -> tuple[int, int]:
    """Run interactive d/k/s review. Returns (deleted_count, kept_count)."""
    deleted = 0
    kept = 0
    total = len(candidates)

    for i, entry in enumerate(candidates, 1):
        label = entry["label"]
        item = entry["item"]
        data = item.get("data", {})
        title = data.get("title", "(no title)")[:70]

        # Try to extract PDF snippet for context
        pdf_snippet = None
        if parent_attachments and storage_path:
            item_key = item.get("key", "")
            attachments = parent_attachments.get(item_key, [])
            pdf_path = _resolve_pdf_path(attachments, storage_path)
            if pdf_path:
                pdf_snippet = extract_pdf_snippet(pdf_path, log)

        print()
        print(f"[{i}/{total}]  Source: {label}")
        print(_item_summary(item, pdf_snippet))
        print()

        while True:
            try:
                choice = input("  Action — [d]elete / [k]eep / [u]pdate via OpenAlex / [s]top: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return deleted, kept

            if choice == "d":
                backup_item_json(item, log)
                success = delete_item_with_retry(zot, item, log)
                if success:
                    print(f"  → Deleted: {title[:60]}")
                    deleted += 1
                else:
                    print(f"  → Deletion failed (see log). Item kept.")
                    kept += 1
                break
            elif choice == "k":
                print(f"  → Kept: {title[:60]}")
                kept += 1
                break
            elif choice == "u":
                pdf_has_content = bool(pdf_snippet and (pdf_snippet.get("title") or pdf_snippet.get("abstract")))

                # Decide source: if PDF content is available, ask; otherwise go straight to OpenAlex
                if pdf_has_content:
                    try:
                        src = input("  Update from: [o]penAlex / [p]df: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print("\nAborted.")
                        return deleted, kept
                else:
                    src = "o"

                if src == "p":
                    print(f"  PDF title:    {(pdf_snippet.get('title') or '(none)')[:70]}")
                    print(f"  PDF abstract: {(pdf_snippet.get('abstract') or '(none)')[:120]}…")
                    try:
                        confirm = input("  Apply these changes to Zotero? [y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print("\nAborted.")
                        return deleted, kept
                    if confirm == "y":
                        updated = update_item_from_pdf(zot, item, pdf_snippet, log)
                        if updated:
                            print("  → Zotero item updated from PDF.")
                        else:
                            print("  → No changes applied (fields already filled or error).")
                else:
                    search_title = (pdf_snippet.get("title") or "") if pdf_snippet else ""
                    if not search_title:
                        search_title = data.get("title", "")
                    print(f"  Searching OpenAlex for: {search_title[:70]!r} ...")
                    metadata = _oa_search_for_metadata(search_title, log)
                    if not metadata:
                        print("  No OpenAlex match found (ratio below 0.75 or network error).")
                    else:
                        print(f"  OpenAlex match (ratio {metadata['match_ratio']:.2f}):")
                        print(f"    Title:   {metadata['title'][:70]}")
                        print(f"    Authors: {', '.join(metadata['authors'][:3])}")
                        print(f"    Year:    {metadata['year']}")
                        print(f"    Journal: {metadata['journal'][:60]}")
                        print(f"    DOI:     {metadata['doi'] or '(none)'}")
                        try:
                            confirm = input("  Apply these changes to Zotero? [y/n]: ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print("\nAborted.")
                            return deleted, kept
                        if confirm == "y":
                            updated = update_item_metadata(zot, item, metadata, log)
                            if updated:
                                print("  → Zotero item updated from OpenAlex.")
                            else:
                                print("  → No changes applied (fields already filled or error).")
                kept += 1
                break
            elif choice == "s":
                print("Stopping review.")
                return deleted, kept
            else:
                print("  Please type d, k, u, or s.")

    return deleted, kept


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Step 1 — Zotero cleanup")
    parser.add_argument("--verbose", action="store_true", help="Show debug output on console")
    args = parser.parse_args()

    log = setup_logging(args.verbose)
    config = load_config(log)
    zot = connect_zotero(config, log)

    harvest_path = Path(config["harvest_progress_path"])
    storage_path = config.get("zotero_storage_path", "")
    delete_titles, low_conf_items = parse_harvest_progress(harvest_path, log)

    zotero_items, parent_attachments = fetch_zotero_items(zot, log)

    # Build review queue
    candidates: list[dict] = []
    unmatched: list[str] = []

    # DELETE candidates (threshold 0.85 — we know the exact wrong title)
    for title in delete_titles:
        matches = match_title(title, zotero_items, threshold=0.85)
        if matches:
            for item in matches:
                candidates.append({
                    "label": f"DELETE candidate — wrong title: {title!r}",
                    "item": item,
                })
        else:
            log.info("No Zotero match found for DELETE candidate: %r", title)
            unmatched.append(f"DELETE: {title!r}")

    # Low-confidence candidates (threshold 0.7)
    for conf, title in low_conf_items:
        matches = match_title(title, zotero_items, threshold=0.70)
        if matches:
            for item in matches[:1]:  # only best match for low-conf
                candidates.append({
                    "label": f"Low-confidence import (conf {conf}) — title: {title!r}",
                    "item": item,
                })
        else:
            log.info("No Zotero match found for low-conf candidate: conf=%.1f  %r", conf, title)
            unmatched.append(f"low-conf {conf}: {title!r}")

    if not candidates:
        print("No matching items found in Zotero for any candidate.")
        if unmatched:
            print(f"\n{len(unmatched)} entries had no Zotero match:")
            for u in unmatched:
                print(f"  - {u}")
        return

    print()
    print("=" * 60)
    print(f"  {len(candidates)} item(s) found for review.")
    if unmatched:
        print(f"  {len(unmatched)} candidate(s) had no Zotero match (see log).")
    print(f"  Deletions backed up to: {DELETIONS_LOG}")
    print("=" * 60)

    deleted, kept = review_candidates(candidates, zot, log,
                                      parent_attachments=parent_attachments,
                                      storage_path=storage_path)

    print()
    print("=" * 60)
    print(f"  Done.  Deleted: {deleted}  Kept: {kept}")
    print("=" * 60)

    if unmatched:
        log.info("Unmatched entries (no Zotero item found):")
        for u in unmatched:
            log.info("  %s", u)


if __name__ == "__main__":
    main()
