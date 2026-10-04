"""Execute the notebook headlessly, in place.

    python tools/run_notebook.py

Useful both as a smoke test of the whole analysis path and as the way the
executed notebook - figures and tables rendered inline - is produced for the paper.
"""

from __future__ import annotations

import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "notebooks" / "Peru_expansion_uncertainty.ipynb"


def main() -> int:
    notebook = nbformat.read(TARGET, as_version=4)
    client = NotebookClient(notebook, timeout=3600, kernel_name="python3",
                            resources={"metadata": {"path": str(ROOT)}})
    try:
        client.execute()
    finally:
        nbformat.write(notebook, str(TARGET))
    failures = [(i, o) for i, c in enumerate(notebook.cells)
                for o in c.get("outputs", []) if o.get("output_type") == "error"]
    for index, output in failures:
        print(f"\ncell {index} raised {output['ename']}: {output['evalue']}", file=sys.stderr)
        print("\n".join(output.get("traceback", []))[-2500:], file=sys.stderr)
    if failures:
        return 1
    print(f"notebook executed cleanly -> {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
