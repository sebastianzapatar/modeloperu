# Power System Expansion Under Uncertainty — Peru

Tornado and P10–P90 uncertainty analysis of a system-dynamics power-system expansion
model applied to Peru, 2025–2050.

This repository contains everything needed to reproduce the experiment: the input
workbook, the Vensim model, the sensitivity design, the simulation driver, the
post-processing code, and a commented notebook that walks through the whole
pipeline and regenerates every figure.

It is the Peru companion to the Brazil–Chile study
([`vensimpypsa`](https://github.com/sebastianzapatar/vensimpypsa)) and shares its
model structure, so the three country studies are directly comparable.

---

## 1. What the experiment answers

Expansion plans rest on numbers nobody knows: what fuel will cost, how fast demand
will grow, how late projects will actually come online. The question here is not
*what will happen* but **which of those unknowns actually moves the system, and by
how much**.

The experiment therefore runs the model **209 times** — one base case, two runs per
factor for the tornado (eight), and **200 Monte Carlo samples** — and reports:

| Product | What it shows | Figure |
|---|---|---|
| **Tornado diagram** | each factor's effect on each KPI, one factor at a time, signed and ranked by swing | `03_tornado.png` |
| **P10–P90 envelopes** | how wide the outcome range stays over time, and in 2050 | `01_…timeseries.png`, `02_…histograms.png` |
| **Best and worst case** | the two Monte Carlo members at the extremes of a single stated metric, against the base case | `04`–`08` |
| **Generation by technology** | best vs. worst vs. base, stacked, TWh/year | `06_generation_by_technology.png` |
| **Installed capacity by technology** | best vs. worst vs. base, stacked, GW | `05_installed_capacity_by_technology.png` |
| **Method checks** | sampling design, convergence of the percentiles, rank correlation per factor | `09`, `10`, `11` |

---

## 2. What the model is

A system-dynamics expansion model coupled to a congestion-aware dispatch layer,
solved in Vensim DSS over **300 months (25 years)** from January 2025 at a time step
of 0.25 months. Peru is represented by 6 zones and 14 generation technologies per
zone. Initial installed capacity is **14,057 MW** and the base case ends 2050 at
**63.85 GW** of generation and **46,341 km** of transmission.

Everything country-specific lives in `PERU.xlsx`; the `.mdl` is the same structure
used for Brazil and Chile.

### How a factor reaches the model — and why it has to work this way

This is the single most important thing to understand before changing anything.

The model reads its inputs through `GET XLS CONSTANTS` / `GET XLS DATA`. One might
therefore expect to vary a factor by rewriting `PERU.xlsx` and reloading the model.
**That does not work here.** When Vensim DSS *publishes* a model, it bakes the
workbook values into the `.vpmx`; the published model never reads the workbook
again at run time. The Vensim DLL, in turn, loads published models only — it cannot
execute a `.mdl`. So a published model is a closed box: rewriting the workbook
beside it changes nothing, **silently**, and every run returns identical results.

Verified directly, four ways: a doctored workbook (initial capacity set to 113 GW)
placed beside the published model in a private sandbox, in a fresh process, still
produced the shipped 14,057 MW; so did doctoring the workbook in the model folder,
and the original publish folder; and a 12-run sweep driven by workbook rewriting
produced twelve byte-identical result sets.

The pipeline therefore varies factors through **named constants and
`SIMULATE>SETVAL`**. `peru_tornado.py --make-vensim-model` rewrites the model
equations so that each uncertain input is multiplied by a sensitivity constant
whose default value reproduces the original model exactly:

| Factor | Knob | What the patched equation does |
|---|---|---|
| Fuel prices | `Sens Fuel Price` | `Fuel Cost[ZTech] = Fuel Cost Data[ZTech] × (1 + knob)` |
| Demand growth | `Sens Demand` | `National demand[…] = National demand Data[…] × (1 + knob × Time / FINAL TIME)` |
| Generation project delays | `Sens Gen Delay` | `Other Generation delays[ZTech] = knob × (Construction time + Licensing time)` |
| Transmission project delays | `Sens Trans Delay` | every project stage — tender, investor selection, licensing, construction, for both lines and GETs — scaled by `(1 + knob)` |

The demand knob is a **growth-path** factor, not a level shift: the multiplier is 1
at January 2025 and `1 + Sens Demand` at January 2050, so the starting point is
untouched and only the growth rate varies. That is what "an increase in demand"
means for an expansion study.

The 12-month planning cycle (`Expansion Plan Delay`) is deliberately **not** scaled:
the model uses it as the period in a `MODULO(Time, …)`, so changing it would shift
the planning calendar rather than delay projects.

> **Publishing cannot be automated.** There is no Vensim command to publish a model,
> and the DLL cannot load a `.mdl`. Adding or changing a knob therefore requires one
> manual step in the Vensim DSS GUI — see §6.

---

## 3. The design

| Group | Runs | Description |
|---|---|---|
| Base case | 1 | every factor at its `PERU.xlsx` value (0 % change) |
| Tornado (one at a time) | 8 | each factor at its minimum and its maximum, the others at 0 % |
| Monte Carlo | 200 | all four factors vary simultaneously |
| **Total** | **209** | each run simulates 2025-01 to 2050-01, 300 months at a 0.25-month step |

### Factors and ranges

| Factor | Range | What it represents |
|---|---|---|
| **Fuel prices** | −15 % / +15 % | `Fuel Cost` of every technology that burns fuel |
| **Demand growth** | −20 % / +20 % | the 2050 demand level reached by the growth path |
| **Generation project delays** | −20 % / +20 % | licensing + construction time of generation projects |
| **Transmission project delays** | −20 % / +20 % | tender, investor selection, licensing and construction of lines and GETs |

Generation delays are expressed relative to the **base case of the workbook**, not
to the planned time. `Gen Investment parameters!B15` is −0.2 in `PERU.xlsx`, i.e.
the realised delay is 80 % of the planned one, so the knob is set to
`(1 + B15) × (1 + change) − 1`: base −0.20, at −20 % → −0.36, at +20 % → −0.04. This
is the same convention used for transmission, so the two delay bars of the tornado
are comparable.

Distributions are uniform and independent — there is no information to justify
another shape — and the ranges are **ranges of admissibility, not estimated
distributions**. The ranking is robust to that; the absolute width of the P10–P90
band is not.

### Monte Carlo sampling

A Latin hypercube: each factor's range is cut into as many equal intervals as there
are samples, one value is drawn at random inside each, and the intervals of the
factors are paired at random. This covers every range evenly with far fewer runs
than simple random sampling.

The 200 samples are drawn as **two blocks** — 120 with seed 2026 and 80 with seed
2027 — rather than one block of 200. Each block is a proper Latin hypercube and
their union keeps uniform marginals. The blocking exists so that a smaller design
stays a prefix of a larger one: the first block is exactly the 120-sample design of
the earlier three-factor Peru study, and because the sampler draws one column per
factor in a fixed order with the demand column appended last, the *other three*
factors keep the values they had there. (Adding a fourth factor still invalidates
every Monte Carlo run, since each sample now also carries a non-zero demand value;
what survived was the base case and the six original one-at-a-time runs. The
three-factor results are archived under `results/peru_3factor/`.)

Both seeds and the factor order are fixed in `peru_tornado.py`, so the whole design
is reproducible from the source alone; it is also written out to
`results/peru/experiment_design.csv`.

> **The factor order in `ALL_FACTORS` is part of the design, not cosmetic.** The
> Latin hypercube assigns its columns in that order, so reordering the dict silently
> produces a different design and invalidates every completed run.

---

## 4. KPIs, and how best and worst are chosen

| KPI | Model variable | Evaluated at |
|---|---|---|
| Total transmission [km] | `Total Transmission` (demand connection + generation connection + interconnection) | Jan 2050 |
| Transmission capacity [GW] | `Total Interconnection Capacity` / 1000 | Jan 2050 |
| Installed generation capacity [GW] | `Total Generation Capacity` | Jan 2050 |
| Country average price [USD/MWh] | mean of `Electricity Tariff[Regulated, zone]` over the active zones | mean of the last 12 months (Feb 2049 – Jan 2050) |

The price is averaged over twelve months because it swings month to month with
hydrology; a single month is not representative.

**Best and worst** are chosen among the 200 Monte Carlo runs on one stated metric:
**lowest / highest country average price**. A single declared criterion keeps the
selection auditable, which is the point of reporting two runs in full. A composite
score over all four KPIs is available by setting
`BEST_WORST_CRITERION = "composite"`.

---

## 5. Repository contents

```
model/
  ModeloCh4_Correccion.mdl      the original Vensim model (source)
  ModeloCh4_Sens.mdl            + the 3 original knobs (fuel, gen delay, trans delay)
  ModeloCh4_Sens.vpmx           published; what the DLL executes for the 3-factor design
  ModeloCh4_Sens2.mdl           + the demand knob; generated by --make-vensim-model
  ModeloCh4_Sens2.vpmx          published; needed for the 4-factor design (see §6)
  PERU.xlsx                     all Peru data and parameters
peru_tornado.py                 model patching, design, Vensim/PySD drivers, analysis, figures
notebooks/
  Peru_expansion_uncertainty.ipynb   commented end-to-end walkthrough, with outputs
tools/
  build_notebook.py             regenerates the notebook from plain Python, so the
                                .ipynb is never hand-edited
  run_notebook.py               executes the notebook headlessly, in place
results/peru/
  experiment_design.csv         the design actually executed
  runs/<run_id>.csv / .json     monthly results of each run / the factor values it used
  run_log.csv                   finish time, duration and status of every run
  figures/                      every figure (PNG) and tornado_summary.xlsx
requirements.txt
LICENSE                         MIT (code)
LICENSE-DATA                    CC BY 4.0 (model, data, figures, documentation)
CITATION.cff
```

---

## 6. Running it

### Requirements

- **Windows** with licensed **64-bit Vensim DSS** (`C:\Windows\System32\vendll64.dll`).
  Without it the driver falls back to **PySD**, which is roughly 5× slower
  (~30 min per run) but needs no licence; pass `--engine vensim` to fail loudly
  instead of falling back silently.
- **Python ≥ 3.10**, `pip install -r requirements.txt`.
- A **short, ASCII path without spaces**, outside OneDrive or Dropbox — paths are
  handed to the Vensim DLL as strings, and a synced folder will try to sync every
  temporary file the sweep writes.

### Publishing the model (one manual step, once per change to the knobs)

`ModeloCh4_Sens.vpmx` carries only the three original knobs; `ModeloCh4_Sens2.vpmx`
adds demand and is what the published results use. To rebuild it, or to add a
further knob:

```powershell
python peru_tornado.py --make-vensim-model     # writes model/ModeloCh4_Sens2.mdl
```

then, in **Vensim DSS**:

1. open `model/ModeloCh4_Sens2.mdl`
2. *Model → Units Check* — it must pass
3. *File → Publish* → save as **`model/ModeloCh4_Sens2.vpmx`**, in the same folder

Confirm the published model exposes all four constants: `Sens Fuel Price`,
`Sens Demand`, `Sens Gen Delay`, `Sens Trans Delay`.

### The sweep

```powershell
# 4-factor design, used automatically once ModeloCh4_Sens2.vpmx exists
python peru_tornado.py --workers 6

# force one or the other
python peru_tornado.py --workers 6 --demand on
python peru_tornado.py --workers 6 --demand off

# figures and tables only, from the runs already on disk
python peru_tornado.py --plots-only

# quick check before committing to the full sweep: 5 short runs in a scratch folder
python peru_tornado.py --engine vensim --samples 5 --final-time 24 --out results/_check
```

A full 300-month run takes about 6 min under Vensim; with 6 parallel workers the
209 runs take roughly **3.5 hours** on an 8-core machine. Each worker simulates in
its own temporary folder with its own copy of the model, and worker processes are
recycled every four runs because Vensim keeps every simulated run in memory.

**The sweep is resumable.** Every run writes `runs/<run_id>.csv` with its results and
`runs/<run_id>.json` with the factor values it used; a cached run is reused **only
if its factor values match the design exactly**, so an interrupted sweep costs only
the runs that were in flight, and a change to the design correctly invalidates the
runs it affects.

One deliberate exception: a knob the cached run does not mention counts as zero,
and only as zero. Each knob enters the model as a multiplier whose default value
reproduces the original equation exactly, so a run made before a knob existed *is*
the run that knob would have produced at zero. That is what let the base case and
the six one-at-a-time runs of the three-factor study survive the addition of the
demand factor, while all 200 Monte Carlo samples — which give demand a non-zero
value — were correctly invalidated and re-simulated. The three-factor results are
kept in `results/peru_3factor/` rather than overwritten.

### Things that go wrong

| Symptom | Cause / fix |
|---|---|
| Every run returns identical results, **no error raised** | the factors are not reaching the model. With this pipeline that means the `.vpmx` was published from the unpatched `.mdl`. Check `Total Generation Capacity` at Jan 2050: the base is 63.85 GW, and the tornado runs must differ from it |
| `Vensim rejected SETVAL Sens …` | the `.vpmx` was published from the original `.mdl` instead of `ModeloCh4_Sens(2).mdl` |
| `Engine: pysd` when Vensim was expected | `ModeloCh4_Sens*.vpmx` or `vendll64.dll` not found; use `--engine vensim` to fail explicitly |
| `Vensim could not load the published model` | no active Vensim DSS licence on this machine |
| A short test run shows a huge demand effect | expected: the demand ramp is written as `Time / FINAL TIME`, so `--final-time 24` compresses the whole 25-year demand increase into two years. `--final-time` is for smoke tests only; every reported run uses the full 300 months |
| Base case differs from 63.85 GW / 46,341 km | `PERU.xlsx` is not the base configuration |

---

## 7. Reading the figures

**Tornado (`03`).** For each KPI, the left and right bar are the change from the base
case when a factor sits at its minimum and at its maximum, every other factor held
at base. Factors are ordered by swing, |max − min|. One factor moves at a time, which
is what makes a bar attributable; the rank correlations in `11` are the complement,
measured with all factors moving at once.

**P10–P90 (`01`, `02`).** From the Monte Carlo runs: at each month, P10 is the value
10 % of runs fall below, P50 the median, P90 the value 90 % fall below. The band
holds the central 80 % of outcomes. `10` re-computes those percentiles from the
first 10, 20, 30, 50, 75, 100, 150 and 200 runs: if they have stopped moving, the
sample is large enough — and that is a check, not a claim.

**Colour.** The categorical palette is fixed in slot order and never cycled, so a
technology keeps its colour across every figure and every scenario; it was verified
with a colour-vision-deficiency validator rather than by eye (worst adjacent-pair
separation ΔE 9.1 for protanopia, 19.6 for normal vision, on a light surface). The
14 model technologies are grouped into 8 plotted series — hydro reservoir, hydro
run-of-river, thermal 1, thermal 2, onshore wind, solar PV, solar PV + BESS, and a
residual "other" (biomass, geothermal, offshore wind, distributed generation) —
because a stacked chart stops being readable past eight bands.

---

## 8. Limitations

Stated plainly, because they bound what the tornado means:

1. **Ranges are ranges of admissibility, not estimated distributions.** A uniform
   marginal over ±20 % says what is plausible, not what is likely.
2. **The factors are treated as independent**, and each change applies from the start
   of the simulation and holds to 2050.
3. **Everything else is held at its `PERU.xlsx` value** — hydrology, investment
   costs, discount rate, reserve margins.
4. **200 samples over four factors** resolve first-order effects and the shape of the
   output distribution; they do not resolve high-order interactions. Figure `10`
   reports whether the percentiles have converged.
5. **One representative day per month** in the dispatch layer, so sustained scarcity
   events and intra-month storage cycling are under-represented.
6. **Retirements are exogenous**: the technical-lifetime parameter exists in the
   workbook but is not wired to a retirement flow in this model version.
7. Under the PySD engine, `DELAY MATERIAL` is replaced by `DELAY FIXED` (equivalent
   here, since every delay time in this model is constant during a simulation) and
   delay times are rounded to the 0.25-month step — under a week per stage.
8. The demand ramp is expressed as a fraction of `FINAL TIME`, so it is tied to the
   length of the simulation. That is exact for every reported run, which all use the
   full 300 months, but it means a shortened test run is not a scaled-down version of
   the real one.

---

## 9. Licence and citation

- **Code** (`peru_tornado.py`, the notebook): MIT — see `LICENSE`.
- **Model, data, figures and documentation** (`model/`, `results/`, this README):
  CC BY 4.0 — see `LICENSE-DATA`.

See `CITATION.cff` for how to cite. Nothing here is "available upon request": the
model, the data, the design and the code are all in this repository.
