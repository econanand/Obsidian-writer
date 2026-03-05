"""
Obsidian-writer: Generate Obsidian markdown notes from your Zotero library.

For each paper in Zotero, creates one .md file in the configured output folder.
Skips papers that already have a note (create-only; never overwrites).

Usage:
    python main.py
"""

import json
import re
import sys
import unicodedata
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REQUIRED_CONFIG_KEYS = [
    "zotero_user_id",
    "zotero_api_key",
    "zotero_library_type",
    "zotero_storage_path",
    "output_folder",
]


def load_config():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print(
            f"Error: config.json not found at {config_path}\n"
            "Copy config.json.template to config.json and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(config_path) as f:
        config = json.load(f)
    missing = [k for k in REQUIRED_CONFIG_KEYS if not config.get(k)]
    if missing:
        print(
            f"Error: config.json is missing required field(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return config


# ---------------------------------------------------------------------------
# Zotero connection
# ---------------------------------------------------------------------------

def connect_zotero(config):
    try:
        from pyzotero import zotero
    except ImportError:
        print(
            "Error: pyzotero is not installed.\n"
            "Run: pip install pyzotero",
            file=sys.stderr,
        )
        sys.exit(1)
    return zotero.Zotero(
        library_id=config["zotero_user_id"],
        library_type=config["zotero_library_type"],
        api_key=config["zotero_api_key"],
    )


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_items(zot):
    """Fetch all items in one API call; return (parent_items, attachments_dict).

    attachments_dict maps parent_key -> list of attachment data dicts.
    """
    try:
        all_items = zot.everything(zot.items())
    except Exception as exc:
        print(f"Error fetching items from Zotero: {exc}", file=sys.stderr)
        sys.exit(1)

    parent_items = []
    parent_to_attachments = {}  # parent_key -> [attachment data dict, ...]

    for item in all_items:
        data = item.get("data", {})
        item_type = data.get("itemType", "")
        if item_type == "attachment":
            parent_key = data.get("parentItem")
            if parent_key:
                parent_to_attachments.setdefault(parent_key, []).append(data)
        elif item_type != "note":
            parent_items.append(item)

    return parent_items, parent_to_attachments


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def extract_year(date_str):
    """Extract 4-digit year from various Zotero date formats.

    Handles: '2023', '01/2020', '2022-03-04', '2024-3-28', '1870'.
    """
    if not date_str:
        return None
    # ISO-style: starts with YYYY (e.g., '2023', '2022-03-04')
    m = re.match(r'^(\d{4})', date_str.strip())
    if m:
        return m.group(1)
    # MM/YYYY format (e.g., '01/2020')
    m = re.match(r'^\d{1,2}/(\d{4})', date_str.strip())
    if m:
        return m.group(1)
    # Fallback: any 4-digit number
    m = re.search(r'\b(\d{4})\b', date_str)
    if m:
        return m.group(1)
    return None


def extract_doi(data):
    """Get DOI from data dict; checks DOI field then extra field."""
    doi = data.get("DOI", "").strip()
    if doi:
        return doi
    extra = data.get("extra", "")
    if extra:
        m = re.search(r'DOI:\s*(\S+)', extra, re.IGNORECASE)
        if m:
            return m.group(1).rstrip(".,;)")
    return None


def extract_pdf_path(attachments, storage_path):
    """Return path to first existing PDF attachment, or None."""
    for att in attachments:
        link_mode = att.get("linkMode", "")
        content_type = att.get("contentType", "")
        if content_type != "application/pdf":
            continue
        if link_mode == "linked_file":
            path = Path(att.get("path", ""))
            if path.exists():
                return str(path)
        elif link_mode == "imported_file":
            key = att.get("key", "")
            filename = att.get("filename", "")
            if key and filename:
                path = Path(storage_path) / key / filename
                if path.exists():
                    return str(path)
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _creator_names(creators):
    """Yield (first, last) or (None, name) tuples for each creator."""
    for c in creators:
        if c.get("name"):
            yield (None, c["name"])
        else:
            yield (c.get("firstName", "").strip(), c.get("lastName", "").strip())


def format_authors_wikilink(creators):
    """Return comma-separated [[WikiLink]] author list."""
    parts = []
    for first, last in _creator_names(creators):
        if first is None:
            parts.append(f"[[{last}]]")
        elif first and last:
            parts.append(f"[[{first} {last}]]")
        elif last:
            parts.append(f"[[{last}]]")
    return ", ".join(parts)


def format_authors_bibtex(creators):
    """Return ' and '-separated author list for BibTeX."""
    parts = []
    for first, last in _creator_names(creators):
        if first is None:
            parts.append(last)
        elif first and last:
            parts.append(f"{first} {last}")
        elif last:
            parts.append(last)
    return " and ".join(parts)


def format_tags(tags):
    """Convert Zotero tags list to Obsidian #tag-name format.

    Spaces become hyphens. Tags starting with a digit are skipped.
    """
    if not tags:
        return ""
    formatted = []
    for t in tags:
        name = t.get("tag", "").strip()
        if name and not name[0].isdigit():
            name = name.replace(" ", "-")
            formatted.append(f"#{name}")
    return " ".join(formatted)


def generate_cite_key(data, creators, year):
    """Generate a BibTeX cite key.

    Uses 'Citation Key: <value>' from extra if present.
    Otherwise: {FirstAuthorLastname}{Year}.
    """
    extra = data.get("extra", "")
    if extra:
        m = re.search(r'Citation Key:\s*(\S+)', extra, re.IGNORECASE)
        if m:
            return m.group(1)

    # Build from first author's last name + year
    last_name = ""
    for c in creators:
        if c.get("name"):
            last_name = c["name"].strip().rsplit(" ", 1)[-1]
        else:
            last_name = c.get("lastName", "").strip()
        if last_name:
            break

    # Strip diacritics, keep only ASCII letters
    last_name = unicodedata.normalize("NFKD", last_name).encode("ascii", "ignore").decode()
    last_name = re.sub(r"[^A-Za-z]", "", last_name)

    year_str = year or ""
    return f"{last_name}{year_str}" if last_name else f"Unknown{year_str}"


def sanitize_filename(title):
    """Remove forbidden filename characters from title (no replacement character).

    Truncates to 200 characters to stay safely within the 255-byte OS limit
    (the .md extension adds 3 more). Truncation is done at a word boundary.
    """
    sanitized = re.sub(r'[:\\/?*<>|"]', "", title)
    sanitized = re.sub(r" +", " ", sanitized).strip()
    if len(sanitized) > 200:
        sanitized = sanitized[:200].rsplit(" ", 1)[0].rstrip()
    return sanitized


def build_bibtex_type(item_type):
    """Map Zotero item type to BibTeX entry type."""
    mapping = {
        "journalArticle": "article",
        "bookSection": "incollection",
        "book": "book",
        "conferencePaper": "inproceedings",
    }
    return mapping.get(item_type, "misc")


# ---------------------------------------------------------------------------
# Note rendering
# ---------------------------------------------------------------------------

def render_note(data, pdf_path, year, doi):
    """Build the full markdown note content matching the golden note format."""
    title = data.get("title", "Untitled")
    item_type = data.get("itemType", "journalArticle")
    creators = data.get("creators", [])

    # Journal / book title
    if item_type == "bookSection":
        journal = data.get("bookTitle", "").strip()
    else:
        journal = data.get("publicationTitle", "").strip()

    volume = data.get("volume", "").strip()
    issue = data.get("issue", "").strip()
    pages = data.get("pages", "").strip()
    publisher = data.get("publisher", "").strip()
    date_added = data.get("dateAdded", "")[:10]  # YYYY-MM-DD

    authors_wiki = format_authors_wikilink(creators)
    authors_bib = format_authors_bibtex(creators)
    cite_key = generate_cite_key(data, creators, year)
    bibtex_type = build_bibtex_type(item_type)
    tags_str = format_tags(data.get("tags", []))

    # --- Metadata lines (omit optional fields if empty) ---
    meta_lines = [f"- **Authors**: {authors_wiki}"]
    if journal:
        meta_lines.append(f"- **Journal**: {journal}")
    if year:
        meta_lines.append(f"- **Year**: {year}")
    if volume:
        meta_lines.append(f"- **Volume**: {volume}")
    if issue:
        meta_lines.append(f"- **Issue**: {issue}")
    if pages:
        meta_lines.append(f"- **Pages**: {pages}")
    if doi:
        meta_lines.append(f"- **DOI**: [{doi}](https://doi.org/{doi})")
    if publisher:
        meta_lines.append(f"- **Publisher**: {publisher}")
    if pdf_path:
        meta_lines.append(f"- **PDF Location**: `{pdf_path}`")
    meta_lines.append(f"- **Date Added**: {date_added}")
    metadata_block = "\n".join(meta_lines)

    # --- BibTeX fields (trailing comma on all but last) ---
    bib_fields = [("title", title), ("author", authors_bib)]
    if item_type == "bookSection":
        if journal:
            bib_fields.append(("booktitle", journal))
    else:
        if journal:
            bib_fields.append(("journal", journal))
    if year:
        bib_fields.append(("year", year))
    if volume:
        bib_fields.append(("volume", volume))
    if issue:
        bib_fields.append(("number", issue))
    if pages:
        bib_fields.append(("pages", pages))
    if doi:
        bib_fields.append(("doi", doi))
    if publisher:
        bib_fields.append(("publisher", publisher))

    bib_lines = "\n".join(
        f"  {k} = {{{v}}}{',' if i < len(bib_fields) - 1 else ''}"
        for i, (k, v) in enumerate(bib_fields)
    )

    # --- Assemble note using explicit line list ---
    # Empty strings in the list become blank lines when joined with "\n".
    parts = []
    parts.append(f"# {title}")
    parts.append("")
    parts.append("## Metadata")
    parts.append("")
    parts.append(metadata_block)
    parts.append("")
    parts.append("## BibTeX Citation")
    parts.append("")
    parts.append("```bibtex")
    parts.append(f"@{bibtex_type}{{{cite_key},")
    parts.append(bib_lines)
    parts.append("}")
    parts.append("```")
    parts.append("")
    parts.append("## Key Points")
    parts.append("")
    parts.append("- ")
    parts.append("")
    parts.append("## Notes")
    parts.append("")
    parts.append("")
    parts.append("")
    parts.append("## Related Papers")
    parts.append("")
    parts.append("- ")
    parts.append("")
    parts.append("## Tags")
    parts.append("")
    if tags_str:
        parts.append(tags_str)

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Writing notes
# ---------------------------------------------------------------------------

def write_note(output_folder, title, content, existing_lower):
    """Write note to file; return ('created', filename) or ('skipped', filename).

    Existence check is case-insensitive to handle hand-written note variants.
    """
    filename = sanitize_filename(title) + ".md"
    if filename.lower() in existing_lower:
        return ("skipped", filename)
    filepath = Path(output_folder) / filename
    filepath.write_text(content, encoding="utf-8")
    return ("created", filename)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    output_folder = Path(config["output_folder"])
    storage_path = config["zotero_storage_path"]

    if not output_folder.exists():
        print(
            f"Error: output folder does not exist: {output_folder}",
            file=sys.stderr,
        )
        sys.exit(1)

    zot = connect_zotero(config)
    print("Fetching items from Zotero...")
    parent_items, parent_to_attachments = fetch_all_items(zot)

    # Build case-insensitive set of existing note filenames once
    existing_lower = {f.name.lower() for f in output_folder.glob("*.md")}

    # Filter to content items only (skip attachment and note item types)
    skip_types = {"attachment", "note"}
    content_items = [
        item for item in parent_items
        if item.get("data", {}).get("itemType", "") not in skip_types
    ]

    total = len(content_items)
    created = 0
    skipped = 0

    for i, item in enumerate(content_items, 1):
        data = item.get("data", {})
        title = data.get("title", "").strip()
        item_key = item.get("key", "")

        if not title:
            print(f"[{i:3}/{total}] SKIP (no title)  key={item_key}")
            skipped += 1
            continue

        year = extract_year(data.get("date", ""))
        doi = extract_doi(data)
        attachments = parent_to_attachments.get(item_key, [])
        pdf_path = extract_pdf_path(attachments, storage_path)

        content = render_note(data, pdf_path, year, doi)
        status, filename = write_note(output_folder, title, content, existing_lower)

        label = "CREATED " if status == "created" else "skipped "
        print(f"[{i:3}/{total}] {label}  {filename}")

        if status == "created":
            created += 1
            existing_lower.add(filename.lower())
        else:
            skipped += 1

    print()
    print("=" * 50)
    print("  Done.")
    print(f"  Created:  {created:4}")
    print(f"  Skipped:  {skipped:4}  (note already existed)")
    print("=" * 50)


if __name__ == "__main__":
    main()
