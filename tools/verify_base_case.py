"""Check that the published model reproduces the documented base case.

The sweep reuses the base run from the three-factor model, on the argument that a
sensitivity knob at zero reproduces the original equation exactly. The argument is
sound - each knob is a multiplier whose default value is the identity - but it is
an argument, not a measurement, and it had never been tested for this particular
publish. A publish picks up whatever workbook Vensim had open at the time, so a
mis-published model would shift every number in the study while still running
perfectly happily.

This simulates the base case on the published four-knob model and compares it with
the cached base run and with the reference values of the study.

Run it on an otherwise idle machine: it starts a seventh Vensim engine, and running
one alongside a six-worker sweep is enough to push the machine into paging.

    python tools/verify_base_case.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import peru_tornado as pt

#: The base case of the study, as documented in README section 2.
REFERENCE = {"Total Generation Capacity": 63.85, "Total Transmission": 46_341.0}
TOLERANCE = {"Total Generation Capacity": 0.05, "Total Transmission": 50.0}


def main() -> int:
    pt.select_factors(True)
    model = pt.VENSIM_MODEL_4F
    if not model.exists():
        print(f"{model.name} not found; publish it first (README section 6)", file=sys.stderr)
        return 2

    # A fresh directory per invocation: Vensim keeps the model file it loaded
    # open, so reusing one name fails to overwrite it if an earlier check is
    # still winding down.
    sandbox = ROOT / "results" / f"_basecheck_{int(time.time())}"
    sandbox.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model, sandbox / "m.vpmx")
    shutil.copy2(pt.DATA_XLSX, sandbox / "PERU.xlsx")

    dll = ctypes.WinDLL(ctypes.util.find_library("vendll64"))
    dll.vensim_command.argtypes = [ctypes.c_char_p]
    dll.vensim_command.restype = ctypes.c_int
    dll.vensim_get_data.argtypes = [ctypes.c_char_p] * 3 + [
        ctypes.POINTER(ctypes.c_float)] * 2 + [ctypes.c_int]
    dll.vensim_get_data.restype = ctypes.c_int
    dll.vensim_be_quiet(2)

    (sandbox / "p.lst").write_text("\n".join(REFERENCE) + "\n", encoding="utf-8")
    # The base case is every factor at zero change, which for generation delays means
    # the workbook's own -0.2 rather than 0; _params applies that conversion.
    design = pt.design_runs()
    params = pt._params(design.loc["base"])
    print("base-case knob values:", {k: round(v, 4) for k, v in params.items()})

    started = time.perf_counter()
    commands = [
        f"SPECIAL>LOADMODEL|{sandbox / 'm.vpmx'}",
        f"SIMULATE>SAVELIST|{sandbox / 'p.lst'}",
        f"SIMULATE>SETVAL|FINAL TIME={pt.FINAL_TIME:g}",
        *[f"SIMULATE>SETVAL|{k}={v:.10g}" for k, v in params.items()],
        f"SIMULATE>RUNNAME|{sandbox / 'bc'}",
        "MENU>RUN|o",
    ]
    for command in commands:
        if not dll.vensim_command(command.encode()):
            print(f"Vensim rejected: {command}", file=sys.stderr)
            return 1
    print(f"simulated in {(time.perf_counter() - started) / 60:.1f} min")

    def final(variable: str, n: int = 8192) -> float:
        values, times = (ctypes.c_float * n)(), (ctypes.c_float * n)()
        count = dll.vensim_get_data(
            str(sandbox / "bc.vdfx").encode(), variable.encode(), b"Time", values, times, n)
        if count <= 0:
            raise KeyError(variable)
        return float(np.array(values[:count])[-1])

    cached = pd.read_csv(ROOT / "results" / "peru" / "runs" / "base.csv")
    ok = True
    print(f"\n{'variable':32s} {'published':>14s} {'cached run':>14s} {'reference':>12s}")
    for variable, reference in REFERENCE.items():
        published = final(variable)
        cached_value = float(cached[variable].iloc[-1])
        print(f"{variable:32s} {published:14,.2f} {cached_value:14,.2f} {reference:12,.2f}")
        if abs(published - reference) > TOLERANCE[variable]:
            print(f"   MISMATCH against the reference by {published - reference:+,.2f}")
            ok = False
        if abs(published - cached_value) > TOLERANCE[variable]:
            print(f"   MISMATCH against the cached run by {published - cached_value:+,.2f}")
            ok = False
    dll.vensim_command(b"SPECIAL>CLEARRUNS")

    print("\nVERDICT:", "the published four-knob model reproduces the base case"
          if ok else "the published model does NOT reproduce the base case")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
