# How to maintain THIS manual

This folder holds the **source** of the project's human maintenance manual. The sections
are plain markdown, ordered by their numeric filename prefix. A build script stitches
them into one searchable/printable document.

## Editing

1. Edit the relevant section file (`NN_*.md`). Keep the **audience** of its track in mind:
   - `0x` = front matter / index
   - `1x` = **Operator track** — plain language, for a non-technical maintainer
   - `2x` = **Engineer track** — technical, for a software engineer
   - `3x` = **Reference** — tools, decisions, troubleshooting, access sheet, appendix
2. Update the `_Last reviewed: YYYY-MM-DD_` line at the top of any section you touch.
3. Rebuild:
   ```
   python scripts/build_manual.py
   ```
   This writes `docs/MANUAL.md` (searchable; print via VS Code "Markdown PDF" or
   `pandoc docs/MANUAL.md -o MANUAL.pdf`) and, if `pip install markdown` is present,
   `docs/MANUAL.html` (open in a browser → Print → Save as PDF; clean page breaks).
4. Commit the source **and** the rebuilt `docs/MANUAL.md` / `docs/MANUAL.html`.

Docs-only changes do **not** restart any service on the server (per the autopull
changed-file mapping), so committing the manual has zero deploy impact.

## Rules

- **No secrets in git.** `33_access_sheet.md` is a blank template — passwords are filled
  by hand after printing, never committed. The env-var reference lists names + purpose
  only, never values.
- Prefer **linking** to the deeper docs (`OPERATIONS.md`, `OPS_CHEATSHEET.md`,
  `design_state_2026-04-26.md`, the `memory/` files) over copying them — this manual is a
  curator/front-door, not a duplicate.
- When the system changes, update the section AND rebuild in the same commit.
