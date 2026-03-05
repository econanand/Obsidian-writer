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

def fetch_zotero_items(zot, log: logging.Logger) -> list[dict]:
    """Fetch all non-attachment, non-note items from Zotero."""
    log.info("Fetching all items from Zotero...")
    try:
        all_items = zot.everything(zot.items())
    except Exception as exc:
        log.error("Failed to fetch items from Zotero: %s", exc)
        sys.exit(1)
    items = [
        item for item in all_items
        if item.get("data", {}).get("itemType", "") not in ("attachment", "note")
    ]
    log.info("Fetched %d content items", len(items))
    return items


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

def _item_summary(item: dict) -> str:
    data = item.get("data", {})
    title = data.get("title", "(no title)")[:70]
    year = data.get("date", "")[:4] or "?"
    item_type = data.get("itemType", "?")
    doi = data.get("DOI", "") or "(no DOI)"
    date_added = data.get("dateAdded", "")[:10] or "?"
    return (
        f"  Title:      {title}\n"
        f"  Year:       {year}\n"
        f"  Type:       {item_type}\n"
        f"  DOI:        {doi}\n"
        f"  Added:      {date_added}"
    )


def review_candidates(
    candidates: list[dict],       # list of {label, zotero_item}
    zot,
    log: logging.Logger,
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

        print()
        print(f"[{i}/{total}]  Source: {label}")
        print(_item_summary(item))
        print()

        while True:
            try:
                choice = input("  Action — [d]elete / [k]eep / [s]top: ").strip().lower()
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
            elif choice == "s":
                print("Stopping review.")
                return deleted, kept
            else:
                print("  Please type d, k, or s.")

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
    delete_titles, low_conf_items = parse_harvest_progress(harvest_path, log)

    zotero_items = fetch_zotero_items(zot, log)

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

    deleted, kept = review_candidates(candidates, zot, log)

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
