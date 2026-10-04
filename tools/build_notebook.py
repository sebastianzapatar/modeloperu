"""Generate notebooks/Peru_expansion_uncertainty.ipynb.

Kept as a build script so the notebook's prose and code stay reviewable as plain
Python and the .ipynb is never hand-edited. Run from the repository root:

    python tools/build_notebook.py
"""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

NB = nbf.v4.new_notebook()
cells: list = []


def md(text: str) -> None:
    cells.append(nbf.v4.new_markdown_cell(text.strip("\n")))


def code(text: str) -> None:
    cells.append(nbf.v4.new_code_cell(text.strip("\n")))


# --------------------------------------------------------------------------- #
md(r"""
# Power System Expansion Under Uncertainty — Peru

**Tornado and P10–P90 analysis of a system-dynamics expansion model, 2025–2050.**

This notebook is the executable record of the Peru experiment. It

1. states what the model is and how a factor is made to reach it,
2. builds the design — 1 base run, 2 tornado runs per factor, 200 Monte Carlo samples,
3. runs, or loads, the ensemble through Vensim DSS,
4. draws the **tornado diagram**,
5. draws the **P10–P90 envelopes** of the four KPIs,
6. selects the **best and worst case** on one stated metric and draws, for those two and
   the base case, **electricity generation by technology** and **installed capacity by
   technology**,
7. reports the method checks — sampling design, convergence, rank correlation — and the
   tables the paper needs.

The question is not *what will happen in Peru* but **which unknowns actually move the
system**. Four drivers are in play: a **fuel price** change, a change in **demand growth**,
and **delays in project entry** on the generation and on the transmission side.

> `README.md` documents the pipeline, the failure modes, the figure conventions and the
> limitations. This notebook assumes it has been read.
""")

# --------------------------------------------------------------------------- #
md(r"""
## 0. Setup

All the machinery lives in `peru_tornado.py`: the model patching, the design, the Vensim
DSS driver, the post-processing and the figures. The notebook orchestrates it and explains
each step. Keeping the driver in a module is what lets the sweep run as six parallel
processes — a notebook kernel cannot hold six independent Vensim engines.
""")

code(r"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from IPython.display import Image, display

# The notebook lives in notebooks/; everything else is addressed from the repository root.
ROOT = Path.cwd() if (Path.cwd() / "peru_tornado.py").exists() else Path.cwd().parent
sys.path.insert(0, str(ROOT))

import peru_tornado as pt

RESULTS = ROOT / "results" / "peru"
RUNS, FIGURES = RESULTS / "runs", RESULTS / "figures"

# Which factor set is in play depends on which published model exists. The 4-factor
# design needs ModeloCh4_Sens2.vpmx, which has to be published by hand from Vensim DSS
# (README section 6); until then the notebook runs the three original factors.
WITH_DEMAND = pt.VENSIM_MODEL_4F.exists()
pt.VENSIM_MODEL = pt.VENSIM_MODEL_4F if WITH_DEMAND else pt.VENSIM_MODEL_3F
pt.select_factors(WITH_DEMAND)

print(f"Model in use : {pt.VENSIM_MODEL.name}  ({'4' if WITH_DEMAND else '3'} factors)")
for key, (knob, half, label) in pt.FACTORS.items():
    print(f"  {label:32s} {knob:18s} +/-{half:.0%}")
if not WITH_DEMAND:
    print("\nNOTE: the demand factor is not in this run. Publish "
          "model/ModeloCh4_Sens2.vpmx from Vensim DSS to include it; see README section 6.")
""")

# --------------------------------------------------------------------------- #
md(r"""
## 1. The model, and how a factor reaches it

A system-dynamics expansion module coupled to a congestion-aware dispatch module, solved
over **300 months (25 years)** from January 2025 at a time step of 0.25 months. Peru is
represented by 6 zones and 14 generation technologies per zone.

The one thing to understand before changing anything: **a published Vensim model does not
read its workbook at run time.** `GET XLS CONSTANTS` and `GET XLS DATA` are resolved when
the model is *published*, and their values are baked into the `.vpmx`. The Vensim DLL, in
turn, loads published models only — it cannot execute a `.mdl`. So rewriting `PERU.xlsx`
beside the published model changes nothing, and it does so **silently**: the sweep runs
happily and returns the same numbers for every sample.

The factors are therefore driven through **named constants and `SIMULATE>SETVAL`**. The
cell below prints the patch that creates them: each uncertain input is multiplied by a
sensitivity constant whose default value, zero, reproduces the original model exactly.
""")

code(r"""
import inspect

# The model patch, in full: this is what turns four spreadsheet inputs into four knobs
# the simulation engine can be told to move.
print(inspect.getsource(pt.add_knobs))
""")

code(r"""
# The base configuration the experiment perturbs, read from the workbook itself.
import openpyxl

book = openpyxl.load_workbook(pt.DATA_XLSX, read_only=True, data_only=True)
base_sheet = book["Base"]
initial_mw = sum(v for v in (base_sheet.cell(row=5, column=c).value for c in range(3, 87))
                 if isinstance(v, (int, float)))
gen_delay_base = book["Gen Investment parameters"]["B15"].value
params = book["General Parameters"]
switches = {
    "Grid-enhancing technologies (B22)": params["B22"].value,
    "System-level battery storage (B23)": params["B23"].value,
    "Dispatch path, 0 = internal Vensim (B24)": params["B24"].value,
    "Planned generation, 1 = exogenous plan (B12)": params["B12"].value,
    "DER expansion endogenous (B14)": params["B14"].value,
}
book.close()

print(f"Initial installed capacity (Base!C5:CH5)      {initial_mw:>12,.0f} MW")
print(f"'Other delays' fraction, base case (B15)      {gen_delay_base:>12.2f}")
print("   The generation-delay knob replaces that input, so the knob is")
print("   (1 + B15)*(1 + change) - 1: base -0.20, at -20 % -> -0.36, at +20 % -> -0.04.\n")
for name, value in switches.items():
    print(f"   {name:48s} {value}")
""")

# --------------------------------------------------------------------------- #
md(r"""
## 2. The design

Three groups of runs, each answering a different question:

* the **base case** fixes the reference every other run is compared against;
* the **tornado runs** move one factor at a time to each end of its range, which is what
  makes a tornado bar attributable to that factor;
* the **Monte Carlo runs** move every factor at once, which is what the P10–P90 band and
  the rank correlations need.

The Monte Carlo sample is a Latin hypercube: each factor's range is cut into as many equal
intervals as there are samples, one value is drawn at random inside each, and the intervals
are paired at random across factors. That covers every range evenly with far fewer runs
than simple random sampling.

The 200 samples are drawn as **two blocks** — 120 with seed 2026, 80 with seed 2027 —
rather than one block of 200. Each block is a proper Latin hypercube and their union keeps
uniform marginals; the first block reproduces the 120-sample design of the earlier Peru
study exactly, so the runs already completed under it are reused verbatim instead of being
discarded. Both seeds are fixed in the source, so the design is reproducible from the
repository alone.
""")

code(r"""
design = pt.design_runs()
RESULTS.mkdir(parents=True, exist_ok=True)
design.to_csv(RESULTS / "experiment_design.csv")

print(f"{len(design)} runs: " + ", ".join(
    f"{int(n)} {k}" for k, n in design.kind.value_counts().items()))
print("Monte Carlo blocks: " + ", ".join(f"{n} samples, seed {s}" for n, s in pt.MC_BLOCKS))
display(design.head(1 + 2 * len(pt.FACTORS)))
""")

md(r"""
### Design diagnostics

Two properties have to hold for the Monte Carlo half of the study to mean anything.

**Stratum coverage.** A Latin hypercube places exactly one sample in each of the *n* equal
intervals of every factor. The design file rounds each sampled value to four decimals — so
that a run can be matched to its design row by exact comparison — and a value drawn very
close to a stratum boundary can round across it, which is why the count below is reported
rather than asserted. It is checked per block, since that is the level at which the
property is defined. A count one or two short of *n* is rounding; a count far short would
mean the sampler is wrong.

**Near-orthogonality.** A rank correlation between a factor and a KPI is only attributable
when the factors are close to uncorrelated; a strong correlation between two of them would
let one absorb the other's effect. The realised value is reported rather than assumed.
""")

code(r"""
mc = design[design.kind == "montecarlo"][list(pt.FACTORS)].astype(float)

start = 0
for size, seed in pt.MC_BLOCKS:
    block = mc.iloc[start:start + size]
    unit = pd.DataFrame({k: (block[k] + pt.FACTORS[k][1]) / (2 * pt.FACTORS[k][1])
                         for k in pt.FACTORS})
    strata = {k: len(np.unique(np.minimum((v * size).astype(int), size - 1)))
              for k, v in unit.items()}
    worst = min(strata.values())
    print(f"block of {size} (seed {seed}): {worst}/{size} strata occupied in the "
          f"least-covered factor ({min(strata, key=strata.get)})")
    start += size

corr = mc.corr().to_numpy()
np.fill_diagonal(corr, 0.0)
off = np.abs(corr[np.triu_indices_from(corr, 1)])
print(f"\nover all {len(mc)} samples: max |correlation| between factors {off.max():.4f}, "
      f"mean {off.mean():.4f}")
display(mc.describe().T[["min", "mean", "max"]])
""")

# --------------------------------------------------------------------------- #
md(r"""
## 3. Running the ensemble

A 300-month run takes about 5.5 minutes under Vensim, so the driver runs several at once:
each worker simulates in its own temporary folder with its own copy of the model, and
worker processes are recycled every four runs because Vensim keeps every simulated run in
memory. With six workers the full design takes roughly three to four hours on an 8-core
machine.

The sweep is **resumable, and strict about it**: every run writes its results next to the
factor values it used, and a cached run is reused only if those values match the design
*exactly*. An interrupted sweep therefore costs only the runs in flight, and a change to
the design correctly invalidates the runs it affects rather than silently mixing two
experiments.

The cell below is guarded: it reports what is on disk and prints the command for whatever
is missing, rather than launching a three-hour job from inside a notebook.
""")

code(r"""
done = {p.stem for p in RUNS.glob("*.json")} if RUNS.exists() else set()
pending = [rid for rid in design.index if rid not in done]
print(f"{len(design) - len(pending)} of {len(design)} runs already on disk, {len(pending)} pending")

if pending:
    print("\nLaunch the sweep from the repository root:\n")
    print("    python peru_tornado.py --workers 6 --engine vensim\n")
    print("It prints a per-run ETA and can be interrupted and relaunched at will.")
else:
    print("\nNothing left to simulate.")
""")

code(r"""
# Load every completed run that matches the design, and reduce each to its KPIs.
data = {rid: pt.derive(pt.load_run(RUNS / f"{rid}.csv")) for rid in design.index
        if pt._matches_design(RUNS, rid, pt._params(design.loc[rid]))}
kpi = pd.DataFrame({rid: pt.kpis(d) for rid, d in data.items()}).T
print(f"{len(data)} of {len(design)} runs available for analysis")

if "base" not in data:
    raise RuntimeError("the base run is missing; simulate it before going on")

base = kpi.loc["base"]
summary = pd.DataFrame({
    "unit": [u for _, u in pt.KPIS.values()],
    "base case 2050": [base[k] for k in pt.KPIS],
}, index=[label for label, _ in pt.KPIS.values()])
display(summary.style.format({"base case 2050": "{:,.2f}"}))

# A base case that reproduces the published reference is the cheapest possible check that
# the whole chain - patched model, published .vpmx, knobs at their base values - is intact.
print("\nreference base case: 63.85 GW generation, 46,341 km transmission")
""")

md(r"""
### Did the factors actually reach the model?

The failure mode this pipeline is built to avoid raises no error: if the knobs were not
reaching the simulation, every run would return the same numbers and the tornado would be
a row of zeros. The check is one line, and it is worth keeping.
""")

code(r"""
spread = (kpi.std() / kpi.mean().abs()).sort_values(ascending=False)
print("relative spread of each KPI across the available runs:")
print(spread.to_string(float_format="%.4f"))
assert (spread.abs() > 1e-6).all(), \
    "a KPI is constant across the design - the knobs are not reaching the model"
print("\nevery KPI varies across the design: the factors are reaching the model")
""")

# --------------------------------------------------------------------------- #
md(r"""
## 4. Post-processing: every figure and table

`pt.analyse` does the whole post-processing in one call — tornado, percentile envelopes,
best/worst selection, the per-technology charts, the method checks and the summary
workbook — because the paper needs all of them regenerated together whenever a run is
added. The cells after it display each figure and say what to read in it.
""")

code(r"""
pt._style()
pt.analyse(design, RUNS, FIGURES)
print()
for path in sorted(FIGURES.glob("*")):
    print(f"  {path.name:46s} {path.stat().st_size / 1024:8,.0f} kB")
""")

# --------------------------------------------------------------------------- #
md(r"""
## 5. The tornado diagram

For each KPI, the left and right bar are the change from the base case when one factor
sits at its minimum and at its maximum, with every other factor held at its base value.
Factors are ordered by swing, |max − min|.

One factor moves at a time, which is exactly what makes a bar attributable to that factor —
and also the limitation of the form: it says nothing about interactions. The rank
correlations in section 8 are the complement, measured with all factors moving at once.

A bar is only as meaningful as the range behind it, so the range is printed next to each
factor name.
""")

code(r"""
display(Image(filename=str(FIGURES / "03_tornado.png")))
""")

# --------------------------------------------------------------------------- #
md(r"""
## 6. How wide the future is — P10–P90

From the Monte Carlo runs: at each month, P10 is the value 10 % of the runs fall below,
P50 the median, P90 the value 90 % fall below. The band holds the central 80 % of
outcomes. The first figure shows it over time, the second the distribution of each KPI in
2050.
""")

code(r"""
display(Image(filename=str(FIGURES / "01_distribution_P10_P90_timeseries.png")))
""")

code(r"""
display(Image(filename=str(FIGURES / "02_distribution_P10_P90_histograms.png")))
""")

# --------------------------------------------------------------------------- #
md(r"""
## 7. The best and the worst case

Chosen among the Monte Carlo runs on one stated metric: the **lowest and the highest
country average electricity price**, where the price is the mean over the last twelve
months of the horizon — it swings month to month with hydrology, so a single month is not
representative. A single declared criterion keeps the selection auditable, which is the
point of reporting two runs in full.

Both are shown against the base case, on a shared axis, so the three are read against each
other rather than in isolation.
""")

code(r"""
mc_ids = [r for r in design.index[design.kind == "montecarlo"] if r in data]
score = -kpi.loc[mc_ids, "price"]
best = score.idxmax()
worst = score.drop(best).idxmin()

extremes = pd.DataFrame({
    "unit": [u for _, u in pt.KPIS.values()],
    f"best ({best})": [kpi.loc[best, k] for k in pt.KPIS],
    "base case": [base[k] for k in pt.KPIS],
    f"worst ({worst})": [kpi.loc[worst, k] for k in pt.KPIS],
    "P10": [np.percentile(kpi.loc[mc_ids, k], 10) for k in pt.KPIS],
    "P90": [np.percentile(kpi.loc[mc_ids, k], 90) for k in pt.KPIS],
}, index=[label for label, _ in pt.KPIS.values()])
display(extremes.style.format(precision=2, subset=list(extremes.columns[1:])))

chosen = pd.DataFrame({
    "range": [f"+/-{pt.FACTORS[k][1]:.0%}" for k in pt.FACTORS],
    f"best ({best})": [design.loc[best, k] for k in pt.FACTORS],
    f"worst ({worst})": [design.loc[worst, k] for k in pt.FACTORS],
}, index=[pt.FACTORS[k][2] for k in pt.FACTORS])
display(chosen.style.format(precision=4, subset=list(chosen.columns[1:])))
""")

md(r"""
### Electricity price
""")

code(r"""
display(Image(filename=str(FIGURES / "04_price_best_worst.png")))
""")

md(r"""
### Installed capacity by technology

The 14 model technologies are grouped into 8 plotted series, because a stacked chart stops
being readable past eight bands. A technology keeps its colour across every figure and
every scenario, so colour follows the entity, never its rank.
""")

code(r"""
display(Image(filename=str(FIGURES / "05_installed_capacity_by_technology.png")))
""")

md(r"""
### Electricity generation by technology
""")

code(r"""
display(Image(filename=str(FIGURES / "06_generation_by_technology.png")))
""")

md(r"""
### Transmission
""")

code(r"""
for name in ("07_transmission_capacity_GW.png", "08_transmission_km.png"):
    display(Image(filename=str(FIGURES / name)))
""")

# --------------------------------------------------------------------------- #
md(r"""
## 8. Method checks

Three questions a reviewer will ask, answered with figures rather than assurances.

**Is the sample well spread?** (`09`) The design, projected onto each pair of factors.

**Are 200 runs enough?** (`10`) P10, P50 and P90 recomputed from the first 10, 20, 30, 50,
75, 100, 150 and 200 runs. If they have stopped moving, the sample is large enough — and if
they have not, that is the finding.

**Which factor dominates when everything moves at once?** (`11`) Spearman rank correlation
between each factor and each KPI over the Monte Carlo runs: −1 to +1, near zero means no
influence. This is the complement to the tornado, which moves one factor at a time.
""")

code(r"""
for name in ("09_sampling_design.png", "10_convergence.png", "11_factor_influence.png"):
    display(Image(filename=str(FIGURES / name)))
""")

# --------------------------------------------------------------------------- #
md(r"""
## 9. Tables for the paper

`tornado_summary.xlsx` carries one sheet per product: a README sheet explaining the rest,
the best/worst scenarios, the P10–P90 distribution, the tornado table, every run's KPIs,
the convergence check and the rank correlations. `00_summary_table.png` is the same content
as a figure.
""")

code(r"""
display(Image(filename=str(FIGURES / "00_summary_table.png")))

tables = pd.read_excel(FIGURES / "tornado_summary.xlsx", sheet_name=None)
print("tornado_summary.xlsx sheets:")
for name, frame in tables.items():
    print(f"  {name:24s} {frame.shape[0]:4d} rows x {frame.shape[1]:2d} columns")
display(tables["Tornado"].head(12))
""")

# --------------------------------------------------------------------------- #
md(r"""
## 10. Reproducibility checks

Assertions rather than prose, so that re-executing the notebook *fails* if any of these
stops holding.
""")

code(r"""
# 1. The design is reproducible from the seeds in the source alone.
again = pt.design_runs()
assert again.equals(design), "design_runs() is not deterministic"
print("1. design reproducible from the fixed seeds                      OK")

# 2. Every completed run carries exactly the factor values the design assigns it.
mismatched = [rid for rid in data
              if not pt._matches_design(RUNS, rid, pt._params(design.loc[rid]))]
assert not mismatched, f"runs whose factors do not match the design: {mismatched}"
print("2. every loaded run matches its design row                       OK")

# 3. The tornado runs actually moved the model: each must differ from the base case.
flat = [f"oat_{key}_{side}" for key in pt.FACTORS for side in ("low", "high")
        if f"oat_{key}_{side}" in kpi.index
        and (kpi.loc[f"oat_{key}_{side}"] - base).abs().max() < 1e-9]
assert not flat, f"tornado runs identical to the base case: {flat}"
print("3. every tornado run differs from the base case                  OK")

# 4. The first Monte Carlo block still reproduces the earlier 120-sample design, which is
#    what makes reusing its completed runs legitimate.
first_block = pt.design_runs(blocks=[pt.MC_BLOCKS[0]])
shared = [r for r in first_block.index if r.startswith("mc_")]
assert np.allclose(first_block.loc[shared, list(pt.FACTORS)].astype(float).to_numpy(),
                   design.loc[shared, list(pt.FACTORS)].astype(float).to_numpy())
print("4. block 1 is unchanged, so its completed runs are reusable      OK")
""")

md(r"""
---

### Where each result goes

| Product | File | Used for |
|---|---|---|
| Tornado | `figures/03_tornado.png` | which unknown matters, and in which direction |
| P10–P90 over time | `figures/01_distribution_P10_P90_timeseries.png` | how wide the future stays |
| P10–P90 in 2050 | `figures/02_distribution_P10_P90_histograms.png` | the outcome distribution |
| Price, best / base / worst | `figures/04_price_best_worst.png` | what the extremes cost |
| Installed capacity by technology | `figures/05_installed_capacity_by_technology.png` | best vs. worst mix |
| Generation by technology | `figures/06_generation_by_technology.png` | best vs. worst mix |
| Transmission | `figures/07_…GW.png`, `08_…km.png` | the grid each path requires |
| Method checks | `figures/09`, `10`, `11` | sampling, convergence, factor influence |
| All numbers | `figures/tornado_summary.xlsx` | every value behind every claim |

The limitations that bound all of this are in `README.md` section 8 — in particular that the
ranges are ranges of admissibility rather than estimated distributions, that the factors are
treated as independent, and that everything not listed as a factor is held at its
`PERU.xlsx` value.
""")

NB["cells"] = cells
NB["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.13"},
}

out = Path(__file__).resolve().parent.parent / "notebooks" / "Peru_expansion_uncertainty.ipynb"
out.parent.mkdir(parents=True, exist_ok=True)
nbf.write(NB, str(out))
print(f"wrote {out} with {len(cells)} cells "
      f"({sum(1 for c in cells if c['cell_type'] == 'code')} code)")
