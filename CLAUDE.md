# Obsidian-writer — Claude Context

## What This Project Does

A standalone Python script that reads the Zotero library (via the Zotero API)
and generates Obsidian markdown notes for research papers — one note per paper.

It is a companion tool to Zotero-sweep: Zotero-sweep imports PDFs into Zotero;
Obsidian-writer then creates Obsidian notes from what is in the library.

## Key Behaviour Rules

- **Create only** — if a note already exists for a paper, skip it. Never
  overwrite or modify existing notes.
- **Flat output folder** — all notes go into one directory:
  `/home/anand/Documents/Obsidian Vault/Papers/`
- **Note filename** — use the paper title as the filename: `{Title}.md`

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
- The Tags section is left empty — the user fills in tags manually
- Omit optional fields (Issue, Pages, PDF Location) if data is not available
  in Zotero rather than leaving them blank

## Data Source

Zotero library accessed via the pyzotero API (same credentials as
Zotero-sweep). Read `config.json` in this project for API key and user ID.
Never read or write `zotero.sqlite` directly.

## Approved Libraries

- `pyzotero` — Zotero API access

Do not introduce other third-party libraries without asking first.

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
