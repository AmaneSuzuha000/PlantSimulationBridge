"""Build a queryable API index from the installed Plant Simulation help.

The help ships as ~7.9k HTML pages inside PlantSimulationENU.chm. Prose is
useless to a bridge; what a bridge needs is a machine-readable inventory of
every documented member: which class owns it, its exact syntax, its data type,
and whether it can be written or only read.

The help marks the three cases with sections that are reliable across pages:

  * an "Assignment" section          -> the member is writable
  * a "Return Value" section only    -> the member is readable, not writable
  * a parameter list in the syntax   -> the member is a method

An attribute's syntax line carries no assignment operator -- the assignment
form lives in the Assignment section -- which is why classification keys off
the sections rather than off the syntax text.

Usage:
    python tools/pshelp_index.py --help-root /tmp/pshelp --out api_index.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path

TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.DOTALL | re.IGNORECASE)
OWNER_RE = re.compile(r"^(?:Attributes|Methods)\s+of\s+(?:all\s+the\s+|the\s+|all\s+)?(.+)$", re.IGNORECASE)
DATATYPE_RE = re.compile(r"(?:has|have)\s+the\s+data\s+type\s+([A-Za-z]+)")

STOP_LINES = {"Parameters", "Return Value", "Example:", "Assignment", "Related Topics", "Note:", "Syntax:"}


def strip_tags(raw: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h\d|table)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _syntax_after(lines, index, limit=4):
    collected = []
    for line in lines[index + 1:]:
        if line in STOP_LINES:
            break
        collected.append(line)
        if len(collected) >= limit:
            break
    return " ".join(" ".join(collected).split())


def parse_page(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    title_match = TITLE_RE.search(raw)
    title = strip_tags(title_match.group(1)).strip() if title_match else path.stem
    body = strip_tags(raw)
    lines = body.splitlines()
    syntax_positions = [i for i, line in enumerate(lines) if line == "Syntax:" or line.startswith("Syntax:")]
    if not syntax_positions:
        return None
    read_syntax = _syntax_after(lines, syntax_positions[0])
    if not read_syntax:
        return None

    # The help is really two manuals. The main "Reference Help" and a separate
    # "Add-Ins Reference Help" (COM/C/DDE, Kanban, Database, genetic
    # algorithms, ValueStreamMapping, ActiveX, Gantt). Matching only the
    # former left every add-in page with no owner at all: 417 of 2,772 members.
    breadcrumb = next(
        (line for line in lines[:8]
         if line.startswith(("Reference Help >", "Add-Ins Reference Help >"))),
        "",
    )
    segments = [p.strip() for p in breadcrumb.replace("Reference Help >", "").split(">") if p.strip()]
    owner = ""
    for segment in segments:
        match = OWNER_RE.match(segment)
        if match:
            owner = match.group(1).strip()
            break
    if not owner and len(segments) >= 2:
        owner = segments[-2]

    writable = "Assignment" in body
    returns = "Return Value" in body
    is_method = bool(re.search(r"\([^)]*[:<]", read_syntax)) or "(<" in read_syntax
    if is_method:
        kind = "method"
    elif writable:
        kind = "attribute"
    elif returns:
        kind = "readonly"
    else:
        kind = "topic"

    write_syntax = ""
    if writable and len(syntax_positions) > 1:
        write_syntax = _syntax_after(lines, syntax_positions[1])

    datatype_match = DATATYPE_RE.search(body)
    category = re.match(r"([A-Za-z]+?)(?:\d|\.|$)", path.stem)
    return {
        "page": path.stem,
        "title": title,
        "category": category.group(1) if category else "",
        "owner": owner,
        "breadcrumb": breadcrumb,
        "kind": kind,
        "writable": writable,
        "returns": returns,
        "datatype": datatype_match.group(1).lower() if datatype_match else "",
        "syntax": read_syntax,
        "write_syntax": write_syntax,
    }


def build(help_root):
    entries = []
    for path in sorted(help_root.glob("*.html")):
        entry = parse_page(path)
        if entry:
            entries.append(entry)
    return {
        "help_root": str(help_root),
        "pages_scanned": len(list(help_root.glob("*.html"))),
        "members": len(entries),
        "kinds": dict(Counter(e["kind"] for e in entries)),
        "categories": dict(Counter(e["category"] for e in entries).most_common(20)),
        "owners": len({e["owner"] for e in entries if e["owner"]}),
        "top_owners": Counter(e["owner"] for e in entries if e["owner"]).most_common(40),
        "entries": entries,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--help-root", default="/tmp/pshelp")
    parser.add_argument("--out", default="api_index.json")
    args = parser.parse_args()
    result = build(Path(args.help_root))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("pages_scanned", "members", "kinds", "owners")}, indent=2))
    print("categories:", json.dumps(result["categories"], ensure_ascii=False))
    for owner, count in result["top_owners"][:30]:
        print(f"  {owner:<45} {count:d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
