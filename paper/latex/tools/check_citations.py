"""Audit the manuscript's citations against refs.bib.

Two failure modes matter and neither is caught by a successful LaTeX build once
the .bbl is stale: a key cited but absent from the bibliography, and an entry in
the bibliography that nothing cites. The first produces a "?" in the PDF; the
second is padding, which a reviewer will notice.

    python tools/check_citations.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CITE = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}")
ENTRY = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")


def main() -> int:
    sources = [ROOT / "main.tex", ROOT / "supplementary.tex"]
    sources += sorted((ROOT / "sections").glob("*.tex"))
    text = "".join(p.read_text(encoding="utf-8") for p in sources if p.exists())

    used: set[str] = set()
    for group in CITE.findall(text):
        used.update(k.strip() for k in group.split(",") if k.strip())

    bib = (ROOT / "refs.bib").read_text(encoding="utf-8")
    defined = set(ENTRY.findall(bib))
    # Entries whose metadata the co-authors still have to complete are flagged in
    # refs.bib with a note; they are reported separately rather than counted clean.
    unresolved = set(re.findall(r"@\w+\{([^,]+),[^@]*to be completed", bib))

    missing = sorted(used - defined)
    uncited = sorted(defined - used)

    print(f"cited in the manuscript : {len(used)}")
    print(f"defined in refs.bib     : {len(defined)}")
    print(f"  of which unresolved   : {len(unresolved)} "
          f"({', '.join(sorted(unresolved)) if unresolved else 'none'})")
    print(f"cited but not defined   : {missing or 'none'}")
    print(f"defined but not cited   : {uncited or 'none'}")

    renewable = len(re.findall(r"j\.renene", bib))
    print(f"Renewable Energy entries: {renewable}")

    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
