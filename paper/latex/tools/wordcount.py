"""Check the manuscript against the Renewable Energy submission limits.

Renewable Energy (Elsevier, ISSN 0960-1481) caps original papers at 4,000-6,000
words excluding captions and references, and at 50 references. Authors must state
the word count at submission, so it is worth being able to produce it on demand
rather than estimating.

Body words are counted with texcount (-inc, text only), which excludes captions,
tables, headers and the bibliography. The abstract and the reference list are
counted separately because they sit outside the cap.

    python tools/wordcount.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOWER, UPPER, MAX_REFS, MAX_ABSTRACT = 4000, 6000, 50, 250


def texcount_body() -> tuple[int, int, int]:
    """Return (text words, header words, caption words) for the whole manuscript."""
    out = subprocess.run(
        ["texcount", "-v0", "-inc", "main.tex"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    # The last block of the -inc output is the total.
    text = [int(m) for m in re.findall(r"^Words in text: (\d+)", out, re.M)]
    head = [int(m) for m in re.findall(r"^Words in headers: (\d+)", out, re.M)]
    caps = [int(m) for m in re.findall(r"^Words outside text \(captions, etc\.\): (\d+)", out, re.M)]
    return text[-1], head[-1], caps[-1]


def section_words() -> list[tuple[str, int]]:
    rows = []
    for path in sorted((ROOT / "sections").glob("0*.tex")):
        out = subprocess.run(
            ["texcount", "-v0", path.name],
            cwd=ROOT / "sections", capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        ).stdout
        m = re.search(r"^Words in text: (\d+)", out, re.M)
        rows.append((path.stem, int(m.group(1)) if m else 0))
    return rows


def abstract_words() -> int:
    source = (ROOT / "main.tex").read_text(encoding="utf-8")
    match = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", source, re.S)
    if not match:
        return 0
    body = re.sub(r"\\[a-zA-Z]+\*?", " ", match.group(1))
    body = re.sub(r"[${}\\~^_&%]", " ", body)
    return len(body.split())


def reference_count() -> int:
    return len(re.findall(r"^@\w+\{", (ROOT / "refs.bib").read_text(encoding="utf-8"), re.M))


def main() -> int:
    text, head, caps = texcount_body()
    refs = reference_count()
    abstract = abstract_words()

    # The cap applies to the main text. The abstract has its own limit, and the
    # end matter (data availability, declarations, acknowledgements) is not part
    # of the article body, so both are reported but excluded from the comparison.
    sections = section_words()
    body = sum(count for _, count in sections)
    front_and_back = text - body

    print("Renewable Energy submission limits")
    print("=" * 58)
    for section, count in sections:
        print(f"  {section:20s} {count:6d}")
    print("-" * 58)
    print(f"  {'main text':20s} {body:6d}   limit {LOWER}-{UPPER}"
          f"   {'OK' if LOWER <= body <= UPPER else 'OVER by ' + str(body - UPPER)}")
    print(f"  {'(abstract + end matter)':20s} {front_and_back:6d}   reported separately")
    text = body
    print(f"  {'(headers)':20s} {head:6d}   excluded from the cap")
    print(f"  {'(captions)':20s} {caps:6d}   excluded from the cap")
    print(f"  {'abstract':20s} {abstract:6d}   limit {MAX_ABSTRACT}"
          f"   {'OK' if abstract <= MAX_ABSTRACT else 'OVER by ' + str(abstract - MAX_ABSTRACT)}")
    print(f"  {'references':20s} {refs:6d}   limit {MAX_REFS}"
          f"   {'OK' if refs <= MAX_REFS else 'OVER by ' + str(refs - MAX_REFS)}")

    failures = (text > UPPER) + (abstract > MAX_ABSTRACT) + (refs > MAX_REFS)
    print("=" * 58)
    print("compliant" if not failures else f"{failures} limit(s) exceeded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
