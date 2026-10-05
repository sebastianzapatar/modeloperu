"""Generate refs.bib from a pinned DOI list, with metadata fetched from Crossref.

Every entry here is pinned to a DOI that was checked by hand against the work it
is meant to cite, and the metadata is then fetched rather than typed, so the
bibliography cannot drift from the record. Entries that could not be resolved to
a DOI are kept in MANUAL below, carry only what the source actually states, and
are marked in the output so the authors complete them from the copy they read.

    python tools/build_bib.py > refs.bib
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

MAILTO = "pansezapata@gmail.com"

#: key -> DOI. Checked individually: the DOI resolves to the work cited.
PINNED: dict[str, str] = {
    # Peruvian and Latin American power systems
    "rudnick2019": "10.1596/1813-9450-8772",
    "perezreyes2009": "10.1016/j.enpol.2009.01.037",
    "fiestas2024": "10.1016/j.jclepro.2024.143389",
    "delacruz2024": "10.3390/su16124964",
    "valdivia2024": "10.1109/concapan63470.2024.10933870",
    "guliev2022": "10.46272/2409-3416-2021-9-4-120-133",
    "sauma2006": "10.1007/s11149-006-9003-y",
    "delrio2026": "10.1016/j.renene.2026.126194",
    "dasilva2025": "10.1016/j.renene.2025.122996",
    "herrera2019": "10.1016/j.jup.2019.05.010",
    # System dynamics in electricity markets
    "barlas1996": "10.1002/(SICI)1099-1727(199623)12:3<183::AID-SDR103>3.0.CO;2-4",
    "ford2001": "10.1016/S0301-4215(01)00035-0",
    "dyner2001": "10.1016/S0301-4215(01)00040-4",
    "arango2007": "10.1016/j.seps.2006.06.004",
    "assili2008": "10.1016/j.enpol.2008.06.034",
    "morcillo2018": "10.1016/j.apenergy.2018.02.104",
    "zapata2018": "10.1016/j.seta.2018.10.008",
    "luo2024": "10.1016/j.renene.2024.121164",
    "lin2026": "10.1016/j.renene.2025.124069",
    # Expansion planning, delays and uncertainty
    "gacitua2018": "10.1016/j.rser.2018.08.043",
    "babatunde2020": "10.1016/j.egyr.2019.11.048",
    "ritter2019": "10.3390/en12163098",
    "herding2024": "10.1016/j.segan.2024.101349",
    "brown2018": "10.5334/jors.188",
    # Renewable Energy (Elsevier), ISSN 0960-1481
    "valipour2026": "10.1016/j.renene.2025.124922",
    "tao2025": "10.1016/j.renene.2024.121938",
    "sousa2024": "10.1016/j.renene.2024.120694",
    "sheng2026": "10.1016/j.renene.2026.125559",
    "sun2026": "10.1016/j.renene.2025.124902",
    "alvarez2026": "10.1016/j.renene.2026.125204",
    "sadeghian2026": "10.1016/j.renene.2025.124030",
    "oyarzun2026": "10.1016/j.renene.2026.126127",
    "antoniolli2022": "10.1016/j.renene.2022.01.072",
    "wu2025": "10.1016/j.renene.2025.123473",
    "boateng2026": "10.1016/j.renene.2026.125970",
    # Method: sampling, sensitivity, and system dynamics model confidence
    "mckay1992": "10.1145/167293.167637",
    "homma1996": "10.1016/0951-8320(96)00002-6",
    "oliva2004": "10.1002/sdr.298",
    "richardson2024": "10.1002/sdr.1769",
    "forrester1997": "10.1038/sj.jors.2600946",
    "simshauser2010": "10.1016/j.tej.2009.12.006",
}

#: Works cited in the manuscript that no DOI search resolved. Only what the
#: source states is written here; nothing is inferred. Marked in the output.
MANUAL = r"""
% ---------------------------------------------------------------------------
% UNRESOLVED ENTRIES
% These are cited in the draft the co-authors supplied but no DOI could be
% confirmed for them. The fields below carry only what the draft states. Please
% complete volume / pages / DOI from the copy you actually read, or drop the
% citation. Nothing here has been inferred.
% ---------------------------------------------------------------------------
@article{campodonico2022,
  author  = {Campod{\'o}nico, Humberto and Carrera, Carlos},
  title   = {Transici{\'o}n energ{\'e}tica y energ{\'i}as renovables no convencionales en el Per{\'u}},
  year    = {2022},
  note    = {Reference supplied by the co-authors; bibliographic details to be completed}
}

@article{sempertegui2017,
  author  = {Semperteg\'u{}i, Ricardo},
  title   = {Regional electricity market integration in the {A}ndean region: institutional barriers},
  year    = {2017},
  note    = {Reference supplied by the co-authors; bibliographic details to be completed}
}

@article{colinacalvo2024,
  author  = {Colina-Calvo, A.},
  title   = {Fossil fuel dependence and renewable energy development in {P}eru},
  year    = {2024},
  note    = {Reference supplied by the co-authors; bibliographic details to be completed}
}

@article{reyes2025,
  author  = {Reyes, R.},
  title   = {Energy transition policy frameworks in {P}eru and {C}hile: a comparative assessment},
  year    = {2025},
  note    = {Reference supplied by the co-authors; bibliographic details to be completed}
}

@article{torres2026,
  author  = {Torres, J.},
  title   = {Onshore wind development in {P}eru after the suspension of renewable auctions},
  year    = {2026},
  note    = {Reference supplied by the co-authors; bibliographic details to be completed}
}

@misc{enerdata2024,
  author       = {{Enerdata}},
  title        = {{P}eru energy information: natural gas consumption by sector},
  year         = {2024},
  howpublished = {Enerdata Global Energy and CO$_2$ Data},
  note         = {Reference supplied by the co-authors; access date to be completed}
}

% ---------------------------------------------------------------------------
% BOOKS AND SOFTWARE (no DOI; details are stable and were checked by hand)
% ---------------------------------------------------------------------------
@book{sterman2000,
  author    = {Sterman, John D.},
  title     = {Business Dynamics: Systems Thinking and Modeling for a Complex World},
  publisher = {Irwin/McGraw-Hill},
  address   = {Boston},
  year      = {2000},
  isbn      = {9780072389159}
}

@manual{nrel2024,
  author       = {{National Renewable Energy Laboratory}},
  title        = {Annual Technology Baseline: Electricity},
  organization = {NREL},
  address      = {Golden, CO},
  year         = {2024},
  url          = {https://atb.nrel.gov/}
}

@manual{ventana2024,
  author       = {{Ventana Systems}},
  title        = {Vensim {DSS} Reference Manual},
  organization = {Ventana Systems, Inc.},
  year         = {2024}
}
"""


def fetch(doi: str) -> dict:
    url = "https://api.crossref.org/works/" + doi
    request = urllib.request.Request(url, headers={"User-Agent": f"bib-build (mailto:{MAILTO})"})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))["message"]


#: BibTeX is not 8-bit clean: a UTF-8 accented character in refs.bib reaches the
#: .bbl as a broken byte sequence and pdflatex then refuses the file. Every
#: non-ASCII character is therefore written as its LaTeX command.
ACCENTS = {
    "á": r"\'a", "é": r"\'e", "í": r"\'i", "ó": r"\'o", "ú": r"\'u",
    "Á": r"\'A", "É": r"\'E", "Í": r"\'I", "Ó": r"\'O", "Ú": r"\'U",
    "à": r"\`a", "è": r"\`e", "ì": r"\`i", "ò": r"\`o", "ù": r"\`u",
    "ä": r'\"a', "ë": r'\"e', "ï": r'\"i', "ö": r'\"o', "ü": r'\"u',
    "Ä": r'\"A', "Ö": r'\"O', "Ü": r'\"U',
    "â": r"\^a", "ê": r"\^e", "î": r"\^i", "ô": r"\^o", "û": r"\^u",
    "ñ": r"\~n", "Ñ": r"\~N", "ã": r"\~a", "õ": r"\~o",
    "ç": r"\c{c}", "Ç": r"\c{C}", "ø": r"\o{}", "Ø": r"\O{}",
    "å": r"\aa{}", "Å": r"\AA{}", "ß": r"\ss{}",
    "ł": r"\l{}", "Ł": r"\L{}", "š": r"\v{s}", "ž": r"\v{z}", "č": r"\v{c}",
    "ő": r"\H{o}", "ű": r"\H{u}", "ı": r"\i{}",
    "–": "--", "—": "---", "’": "'", "‘": "`", "“": "``", "”": "''",
    "−": "-", " ": " ",
}


def escape(text: str) -> str:
    """Protect the characters BibTeX would eat, and the ones it cannot encode."""
    for a, b in (("&", r"\&"), ("%", r"\%"), ("_", r"\_"), ("#", r"\#")):
        text = text.replace(a, b)
    for a, b in ACCENTS.items():
        text = text.replace(a, b)
    # Anything still outside ASCII would break BibTeX silently; drop it loudly.
    return "".join(c if ord(c) < 128 else "?" for c in text)


def entry(key: str, item: dict) -> str:
    authors = " and ".join(
        escape(f"{a.get('family', '')}, {a.get('given', '')}".strip(", "))
        for a in item.get("author", [])
        if a.get("family")
    ) or "{" + escape(item.get("publisher") or "Anon") + "}"
    year = item.get("issued", {}).get("date-parts", [[""]])[0][0]
    title = escape((item.get("title") or [""])[0])
    journal = escape((item.get("container-title") or [""])[0])
    pages = item.get("page") or item.get("article-number") or ""
    kind = {"journal-article": "article", "proceedings-article": "inproceedings",
            "book": "book", "report": "techreport"}.get(item.get("type", ""), "misc")
    field = "journal" if kind == "article" else "booktitle" if kind == "inproceedings" else "howpublished"

    lines = [f"  author  = {{{authors}}}", f"  title   = {{{title}}}", f"  year    = {{{year}}}"]
    if journal:
        lines.append(f"  {field:7s} = {{{journal}}}")
    if item.get("volume"):
        lines.append(f"  volume  = {{{item['volume']}}}")
    if pages:
        lines.append(f"  pages   = {{{pages}}}")
    if item.get("publisher") and kind in ("book", "techreport", "misc"):
        lines.append(f"  publisher = {{{escape(item['publisher'])}}}")
    lines.append(f"  doi     = {{{item['DOI']}}}")
    return "@" + kind + "{" + key + ",\n" + ",\n".join(lines) + "\n}\n"


def main() -> int:
    print("% refs.bib - generated by tools/build_bib.py; do not edit the generated block.")
    print("% Every entry below is pinned to a DOI that was checked against the cited work.\n")
    for key, doi in PINNED.items():
        try:
            print(entry(key, fetch(doi)))
        except Exception as error:  # noqa: BLE001
            print(f"% FAILED to fetch {key} ({doi}): {error}", file=sys.stderr)
        time.sleep(0.3)
    print(MANUAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
