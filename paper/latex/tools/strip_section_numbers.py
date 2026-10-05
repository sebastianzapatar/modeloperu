"""Remove the manual S-numbers from supplementary subsection titles.

The supplementary sections are numbered S1..S6 by \\renewcommand, so a title
written as "S3.2 Parameter verification" renders as "S3.2 S3.2 Parameter
verification". This strips the manual prefix once; it is idempotent.

    python tools/strip_section_numbers.py
"""

from __future__ import annotations

import re
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "supplementary.tex"
PATTERN = re.compile(r"(\\subsection\{)S\d+(?:\.\d+)*\s+")


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    patched, count = PATTERN.subn(r"\1", text)
    TARGET.write_text(patched, encoding="utf-8")
    print(f"stripped {count} manual section numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
