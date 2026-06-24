"""Assemble the human maintenance manual from its source sections.

Reads every `docs/manual/NN_*.md` section (in sorted filename order) and produces:
  - docs/MANUAL.md   : one searchable file with a title block, build timestamp, an
                       auto-generated clickable Table of Contents, and all sections
                       joined by dividers. Print via VS Code "Markdown PDF" or pandoc.
  - docs/MANUAL.html  : (only if the `markdown` package is installed) a single
                       self-contained, print-styled file — open in a browser and
                       Print -> Save as PDF. Page break before each top-level section.

This is a docs-only build tool. It pulls in NO bot runtime code, and markdown/docs
changes do NOT trigger any service restart on the server (per the autopull changed-file
mapping), so committing the manual has zero deploy impact.

Usage:
    python scripts/build_manual.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "docs" / "manual"
OUT_MD = REPO_ROOT / "docs" / "MANUAL.md"
OUT_HTML = REPO_ROOT / "docs" / "MANUAL.html"
OUT_ACCESS = REPO_ROOT / "docs" / "ACCESS_SHEET.html"  # standalone, LANDSCAPE, for printing

TITLE = "Crypto Trading Fleet — Maintenance Manual"


def _slug(text: str, seen: dict[str, int]) -> str:
    """GitHub-style heading anchor: lowercase, strip punctuation, spaces->hyphens."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = s.strip("-")
    if s in seen:
        seen[s] += 1
        return f"{s}-{seen[s]}"
    seen[s] = 0
    return s


def _section_files() -> list[Path]:
    if not SRC_DIR.exists():
        print(f"ERROR: source dir not found: {SRC_DIR}", file=sys.stderr)
        sys.exit(2)
    # README.md is meta (how to edit) — not part of the assembled manual.
    files = sorted(p for p in SRC_DIR.glob("*.md") if p.name.lower() != "readme.md")
    if not files:
        print(f"ERROR: no section files in {SRC_DIR}", file=sys.stderr)
        sys.exit(2)
    return files


# Part banners inserted before the first section of each track (by filename leading digit).
TRACK_BANNERS = {
    "1": "Part 1 · Operator Track — Keeping It Alive (plain language)",
    "2": "Part 2 · Engineer Track — Understand & Continue (technical)",
    "3": "Part 3 · Reference",
}


def _label_first_h2(text: str, label: str) -> str:
    """Prefix the FIRST `## ` heading in a section with its section number (e.g. 1.1)."""
    out, done = [], False
    for line in text.split("\n"):
        if not done:
            m = re.match(r"^##\s+(.*\S)\s*$", line)
            if m:
                line = f"## {label} {m.group(1).strip()}"
                done = True
        out.append(line)
    return "\n".join(out)


def build_markdown(files: list[Path]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = []
    current_track: str | None = None

    for f in files:
        prefix = f.name[:2]
        track = prefix[0] if prefix.isdigit() else None
        # 10 -> "1.0", 11 -> "1.1", 34 -> "3.4"; front matter (0x) gets no number.
        label = f"{prefix[0]}.{prefix[1]}" if (track and track != "0") else None
        if track in ("1", "2", "3") and track != current_track:
            parts.append(f"# {TRACK_BANNERS[track]}")
            current_track = track
        raw = f.read_text(encoding="utf-8").rstrip("\n")
        if label:
            raw = _label_first_h2(raw, label)
        parts.append(raw)

    body = "\n\n---\n\n".join(parts)

    # Build the TOC from the assembled body, so Part banners and the numbered
    # section headings both appear (and anchors match the rendered headings).
    toc: list[str] = []
    seen: dict[str, int] = {}
    for line in body.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*\S)\s*$", line)
        if not m:
            continue
        level = len(m.group(1))
        heading = m.group(2).strip()
        anchor = _slug(heading, seen)
        indent = "  " * (level - 1)
        link = f"[{heading}](#{anchor})"
        toc.append(f"{indent}- " + (f"**{link}**" if level == 1 else link))

    header = (
        f"# {TITLE}\n\n"
        f"*Living document — rebuilt from `docs/manual/` by `scripts/build_manual.py`.*  \n"
        f"*Last built: {stamp}.*\n\n"
        "> **How to read this:** **Part 1 — Operator track (sections 1.x)** is plain-language,\n"
        "> for keeping the system alive day to day. **Part 2 — Engineer track (2.x)** is technical,\n"
        "> for understanding and changing it. **Part 3 — Reference (3.x)** holds tools, decisions,\n"
        "> troubleshooting, the access sheet, and the appendix. Use the table of contents\n"
        "> or Ctrl-F to jump around.\n\n"
        "---\n\n"
        "## Table of Contents\n\n"
        + "\n".join(toc)
        + "\n"
    )
    return header + "\n\n---\n\n" + body + "\n"


def build_html(markdown_text: str) -> str | None:
    try:
        import markdown  # type: ignore
    except Exception:
        return None
    html_body = markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )
    css = """
    @media print { h1, h2 { page-break-before: always; } h1:first-of-type { page-break-before: avoid; } a { color: inherit; text-decoration: none; } }
    .page-break { page-break-before: always; break-before: page; height: 0; }
    body { font-family: Georgia, 'Times New Roman', serif; max-width: 50rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
    h1, h2, h3 { font-family: -apple-system, Segoe UI, Arial, sans-serif; line-height: 1.25; }
    h1 { border-bottom: 2px solid #333; padding-bottom: .2em; }
    h2 { border-bottom: 1px solid #ccc; padding-bottom: .15em; margin-top: 1.6em; }
    code { background: #f3f3f3; padding: .1em .3em; border-radius: 3px; font-size: .9em; }
    pre { background: #f6f6f6; padding: .8em; border-radius: 5px; overflow-x: auto; }
    pre code { background: none; padding: 0; }
    table { border-collapse: collapse; width: 100%; font-size: .92em; }
    th, td { border: 1px solid #ccc; padding: .4em .6em; text-align: left; vertical-align: top; }
    th { background: #f0f0f0; }
    blockquote { border-left: 4px solid #bbb; margin: 1em 0; padding: .2em 1em; color: #444; background: #fafafa; }
    """
    return (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        f"<title>{TITLE}</title>\n<style>{css}</style>\n</head>\n<body>\n"
        f"{html_body}\n</body>\n</html>\n"
    )


def build_access_sheet_html() -> bool:
    """Render just the Access Sheet section as a standalone LANDSCAPE printable.

    Roy fills it by hand, so it needs room — landscape, wide table cells. Returns
    True if written (requires the optional `markdown` lib), False if skipped.
    """
    try:
        import markdown  # type: ignore
    except Exception:
        return False
    src = next((p for p in SRC_DIR.glob("*access_sheet*.md")), None)
    if src is None:
        return False
    html_body = markdown.markdown(
        src.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    css = """
    @page { size: A4 landscape; margin: 1cm; }
    body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 1rem; line-height: 1.4; color: #1a1a1a; }
    h2 { border-bottom: 2px solid #333; padding-bottom: .2em; }
    table { border-collapse: collapse; width: 100%; font-size: .82em; }
    th, td { border: 1px solid #999; padding: .4em .45em; text-align: left; vertical-align: top; }
    th { background: #eee; }
    /* Access-sheet tables: honor the colgroup widths, give blank cells writing room. */
    table.sheet { table-layout: fixed; }
    table.sheet td { height: 2.4em; word-wrap: break-word; overflow-wrap: break-word; }
    .page-break { page-break-before: always; break-before: page; height: 0; }
    blockquote { border-left: 4px solid #c0392b; background: #fdf3f2; margin: 1em 0; padding: .4em 1em; }
    code { background: #f3f3f3; padding: .1em .3em; border-radius: 3px; }
    """
    OUT_ACCESS.write_text(
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        f"<title>Access Sheet</title>\n<style>{css}</style>\n</head>\n<body>\n"
        f"{html_body}\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    files = _section_files()
    md = build_markdown(files)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"Built {OUT_MD.relative_to(REPO_ROOT)}  ({len(files)} sections, {md.count(chr(10))+1} lines)")

    html = build_html(md)
    if html is not None:
        OUT_HTML.write_text(html, encoding="utf-8")
        print(f"Built {OUT_HTML.relative_to(REPO_ROOT)}  (open in browser -> Print -> Save as PDF)")
    else:
        print("Skipped HTML (optional): `pip install markdown` to enable, "
              "or render the .md with `pandoc docs/MANUAL.md -o MANUAL.pdf`.")

    if build_access_sheet_html():
        print(f"Built {OUT_ACCESS.relative_to(REPO_ROOT)}  (LANDSCAPE — print this to fill by hand)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
