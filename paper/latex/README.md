# Paper: Transmission Expansion and Reliability in Peru's Energy Transition

LaTeX source for the manuscript and its supplementary material, targeted at
**Renewable Energy** (Elsevier, `elsarticle` class).

```
main.tex                 manuscript (30 pp.)
supplementary.tex        supplementary material (14 pp.)
refs.bib                 bibliography, 56 entries - generated, see below
sections/                one file per section, so co-authors do not collide
  01_introduction.tex    04_methodology.tex      07_conclusions.tex
  02_literature.tex      05_results.tex          fig_model_diagram.tex
  03_peru.tex            06_discussion.tex
figures/                 every figure, copied from results/peru/figures/ plus mapa.png
tools/                   bibliography and consistency checks (see below)
```

## Building

MiKTeX or TeX Live with `elsarticle`, `siunitx`, `tikz`, `booktabs`, `natbib`.

```powershell
latexmk -pdf main.tex
latexmk -pdf supplementary.tex
```

Both compile with **no undefined references and no undefined citations**. If you
add a citation, re-run `latexmk` twice (or once more after BibTeX).

## The bibliography is generated, not hand-written

`refs.bib` is produced by `tools/build_bib.py` from a list of **pinned DOIs**, each
checked by hand against the work it is meant to cite. Metadata is then fetched from
Crossref rather than typed, so the bibliography cannot drift from the record.

```powershell
python tools/build_bib.py > refs.bib
```

Of the 56 entries, **21 are from Renewable Energy** (ISSN 0960-1481), which matters
for the target journal.

### Six entries still need you

Six references come from the draft the co-authors supplied and no DOI search
resolved them. They are in the `MANUAL` block of `tools/build_bib.py`, carry only
what the draft states, and are marked in `refs.bib` with
`note = {... to be completed}`:

| Key | Cited as |
|---|---|
| `campodonico2022` | Campodónico & Carrera (2022) |
| `sempertegui2017` | Semperteguí (2017) |
| `colinacalvo2024` | Colina-Calvo (2024) |
| `reyes2025` | Reyes (2025) |
| `torres2026` | Torres (2026) |
| `enerdata2024` | Enerdata (2024) |

Please complete volume, pages and DOI from the copy you actually read, or drop the
citation. **Nothing in these entries has been inferred** — inventing plausible
metadata is the one failure a reference list cannot survive.

## Checks

```powershell
python tools/check_citations.py        # cited-but-undefined, defined-but-uncited
python tools/strip_section_numbers.py  # idempotent; fixes duplicated S-numbers
```

`check_citations.py` is worth running before every submission: a successful LaTeX
build does not catch a key that is cited but missing once the `.bbl` is stale.

## Notes for the co-authors

- **The title page is a separate file**, as Elsevier requires. It carries the author
  details, ORCID placeholders, the word count and every declaration. Several fields are
  marked `\todo`: postal address and ORCIDs.
- **The AI declaration is included** in both `titlepage.tex` and `main.tex`, in the
  section Elsevier mandates (*Declaration of generative AI and AI-assisted technologies
  in the manuscript preparation process*), placed immediately before the references and
  using Elsevier's prescribed wording. Edit it if the description of what was used does
  not match what you want to state — it is your declaration, not ours.
- **Notes are hidden.** `\notesfalse` in `main.tex` hides the red `\todo` markers;
  set `\notestrue` to show them while drafting.
- **Data availability** points at the public GitHub repository.
- **No acknowledgements section** in `main.tex`; the CRediT statement is there. The
  funding statement on the title page declares no specific grant.
- Cross-references into the supplementary are written as plain text (`Supplementary
  Material~S3`) rather than `\ref`, because `\ref` cannot cross documents. If you
  renumber the supplementary sections, update those by hand — `grep -n
  "Supplementary Material~S" sections/*.tex`.

## Where the numbers come from

Every number in the results section comes from the 129-run experiment in
`../../` (the `peru-expansion-uncertainty` repository): `results/peru/figures/
tornado_summary.xlsx` carries the tornado table, the P10–P90 distribution, the
best/worst scenarios, the rank correlations and every run's KPIs. The figures in
`figures/` are copied from `results/peru/figures/` unchanged.

To regenerate figures and tables after re-running the model:

```powershell
cd ..\..                     # repository root
python peru_tornado.py --plots-only --demand on
copy results\peru\figures\*.png paper\latex\figures\
```
