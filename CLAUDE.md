# Obsidian-writer — Claude Context

## What This Project Does

A 4-script Python pipeline that reads the Zotero library (via the Zotero API)
and generates and enriches Obsidian markdown notes for research papers.

It is a companion tool to Zotero-sweep: Zotero-sweep imports PDFs into Zotero;
Obsidian-writer then creates and enriches Obsidian notes from what is in the library.

## Pipeline Scripts

Run in this order:

```bash
# Step 1 (optional) — review and delete bad Zotero imports
python step1_zotero_cleanup.py

# Step 2 — create notes for new Zotero items
python main.py

# Step 3 — populate Related Papers sections via Semantic Scholar
python step3_link_citations.py          # incremental (uses s2_cache.json)
python step3_link_citations.py --full   # re-query all papers

# Step 4 — generate Key Points for empty notes via Claude API
python step4_key_points.py --dry-run    # preview first
python step4_key_points.py              # then generate
```

All scripts are idempotent.

| Script | Sections it writes | Sections it never touches |
|--------|-------------------|--------------------------|
| `main.py` | Creates entire new note (all sections) | — |
| `step3_link_citations.py` | Related Papers (auto-links only) | Metadata, BibTeX, Key Points, Notes, Tags |
| `step4_key_points.py` | Key Points | Metadata, BibTeX, Notes, Related Papers, Tags |

## Key Behaviour Rules

- **Create only (main.py)** — if a note already exists for a paper, skip it. Never
  overwrite or modify existing notes.
- **Flat output folder** — all notes go into one directory:
  `/home/anand/Documents/Obsidian Vault/Papers/`
- **Note filename** — use the paper title as the filename: `{Title}.md`
- **Section protection** — Metadata, BibTeX, and Notes sections are never modified
  by any script. Steps 3 and 4 only touch their designated sections.
- **Auto-link convention** — links added by Step 3 carry an `<!--auto-->` suffix
  so they can be distinguished from user-written links and updated on subsequent runs.
  Example: `- [[Some Paper Title]] <!--auto-->`
- **No blind overwrites** — Steps 3 and 4 back up notes before writing.

## Note Format (Golden Standard)

All generated notes must follow this exact structure, based on the existing
hand-written notes in `/home/anand/Documents/Obsidian Vault/Papers/`:

```markdown
# {Title}

## Metadata

- **Authors**: [[First Last]], [[First Last]]
- **Journal**: {Journal name}  #{journal-tag if applicable}
- **Year**: {year}
- **Volume**: {volume}
- **Issue**: {issue}          ← omit line if not available
- **Pages**: {start}-{end}    ← omit line if not available
- **DOI**: [{doi}](https://doi.org/{doi})
- **Publisher**: {publisher}
- **PDF Location**: {path to linked PDF file, if available}
- **Date Added**: {date added to Zotero, YYYY-MM-DD}

## BibTeX Citation

```bibtex
@article{{CiteKey{year},
  title = {{Title}},
  author = {First Last and First Last},
  journal = {Journal},
  year = {year},
  volume = {volume},
  number = {issue},
  pages = {pages},
  doi = {doi},
  publisher = {publisher}
}}
```

## Key Points

-

## Notes



## Related Papers

-

## Tags


```

- Authors are written as Obsidian `[[WikiLinks]]`
- The Tags section is populated from Zotero tags (spaces→hyphens, digit-start tags skipped)
- Omit optional fields (Issue, Pages, PDF Location) if data is not available
  in Zotero rather than leaving them blank

## Data Source

Zotero library accessed via the pyzotero API (same credentials as
Zotero-sweep). Read `config.json` in this project for API key and user ID.
Never read or write `zotero.sqlite` directly.

## Backup Protocol

Steps 3 and 4 back up notes before writing:
- Backup location: `{output_folder}/.backup/{title}.{YYYYMMDD_HHMMSS}.md`
- Obsidian ignores `.backup/` by default (dot-prefix directory)
- Step 1 appends full item JSON to `cleanup_deletions.log` before any Zotero deletion

## Logging

All scripts log to `obsidian_writer.log` in the project directory:
- Console: INFO level
- File: DEBUG level
- `--verbose` flag switches console to DEBUG

## Approved Libraries

- `pyzotero` — Zotero API access
- `anthropic` — Claude API (Step 4 only)

All other network access uses `urllib.request` (stdlib). Do not introduce
other third-party libraries without asking first.

## Config Fields

```json
{
  "zotero_user_id": "...",
  "zotero_api_key": "...",
  "zotero_library_type": "user",
  "zotero_storage_path": "/home/.../Zotero/storage",
  "output_folder": "/home/.../Obsidian Vault/Papers/",

  "_comment_step1": "Step 1 — Zotero cleanup",
  "harvest_progress_path": "/home/.../Zotero-sweep/harvest_progress.md",
  "zotero_sweep_log": "/home/.../Zotero-sweep/logs/zotero_sweep.log",

  "_comment_step3": "Step 3 — Citation linking (Semantic Scholar)",
  "semantic_scholar_api_key": "",
  "s2_sleep_seconds": 1.0,

  "_comment_step4": "Step 4 — Key points generation (Claude API)",
  "anthropic_api_key": "...",
  "claude_model": "claude-haiku-4-5-20251001"
}
```

Each script validates only the fields it needs.

## Related Projects

- **Zotero-sweep** (`/home/anand/Git Projects/Zotero-sweep`) — imports PDFs
  into Zotero. Run that first before running Obsidian-writer.

## Paths

| What | Path |
|------|------|
| Obsidian vault | `/home/anand/Documents/Obsidian Vault/` |
| Papers folder | `/home/anand/Documents/Obsidian Vault/Papers/` |
| Golden example notes | `/home/anand/Documents/Obsidian Vault/Papers/*.md` |
| Zotero data | `/home/anand/Zotero/` |
| harvest_progress | `/home/anand/Git Projects/Zotero-sweep/harvest_progress.md` |
