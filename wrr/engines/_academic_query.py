"""Strict identifier queries shared by academic retrieval and routing.

Only complete identifiers are recognized; titles never become identity evidence.
"""
from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class AcademicQuery:
    kind: Literal["doi", "references", "citations"]
    identifier: str
    identifier_type: Literal["doi", "openalex"]


def academic_query(query: str) -> AcademicQuery | None:
    """Parse DOI queries and ``references:`` / ``citations:`` one-hop queries.

    DOI URLs must use doi.org (or dx.doi.org), and OpenAlex URLs must use
    openalex.org. URL queries/fragments, arbitrary URLs and title searches are
    deliberately not interpreted as identifiers. Bare OpenAlex IDs are accepted
    only as relationship seeds, not as DOI lookups.
    """
    value = (query or "").strip()
    relation = re.match(r"^(references|citations)\s*:\s*", value, re.IGNORECASE)
    kind = relation.group(1).lower() if relation else "doi"
    if relation:
        value = value[relation.end():].strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if value.lower().startswith(("http://", "https://")):
        try:
            url = urlsplit(value)
        except ValueError:
            return None
        if url.netloc.lower() not in {"doi.org", "dx.doi.org", "openalex.org"}:
            return None
        if url.query or url.fragment:
            return None
        value = unquote(url.path[1:])
        if url.netloc.lower() == "openalex.org" and not re.fullmatch(r"W\d+", value, re.I):
            return None
        if url.netloc.lower() != "openalex.org" and not value.lower().startswith("10."):
            return None
    if kind != "doi" and re.fullmatch(r"W\d+", value, re.IGNORECASE):
        return AcademicQuery(kind, value.upper(), "openalex")
    if re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f\x7f]+", value):
        return AcademicQuery(kind, value.lower(), "doi")
    return None


_BIOMEDICAL = re.compile(
    r"\b(?:pubmed|europe\s*pmc|biomedical|clinical|medical|medicine|healthcare|"
    r"patient[s]?|cancer|oncology|tumou?r[s]?|"
    r"diabetes|disease[s]?|therapy|therapies|therapeutic|drug[s]?|vaccine[s]?|"
    r"genom(?:e|es|ic|ics)|gene[s]?|protein[s]?|crispr|immun\w*|"
    r"epidemiolog\w*|pharmacolog\w*|randomi[sz]ed\s+(?:controlled\s+)?trials?|biomarker[s]?|"
    r"neuro\w*|alzheimer[s]?|parkinson[s]?|rna|dna|cell[s]?|"
    r"infection[s]?|virus|viral|covid(?:-19)?)\b|"
    r"生物医学|生医|临床|随机(?:对照)?试验|患者|癌|肿瘤|糖尿病|疾病|治疗|药物|疫苗|基因|蛋白|免疫|流行病",
    re.IGNORECASE,
)


def biomedical_scope(query: str) -> bool:
    """Whether a scholarly query has scope covered by Europe PMC."""
    return bool(_BIOMEDICAL.search(query or ""))


_SCHOLARLY_INTENT = re.compile(
    r"\b(?:papers?|research|stud(?:y|ies)|reviews?|literature|trials?|"
    r"randomi[sz]ed|evidence|meta-analysis|systematic|mechanisms?)\b|"
    r"论文|研究|综述|文献|试验|实验|循证|证据|荟萃|机制|生医",
    re.IGNORECASE,
)
_BIOMEDICAL_RESEARCH_MARKERS = re.compile(
    r"\b(?:clinical|crispr|rna|dna|genom\w*|proteom\w*|biomarkers?|"
    r"epidemiolog\w*|pharmacolog\w*)\b|生物医学|基因|蛋白|流行病",
    re.IGNORECASE,
)
_PERSONAL_HEALTH = re.compile(
    r"\b(?:i|my|me|how|what|why|should|can|could|symptoms?)\b|"
    r"\bside effects?\b|怎么|如何|我|怎么办|副作用|症状",
    re.IGNORECASE,
)


def biomedical_query(query: str) -> bool:
    """Conservative academic routing; personal-health advice is not research.

    The academic engine uses ``biomedical_scope`` after an academic route has
    already been selected, so broad biomedical topic searches still gain PMC.
    """
    if not biomedical_scope(query):
        return False
    if re.search(r"\b(?:pubmed|europe\s*pmc)\b", query, re.IGNORECASE):
        return True
    if _SCHOLARLY_INTENT.search(query):
        return True
    return not _PERSONAL_HEALTH.search(query) and bool(_BIOMEDICAL_RESEARCH_MARKERS.search(query))
