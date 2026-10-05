# -*- coding: utf-8 -*-
"""
Tornado + uncertainty (P10-P90) analysis for the Peru power-system
System Dynamics model (ModeloCh4_Correccion.mdl + PERU.xlsx).

Uncertain factors (all relative to the base case):
    * Generation-plant delays  (licensing + construction)          +/- GEN_DELAY_RANGE
    * Transmission delays       (tender, investor selection,
                                 licensing, construction)          +/- TRANS_DELAY_RANGE
    * Fuel prices               (Fuel Cost of every technology)    +/- FUEL_RANGE

Outputs evaluated:
    * Total transmission (km)                   -> Total Transmission
    * Total transmission capacity (GW)          -> Total Interconnection Capacity / 1000
    * Total installed generation capacity (GW)  -> Total Generation Capacity
    * Country average electricity price         -> mean over active zones of
      (USD/MWh)                                     Electricity Tariff[Regulated, Zone]

Steps:
    1. Builds a copy of the model with three sensitivity "knobs"
       (Sens Gen Delay, Sens Trans Delay, Sens Fuel Price) and translates it with PySD.
    2. Runs: base, one-at-a-time low/high runs (tornado) and a Latin-Hypercube
       Monte Carlo sample (P10-P90 distribution). Runs are executed in parallel
       and cached in RESULTS_DIR/runs, so the script can be stopped and resumed.
    3. Picks the best and worst Monte Carlo scenario and makes the charts
       (in English) and an explanatory summary table (Excel + PNG).

Usage:
    python tornado_peru.py                # run everything (missing runs only)
    python tornado_peru.py --plots-only   # only rebuild charts/tables from cached runs
    python tornado_peru.py --samples 60   # change Monte Carlo sample size

Requirements:  pip install pysd pandas numpy matplotlib openpyxl scipy
"""

import os
import platform
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")   # one thread per simulation process

import argparse
import re
import shutil
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "model"
MODEL_MDL = MODEL_DIR / "ModeloCh4_Correccion.mdl"
DATA_XLSX = MODEL_DIR / "PERU.xlsx"
# The published model the DLL executes. ModeloCh4_Sens.vpmx carries the three
# original knobs; ModeloCh4_Sens2.vpmx adds the demand knob and is used whenever
# it exists (see README section "Adding the demand factor").
VENSIM_MODEL_3F = MODEL_DIR / "ModeloCh4_Sens.vpmx"
VENSIM_MODEL_4F = MODEL_DIR / "ModeloCh4_Sens2.vpmx"
VENSIM_MODEL = VENSIM_MODEL_3F
RESULTS_DIR = BASE_DIR / "results" / "peru"

GEN_DELAY_RANGE = 0.20      # +/-20 % generation delays (same as "Delays Gen +/-0.2")
TRANS_DELAY_RANGE = 0.20    # +/-20 % transmission delays (same as "Delays Trans +/-0.2")
FUEL_RANGE = 0.15           # +/-15 % fuel prices
DEMAND_RANGE = 0.20         # +/-20 % on the 2050 demand level (growth path)

# Monte Carlo design, declared as a list of Latin-hypercube blocks rather than one
# flat sample count. Each block is a proper Latin hypercube and the union of the
# blocks keeps uniform marginals, so a smaller design stays an exact prefix of a
# larger one: adding a block extends the study without invalidating a single
# completed run, and dropping one shortens it the same way. The sampler draws one
# column per factor in the order of ALL_FACTORS, so the block below is identical
# to the 120-sample design of the October 2026 study on the three factors it
# shares with it.
#
# To extend to 200 samples later, append (80, 2027) - nothing already simulated
# has to be re-run.
MC_BLOCKS = [(120, 2026)]
N_SAMPLES = sum(n for n, _ in MC_BLOCKS)
SEED = 2026
N_WORKERS = max(1, (os.cpu_count() or 2) // 2)   # parallel simulations (~ physical cores)

START_DATE = "2025-01-01"   # model Time = 0
FINAL_TIME = 300            # months (2050-01)
PRICE_WINDOW = 12           # price KPI = mean of the last 12 months of the run
BEST_WORST_CRITERION = "price"   # "price" (lowest/highest price) or "composite"

# Sensitivity knobs added to the model copy
KNOB_GEN, KNOB_TRANS, KNOB_FUEL = "Sens Gen Delay", "Sens Trans Delay", "Sens Fuel Price"
KNOB_DEMAND = "Sens Demand"

# key: (knob, half-range, label). The demand factor is only in play when the
# published model exposes its knob; FACTORS is narrowed by select_factors().
# The ORDER matters and must not be changed: the Latin hypercube assigns its
# columns in this order, so reordering the dict silently produces a different
# design and invalidates every completed run. Demand is appended last precisely
# so that dropping it leaves the original three-factor design bit-for-bit intact.
ALL_FACTORS = {
    "gen_delay": (KNOB_GEN, GEN_DELAY_RANGE, "Generation project delays"),
    "trans_delay": (KNOB_TRANS, TRANS_DELAY_RANGE, "Transmission project delays"),
    "fuel": (KNOB_FUEL, FUEL_RANGE, "Fuel prices"),
    "demand": (KNOB_DEMAND, DEMAND_RANGE, "Demand growth"),
}
FACTORS = dict(ALL_FACTORS)


def select_factors(with_demand):
    """Fix the factor set for this invocation.

    The design, the run ids and the cached-run check all depend on which factors
    are in play, so this is decided once, up front, and printed.
    """
    global FACTORS
    FACTORS = {k: v for k, v in ALL_FACTORS.items() if with_demand or k != "demand"}
    return FACTORS

# Model variables saved for every run
SAVE_VARS = [
    "Total Generation Capacity", "Generation Capacity by Tech",
    "Generation by Tech per Month", "Total Transmission",
    "Transmission for Interconnection km", "Total Transmission for Generation Connection",
    "Total Transmission for Demand Connection", "Total Interconnection Capacity",
    "Electricity Tariff", "Generation Tariff", "Transmission Tariff",
    "Congestion Tariff", "National Peak Demand",
]

# KPIs: key -> (label, unit)
KPIS = {
    "trans_km": ("Total transmission", "km"),
    "trans_gw": ("Total transmission capacity", "GW"),
    "gen_gw": ("Total installed generation capacity", "GW"),
    "price": ("Country average electricity price", "USD/MWh"),
}

# Technology groups (max. 8 series per stacked chart) – model suffix -> English label
TECH_GROUPS = {
    "HydroDam": "Hydro (reservoir)",
    "HRoR": "Hydro (run-of-river)",
    "Thermo2": "Thermal 2",
    "Thermo1": "Thermal 1",
    "WindOnS": "Onshore wind",
    "PV": "Solar PV",
    "PVBESSOp1": "Solar PV + BESS",
    "PVBESSOp2": "Solar PV + BESS",
}
OTHER_TECH = "Other (biomass, geothermal, offshore wind, DER)"

# Validated categorical palette (fixed order, never cycled)
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
           "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#fcfcfb"
SCEN_COLORS = {"Base": PALETTE[0], "Best": PALETTE[2], "Worst": PALETTE[1]}
TECH_COLORS = dict(zip(list(dict.fromkeys(TECH_GROUPS.values())) + [OTHER_TECH], PALETTE))


# ---------------------------------------------------------------------------
# 1. MODEL PREPARATION
# ---------------------------------------------------------------------------
def _replace_equation(txt, lhs, new_rhs, keep_as=None):
    """Replace the right-hand side of 'lhs=' in the .mdl text.
    If keep_as is given, the original equation is kept under that new name."""
    pat = re.compile(r"(\n" + re.escape(lhs) + r"\s*=)(.*?)(\n\s*~)", re.S)
    m = pat.search(txt)
    if not m:
        raise ValueError(f"Equation not found in model: {lhs}")
    extra = ""
    if keep_as:
        extra = f"\n{keep_as}={m.group(2)}\n\t~\t\n\t~\tOriginal data (tornado script)\n\t|\n"
    txt = txt[:m.start()] + m.group(1) + "\n\t" + new_rhs + m.group(3) + txt[m.end():]
    return txt, extra


def _delay_material_to_fixed(txt):
    """PySD does not implement DELAY MATERIAL. All delay times in this model are
    constants, so DELAY MATERIAL(in, delay, init, missing) == DELAY FIXED(in, delay, init)."""
    out, i = [], 0
    for m in re.finditer(r"DELAY MATERIAL\s*\(", txt):
        out.append(txt[i:m.start()])
        j, depth, commas = m.end(), 1, []
        while depth:
            c = txt[j]
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif c == "," and depth == 1:
                commas.append(j)
            j += 1
        out.append("DELAY FIXED(" + txt[m.end():commas[2]] + ")")
        i = j
    out.append(txt[i:])
    return "".join(out)


def add_knobs(txt):
    """Adds the three sensitivity constants to the .mdl text (base value 0 = original model)."""
    extras = []
    # Fuel prices
    txt, e = _replace_equation(txt, "Fuel Cost[ZTech]",
                               f"Fuel Cost Data[ZTech]*(1+{KNOB_FUEL})", keep_as="Fuel Cost Data[ZTech]")
    extras.append(e)
    # Generation delays: same mechanism used in PERU.xlsx ("Other delays" = f x (construction + licensing))
    txt, _ = _replace_equation(txt, "Other Generation delays[ZTech]",
                               f"{KNOB_GEN}*(Construction time by tech[ZTech]+Generation License Delay[ZTech])")
    # Transmission delays: every project stage is scaled by (1+f)
    for lhs in ["Public Tender Delay TI", "Investor Selection Delay TI[TrasmTech]",
                "Transmission License Delay TI[TrasmTech]", "Transmission Construction Delay TI[TrasmTech]",
                "Public Tender Delay TG", "Investor Selection Delay TG",
                "Transmission License Delay TG", "Transmission Construction Delay TG"]:
        name, sub = (lhs.split("[", 1) + [""])[:2]
        sub = "[" + sub if sub else ""
        txt, e = _replace_equation(txt, lhs, f"{name} Data{sub}*(1+{KNOB_TRANS})", keep_as=f"{name} Data{sub}")
        extras.append(e)
    # Demand: a growth-path factor rather than a level shift. The multiplier is 1
    # at Time = 0 and 1 + Sens Demand at the end of the horizon, so January 2025
    # demand is untouched and the factor varies only how fast demand grows -
    # which is what "an increase in demand" means for an expansion study.
    txt, e = _replace_equation(
        txt, "National demand[ForecastYear,Hour]",
        f"National demand Data[ForecastYear,Hour]*(1+{KNOB_DEMAND}*Time/FINAL TIME)",
        keep_as="National demand Data[ForecastYear,Hour]")
    extras.append(e)
    for knob in (KNOB_GEN, KNOB_TRANS, KNOB_FUEL, KNOB_DEMAND):
        extras.append(f"\n{knob}=\n\t0\n\t~\tDmnl\n\t~\tSensitivity factor (tornado script)\n\t|\n")
    k = txt.index("\\\\\\---///")
    return txt[:k] + "".join(extras) + "\n" + txt[k:]


def write_vensim_model():
    """Write ModeloCh4_Sens2.mdl: the original model plus the four sensitivity knobs.

    This is the file a human has to open in Vensim DSS and publish as
    ModeloCh4_Sens2.vpmx. Publishing cannot be automated: the Vensim DLL loads
    published models only, and it bakes the workbook data into the .vpmx at
    publish time, which is precisely why the factors are driven through named
    constants and SETVAL rather than by rewriting PERU.xlsx.

    DELAY MATERIAL is left alone here - that is only rewritten for the PySD path.
    """
    txt = MODEL_MDL.read_text(encoding="utf-8", errors="replace")
    txt = add_knobs(txt.replace("'?Datos'", "'PERU.xlsx'"))
    out = MODEL_DIR / "ModeloCh4_Sens2.mdl"
    out.write_text(txt, encoding="utf-8")
    return out


def build_model(build_dir):
    """Creates the sensitivity copy of the model and translates it with PySD."""
    import openpyxl
    build_dir.mkdir(parents=True, exist_ok=True)
    mdl_out, py_out = build_dir / "peru_sens.mdl", build_dir / "peru_sens.py"
    xlsx_out = build_dir / "PERU.xlsx"
    # rebuilt when the .mdl or PERU.xlsx change (delete the _model folder to force it)
    src_time = max(MODEL_MDL.stat().st_mtime, DATA_XLSX.stat().st_mtime)
    if py_out.exists() and py_out.stat().st_mtime > src_time:
        return py_out
    print("Preparing model copy ...")
    shutil.copy2(DATA_XLSX, xlsx_out)
    txt = MODEL_MDL.read_text(encoding="utf-8", errors="replace")
    head, rest = txt.split("\n", 1)
    txt = head + "\n" + rest.replace("{UTF-8}", "")          # stray header inside the file
    txt = txt.replace("'?Datos'", "'PERU.xlsx'")               # Vensim indirect file reference
    sheets = {s.lower(): s for s in openpyxl.load_workbook(DATA_XLSX, read_only=True).sheetnames}
    txt = re.sub(r"('PERU\.xlsx',\s*')([^']*)'",               # PySD sheet names are case-sensitive
                 lambda m: m.group(1) + sheets.get(m.group(2).lower(), m.group(2)) + "'", txt)

    txt = _delay_material_to_fixed(add_knobs(txt))
    mdl_out.write_text(txt, encoding="utf-8")

    print("Translating model with PySD (a few minutes) ...")
    _apply_pysd_patches()
    import pysd
    pysd.read_vensim(str(mdl_out), split_views=False, initialize=False)
    return py_out


def _allocate_by_priority_np(req, pri, width, supply):
    """Vectorised version of PySD's ALLOCATE BY PRIORITY (same algorithm as
    pysd.py_backend.allocation._allocate_by_priority_1d, solved for all rows at
    once). req, pri: (..., N); width, supply: (...). Negative values -> 0 (Vensim)."""
    req = np.maximum(np.asarray(req, float), 0)
    shape = req.shape
    N = shape[-1]
    req = req.reshape(-1, N)
    B = req.shape[0]
    pri = np.broadcast_to(np.asarray(pri, float), shape).reshape(B, N)
    w = np.broadcast_to(np.asarray(width, float), shape[:-1]).reshape(B)
    S = np.maximum(np.broadcast_to(np.asarray(supply, float), shape[:-1]).reshape(B), 0)
    result = np.zeros((B, N))
    full = S >= req.sum(1)
    result[full] = req[full]
    idx = np.where((~full) & (S > 0))[0]
    if not len(idx):
        return result.reshape(shape)
    r, p, ww, rem = req[idx], pri[idx], w[idx], S[idx].copy()
    b = len(idx)
    is0 = r == 0
    order = np.argsort(np.where(is0, np.inf, -p), axis=1, kind="stable")
    rs = np.take_along_axis(r, order, 1)
    ps = np.take_along_axis(p, order, 1)
    nnz = (~is0).sum(1)
    dist = np.full((b, N), np.nan)
    if N > 1:
        d = np.minimum(-np.diff(ps, axis=1) / ww[:, None], 1) * rs[:, :-1]
        dist[:, :-1] = np.where(np.arange(N - 1)[None, :] < (nnz - 1)[:, None], d, np.nan)
    out = np.zeros((b, N))
    active = np.zeros((b, N), bool)
    active[:, 0] = True
    c = np.zeros(b, int)
    live = rem > 0
    for _ in range(4 * N + 20):
        if not live.any():
            break
        L = np.where(live)[0]
        a = active[L]
        sl = rs[L] * a
        ssum = sl.sum(1)
        ok = ssum > 0
        sl = np.where(ok[:, None], sl / np.where(ok, ssum, 1)[:, None], 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            q = np.where(a & (sl > 0), (rs[L] - out[L]) / sl, np.inf)
            dx_top = q.min(1)
            dx_top = np.where(np.isinf(dx_top), np.nan, dx_top)
            cL = c[L]
            dx_start = (dist[L, cL] - out[L, cL]) / sl[np.arange(len(L)), cL]
        cand = np.stack([dx_top, dx_start, rem[L]], 1)
        dx = np.where(np.isnan(cand), np.inf, cand).min(1)
        dx = np.where(np.isfinite(dx), dx, rem[L])
        out[L] += sl * dx[:, None]
        start = np.isclose(dx, dx_start, rtol=1e-10, atol=1e-16) & ~np.isnan(dx_start)
        cn = np.minimum(cL + start, N - 1)
        c[L] = cn
        active[L[start], cn[start]] = True
        top = dx == dx_top
        if top.any():
            Lt = L[top]
            active[Lt] &= ~(out[Lt] >= rs[Lt])
        rem[L] -= dx
        live[L] = (rem[L] > 0) & ok
    unsorted = np.zeros((b, N))
    np.put_along_axis(unsorted, order, out, 1)
    result[idx] = unsorted
    return result.reshape(shape)


def _apply_pysd_patches():
    """Compatibility / speed patches so PySD reproduces Vensim behaviour:
    - ALLOCATE BY PRIORITY: vectorised (PySD's loops element by element, far too slow
      for this model) and negative supply/requests treated as zero, like Vensim.
    - DELAY FIXED: support delay times that are arrays (per technology/zone)."""
    import xarray as xr
    import pysd.py_backend.allocation as al
    import pysd.py_backend.statefuls as st
    if getattr(al, "_peru_patched", False):
        return

    def allocate_by_priority(request, priority, width, supply):
        bd = request.dims[:-1]
        template = request.isel({request.dims[-1]: 0}, drop=True)

        def batch(x):
            return x.broadcast_like(template).transpose(*bd).values if isinstance(x, xr.DataArray) else x
        pri = priority.broadcast_like(request).transpose(*request.dims).values \
            if isinstance(priority, xr.DataArray) else priority
        vals = _allocate_by_priority_np(request.values, pri, batch(width), batch(supply))
        return xr.DataArray(vals, request.coords, request.dims)
    al.allocate_by_priority = allocate_by_priority

    class DelayFixedArray(st.DelayFixed):
        def initialize(self, init_val=None):
            order = np.maximum(np.round(self.delay_time_func() / self.tstep() + 1e-6), 1)
            if isinstance(order, xr.DataArray):
                order = order.astype(int)
                self.uniq = [int(o) for o in np.unique(order.values)]
                self.order_da = order if len(self.uniq) > 1 else None
            else:
                self.uniq, self.order_da = [int(order)], None
            self.order = max(self.uniq)
            self.init = self.init_func() if init_val is None else init_val
            self.state, self.hist, self.n = self.init, [], 0

        def update(self, state):
            self.hist.append(self.input_func())
            self.n += 1
            if len(self.hist) > self.order:
                self.hist.pop(0)

            def get(o):
                return self.hist[-o] if self.n >= o else self.init
            if self.order_da is None:
                self.state = get(self.uniq[0])
            else:
                res = None
                for o in self.uniq:
                    v = get(o)
                    res = v if res is None else xr.where(self.order_da == o, v, res)
                self.state = res

        def export(self):
            return {"state": self.state}
    st.DelayFixed = DelayFixedArray
    al._peru_patched = True


# ---------------------------------------------------------------------------
# 2. EXPERIMENT DESIGN AND SIMULATION
# ---------------------------------------------------------------------------
def design_runs(n_samples=None, blocks=None):
    """Build the experiment: one base run, two tornado runs per factor, and the
    Monte Carlo sample.

    The tornado runs move one factor at a time to each end of its range with the
    others at their base value, which is what makes a tornado bar readable as
    "the effect of this factor alone". The Monte Carlo runs move every factor at
    once, which is what the P10-P90 band and the rank correlations need.

    The Monte Carlo sample is a Latin hypercube built in blocks (MC_BLOCKS).
    Within a block each factor's range is cut into as many equal intervals as
    there are samples and exactly one value is drawn at random inside each, then
    the intervals of the factors are paired at random. Blocks exist so that an
    earlier, smaller design stays a prefix of a larger one and its completed runs
    can be reused; each block is seeded separately so the whole design is
    reproducible from this file alone.
    """
    keys = list(FACTORS)
    zero = {k: 0.0 for k in keys}
    runs = [dict(run_id="base", kind="base", **zero)]
    for key, (_, rng, _) in FACTORS.items():
        for side, val in (("low", -rng), ("high", rng)):
            r = dict(run_id=f"oat_{key}_{side}", kind="tornado", **zero)
            r[key] = val
            runs.append(r)

    blocks = blocks or MC_BLOCKS
    if n_samples is not None and n_samples != sum(n for n, _ in blocks):
        blocks = [(n_samples, SEED)]
    index = 0
    for size, seed in blocks:
        rs = np.random.default_rng(seed)
        u = np.empty((size, len(keys)))
        for j in range(len(keys)):
            u[:, j] = (rs.permutation(size) + rs.random(size)) / size
        for i in range(size):
            r = dict(run_id=f"mc_{index:03d}", kind="montecarlo")
            for j, key in enumerate(keys):
                rng = FACTORS[key][1]
                r[key] = round(-rng + 2 * rng * u[i, j], 4)
            runs.append(r)
            index += 1
    return pd.DataFrame(runs).set_index("run_id")


_MODEL = None


class VensimDLL:
    """Runs the published model (.vpmx) with the Vensim DLL (needs Vensim DSS installed).
    Each worker process works in its own temporary folder with its own copy of the model
    and of PERU.xlsx, so several simulations can run in parallel."""
    DLL = r"C:\Windows\System32\vendll64.dll"
    _TECH = ["TBiomass", "TWindOnS", "TWindOffS", "TPV", "THydroDam", "THRoR", "TThermo1", "TThermo2",
             "TGeothermal", "TPVBESSOp1", "TPVBESSOp2", "TOther1", "TOther2", "TOther3"]
    _ZONES = [f"Z{i}" for i in range(1, 7)]

    def __init__(self, vpmx):
        import ctypes
        import tempfile
        self.dir = Path(tempfile.gettempdir()) / "peru_vensim" / f"w{os.getpid()}"
        self.dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vpmx, self.dir / "model.vpmx")
        shutil.copy2(DATA_XLSX, self.dir / "PERU.xlsx")
        os.chdir(self.dir)
        self.c = ctypes
        self.v = ctypes.WinDLL(self.DLL)
        self.v.vensim_command.argtypes = [ctypes.c_char_p]
        self.v.vensim_get_data.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                           ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
                                           ctypes.c_int]
        self.v.vensim_be_quiet(2)
        if not self.cmd("SPECIAL>LOADMODEL|model.vpmx"):
            raise RuntimeError("Vensim could not load the published model")
        self.vars = (
            ["Total Generation Capacity", "Total Transmission", "Transmission for Interconnection km",
             "Total Transmission for Generation Connection", "Total Transmission for Demand Connection",
             "Total Interconnection Capacity", "Transmission Tariff[f0]", "Congestion Tariff[f0]",
             "National Peak Demand[f0]"]
            + [f"Generation Capacity by Tech[{t}]" for t in self._TECH]
            + [f"Generation by Tech per Month[{t}]" for t in self._TECH + ["DERBESS", "DER"]]
            + [f"Electricity Tariff[{u},{z}]" for u in ("Regulated", "Unregulated") for z in self._ZONES]
            + [f"Generation Tariff[f0,{z}]" for z in self._ZONES])

    def cmd(self, s):
        return self.v.vensim_command(s.encode())

    def run(self, params, final_time):
        # Vensim keeps the last .vdfx open, so every run gets its own name and old files
        # are removed only when they are no longer locked.
        self.n_runs = getattr(self, "n_runs", 0) + 1
        name = f"run{self.n_runs}"
        vdf = self.dir / f"{name}.vdfx"
        for old in self.dir.glob("run*.vdfx"):
            try:
                old.unlink()
            except OSError:
                pass
        self.cmd(f"SIMULATE>RUNNAME|{name}")
        self.cmd(f"SIMULATE>SETVAL|FINAL TIME={final_time:g}")
        for k, val in params.items():
            if not self.cmd(f"SIMULATE>SETVAL|{k}={val:.10g}"):
                raise RuntimeError(f"Vensim rejected SETVAL {k} (is the published model ModeloCh4_Sens.vpmx?)")
        if not self.cmd("MENU>RUN|O") or not vdf.exists():
            raise RuntimeError("Vensim simulation failed")
        n = int(final_time / 0.25) + 10
        vals, times = (self.c.c_float * n)(), (self.c.c_float * n)()
        months = np.arange(0, final_time + 1, 1.0)
        out = {}
        for var in self.vars:
            k = self.v.vensim_get_data(str(vdf).encode(), var.encode(), b"Time", vals, times, n)
            if k > 0:
                s = pd.Series(np.array(vals[:k], float), index=np.round(np.array(times[:k], float), 4))
                out[var] = s[~s.index.duplicated(keep="last")].reindex(months, method="nearest")
        # A run that extracted nothing must fail loudly. Without this check an empty
        # result is written to disk and stamped complete: vensim_get_data returns
        # <= 0 for every variable when the run file cannot be read, `out` stays
        # empty, and the resulting one-column CSV looks like a finished run to the
        # resume logic. Ten runs were silently lost that way before this check
        # existed, and they would have reached the figures as missing data rather
        # than as an error.
        missing = [v for v in ("Total Generation Capacity", "Total Transmission",
                               "Total Interconnection Capacity") if v not in out]
        if missing:
            raise RuntimeError(
                f"simulation produced no data for {', '.join(missing)} "
                f"({len(out)} of {len(self.vars)} variables extracted)")

        df = pd.DataFrame(out, index=months)
        df.index.name = "time"
        # Vensim keeps every simulated run loaded in memory, so without this a
        # worker grows by roughly a gigabyte per run and has to be recycled. The
        # data has already been extracted above, so clearing costs nothing and
        # keeps a worker's footprint flat for the whole sweep.
        self.cmd("SPECIAL>CLEARRUNS")
        return df


def _worker_init(engine, path):
    global _MODEL
    warnings.filterwarnings("ignore")
    if engine == "vensim":
        _MODEL = VensimDLL(path)
        return
    _apply_pysd_patches()
    import pysd
    _MODEL = pysd.load(path)


def _worker_run(run_id, params, out_csv, final_time):
    import json
    import traceback
    t0 = time.time()
    try:
        if isinstance(_MODEL, VensimDLL):
            df = _MODEL.run(params, final_time)
        else:
            df = _MODEL.run(params=params, final_time=final_time, return_columns=SAVE_VARS,
                            return_timestamps=np.arange(0, final_time + 1, 1.0), progress=False)
        df.to_csv(out_csv)
        Path(out_csv).with_suffix(".json").write_text(json.dumps(
            {"params": params, "final_time": final_time}), encoding="utf-8")
        return run_id, time.time() - t0, ""
    except Exception:
        return run_id, time.time() - t0, traceback.format_exc(limit=3)


def _same_params(cached, params):
    """Is a cached run's parameter set the one the design asks for?

    Every knob the design sets has to match to within floating-point noise. A knob
    the cached run does not mention counts as zero, and only as zero: each knob
    enters the model as a multiplier whose default value reproduces the original
    equation exactly, so a run made before a knob existed is bit-for-bit the run
    that knob would have produced at zero. That is what lets the base case and the
    one-at-a-time runs of the three-factor design survive the addition of the
    demand factor, while every Monte Carlo sample - which gives the new factor a
    non-zero value - is correctly invalidated and re-simulated.
    """
    for knob, value in params.items():
        if abs(cached.get(knob, 0.0 if value == 0.0 else 1e9) - value) >= 1e-9:
            return False
    return True


def _is_done(runs_dir, rid, params, final_time):
    """A cached run is reused only if it used the same factor values and horizon."""
    import json
    j = runs_dir / f"{rid}.json"
    if not (runs_dir / f"{rid}.csv").exists() or not j.exists():
        return False
    meta = json.loads(j.read_text(encoding="utf-8"))
    return meta.get("final_time") == final_time and _same_params(meta["params"], params)


def _matches_design(runs_dir, rid, params):
    import json
    j = runs_dir / f"{rid}.json"
    if not (runs_dir / f"{rid}.csv").exists() or not j.exists():
        return False
    return _same_params(json.loads(j.read_text(encoding="utf-8"))["params"], params)


def _gen_delay_base():
    """'Other delays' fraction of the base case (PERU.xlsx, Gen Investment parameters!B15).
    The Sens Gen Delay knob replaces that input, so the base run must use this value."""
    global _GEN_BASE
    if _GEN_BASE is None:
        import openpyxl
        wb = openpyxl.load_workbook(DATA_XLSX, read_only=True, data_only=True)
        _GEN_BASE = float(wb["Gen Investment parameters"]["B15"].value or 0.0)
        wb.close()
    return _GEN_BASE


_GEN_BASE = None


def _params(row):
    """Design values are relative changes vs the base; total generation delays scale as
    (1 + base)*(1 + change), i.e. +/-20 % of the base delays (same as transmission)."""
    p = {FACTORS[k][0]: float(row[k]) for k in FACTORS}
    p[KNOB_GEN] = round((1 + _gen_delay_base()) * (1 + p[KNOB_GEN]) - 1, 10)
    return p


class KeepAwake:
    """Stop Windows from suspending the machine while a sweep is running.

    A sweep is hours of work with no keyboard or mouse activity, so an idle
    laptop drops into Modern Standby and takes the Vensim workers down with it:
    the pool's child processes die, the parent waits forever on futures that will
    never complete, and the run log simply stops. That happened once here, and
    cost three hours of wall-clock time with no error message anywhere.

    SetThreadExecutionState is the documented way for a process to say "the system
    is in use even though nobody is touching it". It is scoped to this process and
    released automatically when the process exits, so - unlike changing the power
    plan - it cannot leave the machine misconfigured afterwards.

    It does not override a closed lid or a manual sleep, and it is a no-op off
    Windows; both are reported rather than hidden.
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self):
        self.ok = False
        if platform.system() != "Windows":
            print("note: cannot prevent sleep on this platform", flush=True)
            return self
        import ctypes
        self._k32 = ctypes.windll.kernel32
        self.ok = bool(self._k32.SetThreadExecutionState(
            self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED))
        print("sleep prevented for the duration of the sweep" if self.ok
              else "WARNING: could not prevent sleep; the sweep may be interrupted",
              flush=True)
        return self

    def __exit__(self, *exc):
        if getattr(self, "ok", False):
            self._k32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


def run_all(design, engine, model_path, runs_dir, n_workers, final_time=FINAL_TIME):
    runs_dir.mkdir(parents=True, exist_ok=True)
    todo = [rid for rid in design.index
            if not _is_done(runs_dir, rid, _params(design.loc[rid]), final_time)]
    print(f"Engine: {engine} ({model_path})")
    print(f"{len(design)} runs in design, {len(todo)} pending, {n_workers} parallel workers")
    if not todo:
        return
    log = runs_dir.parent / "run_log.csv"
    if not log.exists():
        header = "run_id,kind," + ",".join(FACTORS) + ",finished_at,minutes,status"
        log.write_text(header + "\n", encoding="utf-8")
    t0 = time.time()
    # Workers are NOT recycled. They used to be, because Vensim accumulates every
    # simulated run in memory; VensimDLL.run now clears them instead, so a worker's
    # footprint stays flat and it can serve the whole sweep.
    #
    # Recycling was not merely unnecessary, it was the failure mode: with six
    # workers and a limit of four tasks each, all six retired on the same task and
    # their six replacements copied a 47 MB model and loaded it simultaneously.
    # The memory spike killed the pool outright - no traceback, no stderr, just a
    # parent waiting forever on futures that would never complete. It happened
    # twice, both times at exactly 24 completed runs.
    with KeepAwake(), ProcessPoolExecutor(
            max_workers=min(n_workers, len(todo)), initializer=_worker_init,
            initargs=(engine, str(model_path))) as ex:
        futs = []
        for rid in todo:
            r = design.loc[rid]
            params = _params(r)
            futs.append(ex.submit(_worker_run, rid, params, str(runs_dir / f"{rid}.csv"), final_time))
        for i, f in enumerate(as_completed(futs), 1):
            rid, dt, err = f.result()
            r = design.loc[rid]
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{rid},{r.kind}," + ",".join(f"{r[k]}" for k in FACTORS) + ","
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')},{dt / 60:.2f},{'ERROR' if err else 'ok'}\n")
            if err:
                print(f"  !! {rid} failed:\n{err}", flush=True)
            el = (time.time() - t0) / 60
            eta = el / i * (len(todo) - i)
            print(f"  [{i}/{len(todo)}] {rid} done ({dt / 60:.1f} min/run, elapsed {el:.0f} min, "
                  f"remaining ~{eta:.0f} min)", flush=True)


# ---------------------------------------------------------------------------
# 3. POST-PROCESSING
# ---------------------------------------------------------------------------
def load_run(path):
    df = pd.read_csv(path, index_col=0)
    df.index = pd.date_range(START_DATE, periods=len(df), freq="MS")
    return df


def _cols(df, var):
    return [c for c in df.columns if c.startswith(var + "[")]


def _tech_of(col):
    t = col.split("[", 1)[1].rstrip("]")
    return t[1:] if t.startswith("T") else t


def derive(df):
    """Builds the series used in charts/KPIs from a raw run."""
    out = {}
    out["trans_km"] = df["Total Transmission"]
    out["trans_gw"] = df["Total Interconnection Capacity"] / 1000.0
    out["gen_gw"] = df["Total Generation Capacity"]
    tar = df[[c for c in _cols(df, "Electricity Tariff") if "[Regulated," in c]]
    active = tar.columns[(tar.abs() > 0).any()]
    out["price"] = tar[active].mean(axis=1)
    zones = [c.split(",")[1].rstrip("]") for c in active]
    gen_tar = df[[f"Generation Tariff[f0,{z}]" for z in zones if f"Generation Tariff[f0,{z}]" in df]].mean(axis=1)
    trans_tar = df[[c for c in df.columns if c.startswith("Transmission Tariff[f0")]].sum(axis=1)
    cong_tar = df[[c for c in df.columns if c.startswith("Congestion Tariff[f0")]].sum(axis=1)
    out["price_parts"] = pd.DataFrame({
        "Generation": gen_tar, "Transmission": trans_tar, "Congestion": cong_tar,
        "Other charges": out["price"] - gen_tar - trans_tar - cong_tar})
    out["cap_tech"] = _group_tech(df[_cols(df, "Generation Capacity by Tech")])
    out["gen_tech"] = _group_tech(df[_cols(df, "Generation by Tech per Month")])
    out["km_type"] = pd.DataFrame({
        "Interconnection": df["Transmission for Interconnection km"],
        "Generation connection": df["Total Transmission for Generation Connection"],
        "Demand connection": df["Total Transmission for Demand Connection"]})
    peak = [c for c in df.columns if c.startswith("National Peak Demand[f0")]
    out["peak_gw"] = df[peak[0]] / 1000.0 if peak else None
    return out


def _group_tech(df):
    g = {}
    for c in df.columns:
        label = TECH_GROUPS.get(_tech_of(c), OTHER_TECH)
        g[label] = g.get(label, 0) + df[c].clip(lower=0)
    order = [t for t in TECH_COLORS if t in g and np.nanmax(g[t].values) > 1e-6]
    return pd.DataFrame(g)[order]


def kpis(d):
    return {
        "trans_km": float(d["trans_km"].iloc[-1]),
        "trans_gw": float(d["trans_gw"].iloc[-1]),
        "gen_gw": float(d["gen_gw"].iloc[-1]),
        "price": float(d["price"].iloc[-PRICE_WINDOW:].mean()),
    }


# ---------------------------------------------------------------------------
# 4. CHARTS (English)
# ---------------------------------------------------------------------------
def _style():
    import matplotlib as mpl
    mpl.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.labelsize": 10,
        "xtick.color": INK2, "ytick.color": INK2, "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "legend.fontsize": 9, "font.family": "DejaVu Sans",
        "lines.linewidth": 2,
    })


def _save(fig, name, out_dir):
    fig.savefig(out_dir / name, dpi=200, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    print("  saved", name)


def _fmt(v, unit):
    return f"{v:,.0f}" if unit == "km" else f"{v:,.2f}" if unit == "GW" else f"{v:,.1f}"


def plot_distribution(series, base, stats, out_dir):
    """Fan chart (P10-P90 band, P50, base) for each KPI + histogram of the final value."""
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5))
    for ax, (k, (label, unit)) in zip(axes.flat, KPIS.items()):
        s = series[k]
        if k == "price":   # monthly prices are noisy: 12-month rolling mean
            s = s.rolling(12, min_periods=1).mean()
            b = base[k].rolling(12, min_periods=1).mean()
        else:
            b = base[k]
        p10, p50, p90 = (s.quantile(q, axis=1) for q in (0.10, 0.50, 0.90))
        ax.fill_between(s.index, p10, p90, color=PALETTE[0], alpha=0.18, lw=0, label="P10–P90 range")
        ax.plot(s.index, p50, color=PALETTE[0], lw=2, label="P50 (median)")
        ax.plot(b.index, b, color=INK, lw=1.4, ls="--", label="Base case")
        ax.set_title(f"{label} [{unit}]" + (" – 12-month rolling mean" if k == "price" else ""), loc="left")
        ax.margins(x=0)
        ax.annotate(f"P90 {_fmt(p90.iloc[-1], unit)}", (p90.index[-1], p90.iloc[-1]), xytext=(4, 2),
                    textcoords="offset points", fontsize=8, color=INK2)
        ax.annotate(f"P10 {_fmt(p10.iloc[-1], unit)}", (p10.index[-1], p10.iloc[-1]), xytext=(4, -9),
                    textcoords="offset points", fontsize=8, color=INK2)
    axes[0, 0].legend(loc="upper left")
    spread = ", ".join(f"{lbl.lower()} ±{rng:.0%}" for _, rng, lbl in FACTORS.values())
    fig.suptitle(f"Uncertainty range P10–P90 ({stats['n']} Monte Carlo runs: {spread})",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "01_distribution_P10_P90_timeseries.png", out_dir)

    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    for ax, (k, (label, unit)) in zip(axes.flat, KPIS.items()):
        v = stats["values"][k]
        ax.hist(v, bins=min(15, max(5, len(v) // 3)), color=PALETTE[0], alpha=0.85,
                edgecolor=SURFACE, linewidth=2)
        ax.axvspan(np.percentile(v, 10), np.percentile(v, 90), color=PALETTE[0], alpha=0.08, lw=0)
        for q, ls in ((10, ":"), (50, "-"), (90, ":")):
            x = np.percentile(v, q)
            ax.axvline(x, color=INK2, ls=ls, lw=1.2)
            ax.annotate(f"P{q}\n{_fmt(x, unit)}", (x, ax.get_ylim()[1]), xytext=(3, -24),
                        textcoords="offset points", fontsize=8, color=INK2)
        ax.axvline(stats["base"][k], color=INK, ls="--", lw=1.4)
        ax.annotate("Base", (stats["base"][k], 0), xytext=(3, 3), textcoords="offset points",
                    fontsize=8, color=INK)
        when = "mean of last 12 months to Jan-2050" if k == "price" else "Jan-2050"
        ax.set_title(f"{label} – {when}", loc="left")
        ax.set_xlabel(unit)
        ax.set_ylabel("Number of runs")
        ax.grid(axis="x", visible=False)
    fig.suptitle("Distribution of final values (P10 / P50 / P90)", fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "02_distribution_P10_P90_histograms.png", out_dir)


def plot_tornado(tornado, out_dir):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.5))
    for ax, (k, (label, unit)) in zip(axes.flat, KPIS.items()):
        t = tornado[tornado.kpi == k].sort_values("swing")
        y = np.arange(len(t))
        base = t["base"].iloc[0]
        lo, hi = t["low_value"] - base, t["high_value"] - base
        # The low and high bars of a factor sit side by side, low above high as in
        # the legend. Drawn on the same line, the shorter bar hid part of the longer
        # one whenever the response is not monotone in the factor.
        ax.barh(y + 0.15, lo, height=0.3, color=PALETTE[2], label="Factor at low (−)",
                edgecolor=SURFACE, linewidth=1)
        ax.barh(y - 0.15, hi, height=0.3, color=PALETTE[1], label="Factor at high (+)",
                edgecolor=SURFACE, linewidth=1)
        ax.axvline(0, color=INK, lw=1)
        ax.set_yticks(y, [f"{r.factor_label}\n({r.range_label})" for r in t.itertuples()])
        span = max(np.abs(np.r_[lo.values, hi.values]).max(), 1e-9)
        # Wide enough that a value label printed past the end of the longest bar
        # still lands inside the panel instead of over the factor names.
        ax.set_xlim(-span * 1.95, span * 1.95)
        # Value labels sit just past the end of their own bar, on that bar's half
        # of the row, so the two labels of a factor never print over each other or
        # over the other bar. The text is never shortened: these are the numbers the
        # paper quotes.
        for yi, l, h, lv, hv in zip(y, lo, hi, t["low_value"], t["high_value"]):
            for (d, v), dy in (((l, lv), 0.15), ((h, hv), -0.15)):
                ax.annotate(f"{_fmt(v, unit)} ({d / base:+.1%})", (d, yi + dy),
                            xytext=(4 if d >= 0 else -4, 0), textcoords="offset points",
                            ha="left" if d >= 0 else "right", va="center",
                            fontsize=8, color=INK2)
        ax.set_title(f"{label}", loc="left")
        ax.set_xlabel(f"Change vs base ({_fmt(base, unit)} {unit})")
        ax.grid(axis="y", visible=False)
        # Cap the tick count: the widened limits that keep the value labels inside
        # the panel otherwise produce five-digit ticks that run into each other.
        ax.xaxis.set_major_locator(plt.MaxNLocator(5, prune="both"))
    # Outside the panels: inside the first one it sat on top of the bars.
    axes[0, 0].legend(loc="upper center", bbox_to_anchor=(1.03, 1.22), ncols=2,
                      frameon=False)
    fig.suptitle("Tornado diagram – one-at-a-time sensitivity (values at Jan-2050; price = mean of last 12 months)",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "03_tornado.png", out_dir)


def _annual(df, how="mean"):
    df = df[df.index.year < df.index.year.max()] if df.index[-1].month == 1 else df
    g = df.groupby(df.index.year)
    return g.sum() if how == "sum" else g.mean()


def plot_scenarios(sc, out_dir):
    import matplotlib.pyplot as plt
    names = list(sc)

    # Price
    fig = plt.figure(figsize=(14, 9))
    gs = fig.add_gridspec(2, 3, height_ratios=[1.1, 1])
    ax = fig.add_subplot(gs[0, :])
    for n in names:
        p = _annual(sc[n]["data"]["price"].to_frame())[0]
        ax.plot(p.index, p.values, color=SCEN_COLORS[n], lw=2.2, label=sc[n]["title"])
        ax.annotate(f"{p.iloc[-1]:.1f}", (p.index[-1], p.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK2)
    ax.set_title("Country average electricity price – annual mean [USD/MWh]", loc="left")
    ax.legend(loc="upper right")
    ax.margins(x=0.02)
    ymax = max(_annual(sc[n]["data"]["price_parts"]).clip(lower=0).sum(axis=1).max() for n in names) * 1.05
    for i, n in enumerate(names):
        a = fig.add_subplot(gs[1, i])
        parts = _annual(sc[n]["data"]["price_parts"]).clip(lower=0)
        a.stackplot(parts.index, parts.T.values, labels=parts.columns,
                    colors=PALETTE[:parts.shape[1]], edgecolor=SURFACE, linewidth=0.8)
        a.set_title(f"{n}: price components", loc="left", fontsize=11)
        a.set_ylim(0, ymax)
        a.set_ylabel("USD/MWh")
        a.margins(x=0)
    fig.axes[-1].legend(loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    _save(fig, "04_price_best_worst.png", out_dir)

    # Installed capacity by technology
    _stack_panels(sc, "cap_tech", "Installed capacity by technology [GW]", "GW",
                  "05_installed_capacity_by_technology.png", out_dir, peak=True)
    # Generation by technology (annual)
    _stack_panels(sc, "gen_tech", "Generation by technology [TWh/year]", "TWh/year",
                  "06_generation_by_technology.png", out_dir, annual_sum=True)

    # Transmission capacity GW
    fig, ax = plt.subplots(figsize=(12, 5))
    for n in names:
        s = sc[n]["data"]["trans_gw"]
        ax.plot(s.index, s.values, color=SCEN_COLORS[n], label=sc[n]["title"])
        ax.annotate(f"{s.iloc[-1]:.2f} GW", (s.index[-1], s.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK2)
    ax.set_title("Total transmission (interconnection) capacity [GW]", loc="left")
    ax.set_ylabel("GW")
    ax.legend(loc="upper left")
    ax.margins(x=0.01)
    fig.tight_layout()
    _save(fig, "07_transmission_capacity_GW.png", out_dir)

    # Transmission km
    fig = plt.figure(figsize=(14, 9))
    gs = fig.add_gridspec(2, 3, height_ratios=[1.1, 1])
    ax = fig.add_subplot(gs[0, :])
    for n in names:
        s = sc[n]["data"]["trans_km"]
        ax.plot(s.index, s.values, color=SCEN_COLORS[n], label=sc[n]["title"])
        ax.annotate(f"{s.iloc[-1]:,.0f} km", (s.index[-1], s.iloc[-1]), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK2)
    ax.set_title("Total transmission length [km]", loc="left")
    ax.legend(loc="upper left")
    ax.margins(x=0.01)
    ymax = max(sc[n]["data"]["km_type"].sum(axis=1).max() for n in names) * 1.05
    for i, n in enumerate(names):
        a = fig.add_subplot(gs[1, i])
        k = sc[n]["data"]["km_type"]
        a.stackplot(k.index, k.T.values, labels=k.columns, colors=PALETTE[:3],
                    edgecolor=SURFACE, linewidth=0.5)
        a.set_title(f"{n}: length by type", loc="left", fontsize=11)
        a.set_ylim(0, ymax)
        a.set_ylabel("km")
        a.margins(x=0)
    fig.axes[-1].legend(loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    _save(fig, "08_transmission_km.png", out_dir)


def _stack_panels(sc, key, title, unit, fname, out_dir, peak=False, annual_sum=False):
    import matplotlib.pyplot as plt
    names = list(sc)
    techs = list(dict.fromkeys(t for n in names for t in sc[n]["data"][key].columns))
    techs = [t for t in TECH_COLORS if t in techs]
    frames = {}
    for n in names:
        d = sc[n]["data"][key].reindex(columns=techs).fillna(0)
        frames[n] = _annual(d, "sum") / 1000.0 if annual_sum else d   # GWh/month -> TWh/year
    ymax = max(f.sum(axis=1).max() for f in frames.values()) * 1.08
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5), sharey=True)
    for ax, n in zip(axes, names):
        f = frames[n]
        ax.stackplot(f.index, f.T.values, labels=f.columns, colors=[TECH_COLORS[t] for t in f.columns],
                     edgecolor=SURFACE, linewidth=0.5)
        if peak and sc[n]["data"]["peak_gw"] is not None:
            ax.plot(sc[n]["data"]["peak_gw"].index, sc[n]["data"]["peak_gw"].values, color=INK,
                    ls="--", lw=1.4, label="National peak demand")
        ax.set_title(sc[n].get("panel_title", sc[n]["title"]), loc="left", fontsize=9.5)
        ax.annotate(f"Total {f.sum(axis=1).iloc[-1]:,.1f}", (f.index[-1], f.sum(axis=1).iloc[-1]),
                    xytext=(-4, 6), textcoords="offset points", ha="right", fontsize=9, color=INK2)
        ax.set_ylim(0, ymax)
        ax.margins(x=0)
    axes[0].set_ylabel(unit)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=min(5, len(l)), bbox_to_anchor=(0.5, -0.08))
    fig.suptitle(title, fontsize=12, color=INK, fontweight="bold", x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, fname, out_dir)


def method_stats(kp, mc_ids):
    """Convergence of P10/P50/P90 with the number of runs (Monte Carlo runs, in sample order)."""
    m = kp.loc[mc_ids]
    steps = sorted(set([n for n in (10, 20, 30, 50, 75, 100, 150, 200, 250, 300, 400, 500)
                        if n <= len(m)] + [len(m)]))
    conv = []
    for n in steps:
        sub = m.iloc[:n]
        for k, (label, unit) in KPIS.items():
            conv.append({"Runs": n, "KPI": f"{label} [{unit}]", "P10": np.percentile(sub[k], 10),
                         "P50": np.percentile(sub[k], 50), "P90": np.percentile(sub[k], 90)})
    conv = pd.DataFrame(conv)
    full = conv[conv.Runs == len(m)].set_index("KPI")
    conv["P50 change vs all runs [%]"] = conv.apply(
        lambda r: 100 * (r.P50 / full.loc[r.KPI, "P50"] - 1) if full.loc[r.KPI, "P50"] else 0, axis=1)
    return conv


def rank_correlation(design, kp, mc_ids):
    """Spearman rank correlation between each factor and each KPI (Monte Carlo runs)."""
    d = design.loc[mc_ids, list(FACTORS)].astype(float).join(kp.loc[mc_ids])
    rc = d.corr(method="spearman").loc[list(FACTORS), list(KPIS)]
    rc.index = [FACTORS[k][2] for k in rc.index]
    rc.columns = [f"{KPIS[k][0]} [{KPIS[k][1]}]" for k in rc.columns]
    return rc


def plot_method(design, kp, mc_ids, conv, rc, out_dir):
    import matplotlib.pyplot as plt
    keys = list(FACTORS)
    m = design.loc[mc_ids, keys].astype(float) * 100
    labels = [FACTORS[k][2] for k in keys]
    oat = design[design.kind != "montecarlo"]

    # 09 - sampling design: histograms (diagonal) + pairwise scatter.
    # The grid follows the number of factors; it used to be hard-coded at 3x3 and
    # broke as soon as a fourth factor was added.
    n = len(keys)
    fig, axes = plt.subplots(n, n, figsize=(3.6 * n, 3.3 * n), squeeze=False)
    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            ax = axes[i, j]
            if i == j:
                ax.hist(m[ki], bins=10, color=PALETTE[0], edgecolor=SURFACE, linewidth=2)
                ax.set_ylabel("Runs" if j == 0 else "")
                ax.grid(axis="x", visible=False)
            else:
                ax.scatter(m[kj], m[ki], s=12, color=PALETTE[0], alpha=0.7, lw=0,
                           label="Monte Carlo runs" if (i, j) == (1, 0) else None)
                ax.scatter(oat[kj] * 100, oat[ki] * 100, s=40, color=PALETTE[1], marker="D",
                           edgecolor=SURFACE, linewidth=1,
                           label="Base + tornado runs" if (i, j) == (1, 0) else None)
            if i == n - 1:
                ax.set_xlabel(f"{labels[j]} [% vs base]")
            if j == 0 and i != j:
                ax.set_ylabel(f"{labels[i]} [% vs base]")
    axes[1, 0].legend(loc="upper left", fontsize=8)
    fig.suptitle(f"Experiment design: {len(mc_ids)} Latin-Hypercube samples (uniform, independent factors) "
                 f"+ {len(oat)} base/tornado runs", fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "09_sampling_design.png", out_dir)

    # 10 - convergence of the percentiles with the number of runs
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    for ax, (k, (label, unit)) in zip(axes.flat, KPIS.items()):
        c = conv[conv.KPI == f"{label} [{unit}]"]
        for q, col in (("P90", PALETTE[1]), ("P50", PALETTE[0]), ("P10", PALETTE[2])):
            ax.plot(c.Runs, c[q], color=col, marker="o", ms=4, label=q)
            ax.annotate(_fmt(c[q].iloc[-1], unit), (c.Runs.iloc[-1], c[q].iloc[-1]), xytext=(4, 0),
                        textcoords="offset points", va="center", fontsize=8, color=INK2)
        ax.set_title(f"{label} [{unit}]", loc="left")
        ax.set_xlabel("Number of Monte Carlo runs used")
    axes[0, 0].legend(loc="best")
    fig.suptitle("Convergence check: percentiles at 2050 recomputed as runs are added",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "10_convergence.png", out_dir)

    # 11 - KPI vs each factor with Spearman rank correlation
    fig, axes = plt.subplots(len(KPIS), len(keys), figsize=(13, 12), sharex="col")
    for r, (k, (label, unit)) in enumerate(KPIS.items()):
        for c, f in enumerate(keys):
            ax = axes[r, c]
            ax.scatter(m[f], kp.loc[mc_ids, k], s=10, color=PALETTE[0], alpha=0.6, lw=0)
            ax.axhline(kp.loc["base", k], color=INK, ls="--", lw=1)
            rho = rc.loc[FACTORS[f][2], f"{label} [{unit}]"]
            ax.set_title(f"ρ = {rho:+.2f}", loc="right", fontsize=9, color=INK2, fontweight="normal")
            if c == 0:
                ax.set_ylabel(f"{label}\n[{unit}]", fontsize=9)
            if r == len(KPIS) - 1:
                ax.set_xlabel(f"{FACTORS[f][2]} [% vs base]")
    fig.suptitle("Factor influence: KPI at 2050 vs factor value over all Monte Carlo runs "
                 "(ρ = Spearman rank correlation; dashed = base case)", fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    _save(fig, "11_factor_influence.png", out_dir)


# ---------------------------------------------------------------------------
# 5. TABLES
# ---------------------------------------------------------------------------
#: What each factor actually does to the model, for the README sheet of the summary
#: workbook. Keyed by factor so the sheet follows the factor set rather than a
#: hard-coded list of three.
FACTOR_NOTES = {
    "gen_delay": "Licensing + construction time of every technology, through the same "
                 "mechanism as 'Other delays' in sheet 'Gen Investment parameters'. "
                 "Expressed against the workbook base case (B15 = -0.2), not the planned time.",
    "trans_delay": "Public-tender, investor-selection, licensing and construction times of "
                   "transmission projects (lines and GETs, sheet 'Trans Investment "
                   "parameters'). The 12-month planning cycle is deliberately not changed.",
    "fuel": "'Fuel Cost' of every technology that burns fuel (sheet 'Gen Investment "
            "parameters', row 21).",
    "demand": "The 2050 demand level reached by the growth path. The multiplier is 1 in "
              "January 2025 and 1 + factor in January 2050, so the starting point is "
              "untouched and only the growth rate varies.",
}


def build_tables(design, kp, tornado, dist, sc, out_dir, conv, rc):
    last = sc["Base"]["data"]["trans_km"].index[-1].year
    years = [y for y in (2030, 2040, 2050) if y <= last] or [last]
    rows = []
    for n, s in sc.items():
        d = s["data"]
        # One column per factor actually in play. These used to be three fixed
        # columns, which silently dropped the demand factor from the sheet.
        r = {"Scenario": n, "Run": s["run_id"],
             **{FACTORS[k][2]: f"{s['factors'][k]:+.1%}" for k in FACTORS}}
        for k, (label, unit) in KPIS.items():
            ser = d[k].rolling(12, min_periods=1).mean() if k == "price" else d[k]
            for y in years:
                r[f"{label} {y} [{unit}]"] = round(float(ser[ser.index.year == y].iloc[-1]), 2)
        rows.append(r)
    scen = pd.DataFrame(rows).set_index("Scenario")
    for k, (label, unit) in KPIS.items():
        col = f"{label} {years[-1]} [{unit}]"
        scen[f"{label} {years[-1]} vs base [%]"] = (scen[col] / scen.loc["Base", col] - 1).mul(100).round(1)

    readme = pd.DataFrame([
        ("Objective", "Assess how uncertainty in " + ", ".join(f[2].lower() for f in FACTORS.values()) +
                      " affects transmission expansion, generation capacity and the country average price."),
        # The engine is reported, not assumed: the same analysis can come from either
        # the Vensim DLL or PySD, and a reader of the workbook has to know which.
        ("Model", f"{MODEL_MDL.name} with data from {DATA_XLSX.name}, patched with the "
                  f"{len(FACTORS)} sensitivity constants and published as {VENSIM_MODEL.name}; "
                  f"simulated 2025-01 to 2050-01 (300 months, dt = 0.25 month) with "
                  f"{'the Vensim DSS DLL' if VENSIM_MODEL.exists() else 'PySD'}."),
        *[(f"Factor {i} – {FACTORS[k][2]}",
           f"±{FACTORS[k][1]:.0%}. {FACTOR_NOTES[k]}")
          for i, k in enumerate(FACTORS, 1)],
        ("Tornado diagram", "One-at-a-time: each factor is set to its low and its high value while the others stay "
                            "at base. Bars show the change of the KPI against the base case; factors are sorted by "
                            "swing (|high − low|)."),
        ("P10–P90 distribution", f"{dist['n']} Latin-Hypercube Monte Carlo runs with all {len(FACTORS)} factors varying "
                                 "simultaneously (uniform distributions). P10 = value exceeded by 90 % of runs; "
                                 "P90 = value exceeded by 10 % of runs; P50 = median."),
        ("Best / worst scenario", "Chosen among the Monte Carlo runs: best = lowest country average price, worst = "
                                  "highest country average price" if BEST_WORST_CRITERION == "price" else
                                  "Chosen among the Monte Carlo runs with a composite score (z-scores): "
                                  "+generation GW +transmission km +transmission GW −price."),
        ("KPI – Total transmission [km]", "'Total Transmission' = demand-connection + generation-connection + "
                                          "interconnection lines, value at Jan-2050."),
        ("KPI – Transmission capacity [GW]", "'Total Interconnection Capacity' (MW) / 1000, value at Jan-2050."),
        ("KPI – Generation capacity [GW]", "'Total Generation Capacity', value at Jan-2050."),
        ("KPI – Country average price [USD/MWh]", "Mean of 'Electricity Tariff[Regulated, Zone]' over the active "
                                                  f"zones; KPI = mean of the last {PRICE_WINDOW} months of the run. "
                                                  "Components: generation tariff, transmission tariff, congestion "
                                                  "tariff and other charges (taxes, distribution, etc.)."),
        ("Sampling", f"Latin Hypercube (seed {SEED}): each factor range is cut into {dist['n']} equal intervals "
                     "and one value is drawn at random inside each interval; the intervals of the three factors are "
                     "paired at random. This covers each range evenly with fewer runs than plain random sampling."),
        ("Convergence check", "Sheet 'Convergence' and chart 10: P10/P50/P90 recomputed with the first 10, 20, 50, "
                              "100 ... runs. If the values no longer move, the number of runs is sufficient."),
        ("Factor influence", "Sheet 'Rank_correlation' and chart 11: Spearman rank correlation between each factor "
                             "and each KPI over all Monte Carlo runs (-1 to +1; near 0 = no influence). It "
                             "complements the tornado, which varies one factor at a time."),
        ("Traceability", "tornado_results/runs/<run>.csv = full monthly output of every run; <run>.json = factor "
                         "values used; run_log.csv = finish time and duration of each run; experiment_design.csv = "
                         "full sample. Model copy actually simulated: tornado_results/_model/peru_sens.mdl."),
        ("Charts", "00 summary table, 01–02 P10–P90 distribution, 03 tornado, 04 price, 05 installed capacity by "
                   "technology, 06 generation by technology, 07 transmission capacity (GW), 08 transmission length "
                   "(km), 09 sampling design, 10 convergence, 11 factor influence."),
    ], columns=["Item", "Explanation"])

    dist_tbl = pd.DataFrame({
        f"{label} [{unit}]": {
            "Base": dist["base"][k], "P10": np.percentile(dist["values"][k], 10),
            "P50": np.percentile(dist["values"][k], 50), "P90": np.percentile(dist["values"][k], 90),
            "Mean": np.mean(dist["values"][k]), "Min": np.min(dist["values"][k]), "Max": np.max(dist["values"][k]),
            "P90 − P10": np.percentile(dist["values"][k], 90) - np.percentile(dist["values"][k], 10),
        } for k, (label, unit) in KPIS.items()}).T.round(2)

    torn_tbl = tornado.rename(columns={
        "kpi_label": "KPI", "unit": "Unit", "factor_label": "Factor", "range_label": "Range", "base": "Base value",
        "low_value": "Value with factor low", "high_value": "Value with factor high", "low_change": "Change (low)",
        "high_change": "Change (high)", "low_pct": "Change (low) %", "high_pct": "Change (high) %",
        "swing": "Swing |high−low|"}).drop(columns=["kpi"])
    torn_tbl = torn_tbl.sort_values(["KPI", "Swing |high−low|"], ascending=[True, False])

    mc = design.join(kp).rename(columns={k: f"{v[0]} [{v[1]}]" for k, v in KPIS.items()})
    mc = mc.rename(columns={k: FACTORS[k][2] for k in FACTORS})

    xlsx = out_dir / "tornado_summary.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        readme.to_excel(w, sheet_name="README", index=False)
        scen.to_excel(w, sheet_name="Best_Worst_Scenarios")
        dist_tbl.to_excel(w, sheet_name="Distribution_P10_P90")
        torn_tbl.round(3).to_excel(w, sheet_name="Tornado", index=False)
        mc.round(4).to_excel(w, sheet_name="All_runs")
        conv.round(3).to_excel(w, sheet_name="Convergence", index=False)
        rc.round(3).to_excel(w, sheet_name="Rank_correlation")
        for ws in w.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 90)
            ws.freeze_panes = "B2"
        from openpyxl.styles import Alignment, Font
        for ws in w.book.worksheets:
            for c in ws[1]:
                c.font = Font(bold=True)
        for row in w.book["README"].iter_rows(min_row=2):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    print("  saved", xlsx.name)
    plot_summary_table(scen, dist_tbl, out_dir, years[-1])
    return scen, dist_tbl, torn_tbl


def plot_summary_table(scen, dist_tbl, out_dir, yr=2050):
    import matplotlib.pyplot as plt
    kp_rows = []
    for k, (label, unit) in KPIS.items():
        c = f"{label} {yr} [{unit}]"
        d = dist_tbl.loc[f"{label} [{unit}]"]
        kp_rows.append([f"{label} [{unit}]", _fmt(scen.loc["Base", c], unit), _fmt(d["P10"], unit),
                        _fmt(d["P50"], unit), _fmt(d["P90"], unit),
                        f"{_fmt(scen.loc['Best', c], unit)} ({scen.loc['Best', f'{label} {yr} vs base [%]']:+.1f}%)",
                        f"{_fmt(scen.loc['Worst', c], unit)} ({scen.loc['Worst', f'{label} {yr} vs base [%]']:+.1f}%)"])
    # One row per factor in play; this used to be three hard-coded rows.
    fac = [[FACTORS[k][2], *[scen.loc[s, FACTORS[k][2]] for s in ("Base", "Best", "Worst")]]
           for k in FACTORS]
    # Height in proportion to the rows each table actually has, so the panels do not
    # leave a gap when the factor count changes.
    rows_top, rows_bottom = len(KPIS) + 1, len(fac) + 1
    fig, (a1, a2) = plt.subplots(
        2, 1, figsize=(14, 1.0 + 0.52 * (rows_top + rows_bottom)),
        gridspec_kw={"height_ratios": [rows_top, rows_bottom]})
    for a in (a1, a2):
        a.axis("off")
    t1 = a1.table(cellText=kp_rows, colLabels=["KPI (2050)", "Base", "P10", "P50", "P90", "Best scenario",
                                               "Worst scenario"], loc="upper center", cellLoc="center",
                  colWidths=[0.31] + [0.115] * 6)
    t2 = a2.table(cellText=fac, colLabels=["Factor (change vs base)", "Base", "Best scenario", "Worst scenario"],
                  loc="upper center", cellLoc="center")
    for t in (t1, t2):
        t.auto_set_font_size(False)
        t.set_fontsize(9.5)
        t.scale(1, 1.6)
        for (r, c), cell in t.get_celld().items():
            cell.set_edgecolor(GRID)
            if r == 0:
                cell.set_text_props(weight="bold", color=INK)
                cell.set_facecolor("#f0efec")
            if c == 0:
                cell.set_text_props(ha="left")
    a1.set_title("Summary – KPIs at 2050 (price: mean of the last 12 months)", loc="left", fontsize=12,
                 fontweight="bold", color=INK)
    a2.set_title("Factor values of each scenario", loc="left", fontsize=12, fontweight="bold", color=INK)
    crit = "lowest / highest country average price" if BEST_WORST_CRITERION == "price" else "composite score"
    fig.text(0.01, 0.01, f"Best / worst = Monte Carlo runs with {crit}. P10–P90 from Latin-Hypercube "
                         f"sampling. Details in tornado_summary.xlsx (sheet README).",
             fontsize=8.5, color=INK2)
    fig.tight_layout()
    _save(fig, "00_summary_table.png", out_dir)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def analyse(design, runs_dir, out_dir):
    _style()
    print("Post-processing ...")
    data = {rid: derive(load_run(runs_dir / f"{rid}.csv")) for rid in design.index
            if _matches_design(runs_dir, rid, _params(design.loc[rid]))}
    print(f"  {len(data)} of {len(design)} runs available")
    if "base" not in data:
        sys.exit("Base run missing – run the simulations first.")
    kp = pd.DataFrame({rid: kpis(d) for rid, d in data.items()}).T
    base_k = kp.loc["base"]

    # Tornado table
    rows = []
    for key, (_, rng, flabel) in FACTORS.items():
        lo, hi = f"oat_{key}_low", f"oat_{key}_high"
        if lo not in kp.index or hi not in kp.index:
            continue
        for k, (label, unit) in KPIS.items():
            b, l, h = base_k[k], kp.loc[lo, k], kp.loc[hi, k]
            rows.append(dict(kpi=k, kpi_label=label, unit=unit, factor_label=flabel,
                             range_label=f"−{rng:.0%} / +{rng:.0%}", base=b, low_value=l, high_value=h,
                             low_change=l - b, high_change=h - b, low_pct=100 * (l / b - 1),
                             high_pct=100 * (h / b - 1), swing=abs(h - l)))
    tornado = pd.DataFrame(rows)

    # Monte Carlo distribution
    mc_ids = [r for r in design.index[design.kind == "montecarlo"] if r in data]
    if len(mc_ids) < 5:
        sys.exit(f"Only {len(mc_ids)} Monte Carlo runs available – need at least 5.")
    series = {k: pd.concat({r: data[r][k] for r in mc_ids}, axis=1) for k in KPIS}
    dist = dict(n=len(mc_ids), base=base_k.to_dict(), values={k: kp.loc[mc_ids, k].values for k in KPIS})

    # Best / worst
    m = kp.loc[mc_ids]
    if BEST_WORST_CRITERION == "price":
        score = -m["price"]
    else:
        z = (m - m.mean()) / m.std(ddof=0).replace(0, 1)
        score = z["gen_gw"] + z["trans_km"] + z["trans_gw"] - z["price"]
    best, worst = score.idxmax(), score.drop(score.idxmax()).idxmin()
    sc = {}
    for n, rid in (("Base", "base"), ("Best", best), ("Worst", worst)):
        f = design.loc[rid, list(FACTORS)].astype(float).to_dict()
        # Built from FACTORS rather than from three hard-coded names, so the title
        # still reports every factor when the demand knob is in play.
        short = {"gen_delay": "gen. delays", "trans_delay": "trans. delays",
                 "fuel": "fuel", "demand": "demand"}
        detail = ", ".join(f"{short.get(k, k)} {f[k]:+.0%}" for k in FACTORS)
        sc[n] = dict(run_id=rid, data=data[rid], factors=f,
                     title="Base case" if n == "Base" else f"{n} ({detail})",
                     # Two lines for the stacked panels: with four factors the
                     # single-line form overflowed into the neighbouring title.
                     panel_title=("Base case" if n == "Base"
                                  else f"{n} ({rid})" + chr(10) + detail))
    print(f"  best = {best}, worst = {worst}")

    out_dir.mkdir(parents=True, exist_ok=True)
    plot_distribution(series, data["base"], dist, out_dir)
    if len(tornado):
        plot_tornado(tornado, out_dir)
    plot_scenarios(sc, out_dir)
    conv, rc = method_stats(kp, mc_ids), rank_correlation(design, kp, mc_ids)
    plot_method(design, kp, mc_ids, conv, rc, out_dir)
    build_tables(design, kp, tornado, dist, sc, out_dir, conv, rc)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=N_SAMPLES, help="Monte Carlo runs")
    ap.add_argument("--workers", type=int, default=N_WORKERS, help="parallel simulations")
    ap.add_argument("--plots-only", action="store_true", help="skip simulations, use cached runs")
    ap.add_argument("--final-time", type=float, default=FINAL_TIME, help="months to simulate (test runs)")
    ap.add_argument("--out", default=str(RESULTS_DIR), help="results folder")
    ap.add_argument("--engine", choices=["auto", "vensim", "pysd"], default="auto",
                    help="vensim = Vensim DLL with the published ModeloCh4_Sens.vpmx (fast); pysd = Python only")
    ap.add_argument("--make-vensim-model", action="store_true",
                    help="only write model/ModeloCh4_Sens2.mdl, to be published from Vensim DSS")
    ap.add_argument("--demand", choices=["auto", "on", "off"], default="auto",
                    help="include the demand factor; 'auto' uses it when ModeloCh4_Sens2.vpmx exists")
    args = ap.parse_args()
    if args.make_vensim_model:
        print("Written:", write_vensim_model())
        print("Open it in Vensim DSS and publish it as model/ModeloCh4_Sens2.vpmx.")
        return

    # Which factors are in play has to be settled before the design is built: the
    # design, the run ids and the cached-run check all depend on it. The demand
    # factor needs a published model that exposes its knob, so the factor set and
    # the model are chosen together, and printed.
    global VENSIM_MODEL, FACTORS
    with_demand = args.demand == "on" or (args.demand == "auto" and VENSIM_MODEL_4F.exists())
    if args.demand == "on" and not VENSIM_MODEL_4F.exists():
        raise SystemExit(
            f"--demand on needs {VENSIM_MODEL_4F.name}. Run "
            "'python peru_tornado.py --make-vensim-model' and publish the .mdl it "
            "writes as that .vpmx from Vensim DSS."
        )
    VENSIM_MODEL = VENSIM_MODEL_4F if with_demand else VENSIM_MODEL_3F
    select_factors(with_demand)
    print(f"Factors ({len(FACTORS)}): " + ", ".join(f[2] for f in FACTORS.values()))
    print(f"Model:   {VENSIM_MODEL.name}")
    if not with_demand:
        print("NOTE: running without the demand factor. Publish "
              "model/ModeloCh4_Sens2.vpmx to include it.")

    warnings.filterwarnings("ignore")
    out = Path(args.out)
    runs_dir, fig_dir = out / "runs", out / "figures"
    design = design_runs(args.samples)
    out.mkdir(exist_ok=True)
    design.to_csv(out / "experiment_design.csv")
    if not args.plots_only:
        engine = args.engine
        if engine == "auto":
            engine = "vensim" if VENSIM_MODEL.exists() and Path(VensimDLL.DLL).exists() else "pysd"
        model_path = VENSIM_MODEL if engine == "vensim" else build_model(RESULTS_DIR / "_model")
        run_all(design, engine, model_path, runs_dir, args.workers, args.final_time)
    analyse(design, runs_dir, fig_dir)
    print(f"\nDone. Figures and tables in: {fig_dir}")


if __name__ == "__main__":
    main()
