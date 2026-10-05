"""Resolve every bibliography entry against Crossref and report what was verified.

A reference list is the one part of a manuscript where a plausible-looking
invention is both easy to produce and fatal. This script therefore never writes a
citation it has not matched: it queries Crossref for each (author, title, year)
triple, keeps the best match only when the title similarity clears a threshold,
and prints the unmatched ones so they can be completed by hand from the source
the authors actually read.

    python tools/verify_refs.py            # verify the list below
    python tools/verify_refs.py --bib      # emit BibTeX for the matched entries
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

MAILTO = "pansezapata@gmail.com"  # Crossref asks for a contact in the polite pool

#: (bibtex key, first author family name, title as cited, year as cited).
#: Year is advisory - Crossref's is authoritative and a mismatch is reported.
WANTED: list[tuple[str, str, str, str]] = [
    # --- Peruvian system, from the manuscript draft -------------------------
    ("rudnick2019", "Rudnick", "Learning from developing country power market experiences: the case of Peru", "2019"),
    ("perezreyes2009", "Perez-Reyes", "Measuring efficiency and productivity change (PTF) in the Peruvian electricity distribution companies after reforms", "2009"),
    ("sauma2011", "Sauma", "Transmission expansion planning in the Andean region", "2011"),
    ("fiestas2024", "Fiestas", "non-conventional renewable energy Peru electricity matrix emissions", "2024"),
    # --- System dynamics in electricity -------------------------------------
    ("arango2002", "Arango", "Simulating the dynamics of electricity markets investment", "2002"),
    ("ford2001", "Ford", "Waiting for the boom: a simulation study of power plant construction in California", "2001"),
    ("dyner2001", "Dyner", "From planning to strategy in the electricity industry", "2001"),
    ("sterman2000", "Sterman", "Business dynamics: systems thinking and modeling for a complex world", "2000"),
    ("barlas1996", "Barlas", "Formal aspects of model validity and validation in system dynamics", "1996"),
    ("zapata2022", "Zapata", "Simulation of 100% renewable electricity system Colombia transition", "2022"),
    ("zapata2023", "Zapata", "Transmission expansion delays renewable energy system dynamics Colombia", "2023"),
    ("herrera2019", "Herrera", "Transmission expansion planning Colombia system dynamics", "2019"),
    ("morcillo2017", "Morcillo", "Simulation of demand growth scenarios in the Colombian electricity market", "2017"),
    ("ochoa2015", "Ochoa", "Expansion of the electricity sector interconnection system dynamics", "2015"),
    ("ritter2019", "Ritter", "Effects of delays in transmission grid expansion Europe", "2019"),
    ("assili2008", "Assili", "An improved mechanism for capacity payment based on system dynamics", "2008"),
    ("eker2018", "Eker", "Model validation uncertainty energy system dynamics", "2018"),
    # --- Planning models, optimisation --------------------------------------
    ("gacitua2018", "Gacitua", "A comprehensive review on expansion planning: models and tools for energy policy analysis", "2018"),
    ("babatunde2019", "Babatunde", "Power system flexibility: a review", "2019"),
    ("goke2022", "Goke", "Stabilized billion-node optimization storage grid expansion", "2022"),
    ("herding2024", "Herding", "Integrated generation and transmission expansion planning renewable", "2024"),
    ("mirzapour2023", "Mirzapour", "Grid-enhancing technologies transmission congestion renewable", "2023"),
    ("brown2018", "Brown", "PyPSA: Python for Power System Analysis", "2018"),
    # --- Verified Renewable Energy articles (Crossref, ISSN 0960-1481) -------
    ("valipour2026", "Valipour", "Boosting renewable hosting capacity via TCSC-enhanced transmission system planning: P-robust stochastic approach", "2026"),
    ("tao2025", "Tao", "An investment game model for offshore power grid multi-stage expansion planning", "2025"),
    ("delrio2026", "Del Rio", "Financing renewable energy projects in Latin America beyond resource quality, levelized cost of electricity, project feasibility, and the role of the weighted average cost of capital", "2026"),
    ("luo2024", "Luo", "A hybrid system dynamics model for power mix trajectory simulation in liberalized electricity markets considering carbon and capacity policy", "2024"),
    ("sousa2024", "Sousa", "The role of storage and flexibility in the energy transition: Substitution effect of resources with application to the Portuguese electricity system", "2024"),
    ("sheng2026", "Sheng", "Optimal planning of urban-scale rooftop PV systems for maximizing renewable energy integration by reducing grid congestion-induced curtailments", "2026"),
    ("zhao2026", "Zhao", "Optimal allocation of renewable power sources for outbound supporting in trans-regional power transmission of China", "2026"),
    ("sun2026", "Sun", "Mitigating local opposition in renewable energy projects expansion: Evidence from Denmark", "2026"),
    ("dasilva2025", "da Silva", "Renewable electricity expansion and sustainable development in Brazil: A regional efficiency analysis", "2025"),
    ("lin2026", "Lin", "Green electricity system dynamics under the carbon border adjustment mechanism", "2026"),
    ("alvarez2026", "Alvarez-Pineiro", "Probabilistic assessment of Spain's 2030 electricity system via Monte Carlo analysis", "2026"),
    ("liu2024", "Liu", "Multi-period optimal capacity expansion planning scheme of regional integrated energy systems considering multi-time scale uncertainty", "2024"),
]

#: Below this title similarity the match is rejected rather than guessed at.
THRESHOLD = 0.62


def query(author: str, title: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "query.bibliographic": title,
        "query.author": author,
        "rows": "5",
        "select": "title,author,issued,volume,page,DOI,container-title,type,article-number,publisher",
        "mailto": MAILTO,
    })
    url = f"https://api.crossref.org/works?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": f"ref-check (mailto:{MAILTO})"})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))["message"]["items"]


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()


def best_match(author: str, title: str) -> tuple[dict | None, float]:
    try:
        items = query(author, title)
    except Exception as error:  # noqa: BLE001 - network failures are reported, not raised
        print(f"    query failed: {error}", file=sys.stderr)
        return None, 0.0
    best, score = None, 0.0
    for item in items:
        candidate = (item.get("title") or [""])[0]
        s = similarity(title, candidate)
        if s > score:
            best, score = item, s
    return best, score


def bibtex(key: str, item: dict) -> str:
    authors = " and ".join(
        f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
        for a in item.get("author", [])
    )
    year = str(item.get("issued", {}).get("date-parts", [[""]])[0][0])
    title = (item.get("title") or [""])[0]
    journal = (item.get("container-title") or [""])[0]
    pages = item.get("page") or item.get("article-number") or ""
    fields = [f"  author  = {{{authors}}}", f"  title   = {{{title}}}", f"  year    = {{{year}}}"]
    if journal:
        fields.append(f"  journal = {{{journal}}}")
    if item.get("volume"):
        fields.append(f"  volume  = {{{item['volume']}}}")
    if pages:
        fields.append(f"  pages   = {{{pages}}}")
    if item.get("DOI"):
        fields.append(f"  doi     = {{{item['DOI']}}}")
    kind = "article" if item.get("type") == "journal-article" else "misc"
    return "@" + kind + "{" + key + ",\n" + ",\n".join(fields) + "\n}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bib", action="store_true", help="emit BibTeX for matched entries")
    args = parser.parse_args()

    matched, unmatched, entries = [], [], []
    for key, author, title, year in WANTED:
        item, score = best_match(author, title)
        time.sleep(0.4)  # polite pool
        if item is None or score < THRESHOLD:
            unmatched.append((key, author, title, year, round(score, 2)))
            if not args.bib:
                print(f"[  NO ] {key:16s} best similarity {score:.2f}")
            continue
        got_year = str(item.get("issued", {}).get("date-parts", [[""]])[0][0])
        journal = (item.get("container-title") or [""])[0]
        matched.append(key)
        entries.append(bibtex(key, item))
        if not args.bib:
            flag = "" if got_year == year else f"  (year cited {year}, Crossref {got_year})"
            print(f"[ OK  ] {key:16s} {score:.2f}  {journal[:46]:46s} {item.get('DOI','')}{flag}")

    if args.bib:
        print("".join(entries))
        return 0

    print(f"\nmatched {len(matched)} of {len(WANTED)}; {len(unmatched)} need manual entry")
    for key, author, title, year, score in unmatched:
        print(f"   {key}: {author} ({year}) - {title[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
