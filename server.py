"""Medical Terminology MCP Server — keyword lookups and term definitions."""

import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Annotated

import httpx
from fastmcp import FastMCP
from pydantic import Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/medical.db")
UMLS_API_KEY = os.getenv("UMLS_API_KEY", "")
NLM_BASE = "https://clinicaltables.nlm.nih.gov"
UMLS_BASE = "https://uts-ws.nlm.nih.gov"
RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"
OPENFDA_BASE = "https://api.fda.gov/drug/label.json"

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "med_lookup",
    instructions=(
        "Medical terminology server with three tools. "
        "Use lookup_keyword to search one or more medical keywords — it searches "
        "abbreviations, conditions, ICD-10 codes, drug info, and UMLS concepts all at once. "
        "Use add_new_keyword to save a missing abbreviation or term definition to the database. "
        "Use remove_keyword to delete a custom-added entry from the database."
    ),
)

# Shared async HTTP client
http_client = httpx.AsyncClient(timeout=45.0)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create tables if they don't exist (idempotent)."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS abbreviations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            abbreviation TEXT NOT NULL,
            meaning TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'csv',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_abbr_upper ON abbreviations (abbreviation COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS custom_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL,
            definition TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'custom',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_term_upper ON custom_terms (term COLLATE NOCASE);
    """)
    conn.close()


# ---------------------------------------------------------------------------
# API query helpers
# ---------------------------------------------------------------------------
async def _query_abbreviations(term: str, conn: sqlite3.Connection) -> list:
    """Look up abbreviation meanings from the local database."""
    rows = conn.execute(
        "SELECT DISTINCT meaning FROM abbreviations WHERE abbreviation = ? COLLATE NOCASE",
        (term,),
    ).fetchall()
    return [r["meaning"] for r in rows]


async def _query_custom_terms(term: str, conn: sqlite3.Connection) -> list:
    """Look up custom term definitions from the local database."""
    rows = conn.execute(
        "SELECT definition FROM custom_terms WHERE term LIKE ? COLLATE NOCASE",
        (f"%{term}%",),
    ).fetchall()
    return [r["definition"] for r in rows]


def _term_matches(term: str, text: str | None) -> bool:
    """Check if the search term is relevant to the result text.

    For short terms (<=5 chars), require the term to appear as a whole word
    or at the start of a word in the text. This prevents 'stat' from matching
    'Diabetes' or 'stature' loosely via the NLM fuzzy search.
    For longer terms, a simple case-insensitive substring check suffices.
    """
    if not text:
        return False
    t = term.lower()
    txt = text.lower()
    if len(t) <= 5:
        # Require whole-word or word-start match
        return bool(re.search(r'\b' + re.escape(t) + r'\b', txt))
    return t in txt


async def _query_conditions(term: str) -> dict:
    """Query NLM Clinical Tables API for conditions and ICD-10 codes."""
    results: dict = {}
    try:
        resp = await http_client.get(
            f"{NLM_BASE}/api/conditions/v3/search",
            params={
                "terms": term,
                "maxList": 5,
                "df": "consumer_name,primary_name",
                "ef": "icd10cm_codes,info_link_data",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) == 4 and data[3]:
            extra = data[2] or {}
            conditions = []
            for i, display in enumerate(data[3]):
                consumer = display[0] if display else None
                primary = display[1] if len(display) > 1 else None
                # Filter out irrelevant fuzzy matches
                if not (_term_matches(term, consumer) or _term_matches(term, primary)):
                    continue
                entry = {"consumer_name": consumer, "primary_name": primary}
                icd_codes = extra.get("icd10cm_codes", [])
                if i < len(icd_codes):
                    entry["icd10cm_codes"] = icd_codes[i]
                conditions.append(entry)
            if conditions:
                results["conditions"] = conditions
    except Exception:
        pass

    try:
        resp = await http_client.get(
            f"{NLM_BASE}/api/icd10cm/v3/search",
            params={"sf": "code,name", "terms": term, "maxList": 5},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) == 4 and data[3]:
            codes = []
            for display in data[3]:
                name = display[1] if len(display) > 1 else None
                if not _term_matches(term, name) and not _term_matches(term, display[0]):
                    continue
                codes.append({"code": display[0], "name": name})
            if codes:
                results["icd10_codes"] = codes
    except Exception:
        pass

    return results


async def _query_drugs(term: str) -> dict:
    """Query RxNorm + OpenFDA for comprehensive drug information."""
    results: dict = {}
    rxcui = None

    # --- RxNorm: name resolution + formulations ---
    try:
        resp = await http_client.get(
            f"{RXNORM_BASE}/drugs.json",
            params={"name": term},
        )
        resp.raise_for_status()
        data = resp.json()
        groups = data.get("drugGroup", {}).get("conceptGroup", [])
        formulations = []
        for group in groups:
            for concept in group.get("conceptProperties", []):
                formulations.append(concept["name"])
                if not rxcui:
                    rxcui = concept.get("rxcui")
        if formulations:
            results["formulations"] = formulations[:10]
    except Exception:
        pass

    # --- RxNorm approximate search (fuzzy fallback) ---
    if not results.get("formulations"):
        try:
            resp = await http_client.get(
                f"{RXNORM_BASE}/approximateTerm.json",
                params={"term": term, "maxEntries": 1},
            )
            resp.raise_for_status()
            candidates = resp.json().get("approximateGroup", {}).get("candidate", [])
            if candidates:
                rxcui = candidates[0].get("rxcui")
                matched_name = candidates[0].get("name", term)
                if matched_name.lower() != term.lower():
                    results["matched_name"] = matched_name
                rel_resp = await http_client.get(
                    f"{RXNORM_BASE}/rxcui/{rxcui}/related.json",
                    params={"tty": "SCD+SBD"},
                )
                rel_resp.raise_for_status()
                rel_groups = rel_resp.json().get("relatedGroup", {}).get("conceptGroup", [])
                formulations = []
                for group in rel_groups:
                    for concept in group.get("conceptProperties", []):
                        formulations.append(concept["name"])
                if formulations:
                    results["formulations"] = formulations[:10]
        except Exception:
            pass

    # --- RxClass: drug classification ---
    try:
        resp = await http_client.get(
            f"{RXNORM_BASE}/rxclass/class/byDrugName.json",
            params={"drugName": term, "relaSource": "ATC"},
        )
        resp.raise_for_status()
        data = resp.json()
        classes = data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", [])
        if classes:
            results["drug_classes"] = list({c["rxclassMinConceptItem"]["className"] for c in classes})
    except Exception:
        pass

    # --- OpenFDA: full prescribing information ---
    try:
        resp = await http_client.get(
            OPENFDA_BASE,
            params={
                "search": f'openfda.generic_name:"{term}"+openfda.brand_name:"{term}"',
                "limit": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        fda_results = data.get("results", [])
        if fda_results:
            label = fda_results[0]
            # Verify the result actually matches the search term closely.
            # OpenFDA brand_name can be comma-lists like "Scrub, Scrub-Stat".
            # Require the search term to match the generic name OR be essentially
            # the full brand name (not just a substring of a multi-product list).
            openfda_block = label.get("openfda", {})
            t_lower = term.lower()
            generic_match = any(
                t_lower in n.lower() for n in openfda_block.get("generic_name", [])
            )
            brand_match = any(
                t_lower == n.lower().strip()
                for raw in openfda_block.get("brand_name", [])
                for n in raw.split(",")
            )
            if not generic_match and not brand_match:
                raise ValueError("FDA result does not match search term")
            fda_info: dict = {}

            # Extract key clinical fields (first element of each array)
            field_map = {
                "indications_and_usage": "indications",
                "mechanism_of_action": "mechanism_of_action",
                "dosage_and_administration": "dosage",
                "warnings_and_cautions": "warnings",
                "boxed_warning": "boxed_warning",
                "contraindications": "contraindications",
                "adverse_reactions": "adverse_reactions",
                "drug_interactions": "drug_interactions",
            }
            for fda_field, key in field_map.items():
                val = label.get(fda_field)
                if val and isinstance(val, list) and val[0]:
                    # Truncate very long text to keep response manageable
                    text = val[0][:2000]
                    if len(val[0]) > 2000:
                        text += "..."
                    fda_info[key] = text

            # Structured metadata from openfda block
            openfda = label.get("openfda", {})
            if openfda.get("brand_name"):
                fda_info["brand_names"] = openfda["brand_name"]
            if openfda.get("generic_name"):
                fda_info["generic_names"] = openfda["generic_name"]
            if openfda.get("route"):
                fda_info["routes"] = openfda["route"]
            if openfda.get("pharm_class_epc"):
                fda_info["pharmacologic_class"] = openfda["pharm_class_epc"]
            if openfda.get("manufacturer_name"):
                fda_info["manufacturer"] = openfda["manufacturer_name"]

            if fda_info:
                results["fda_label"] = fda_info
    except Exception:
        pass

    return results


async def _query_umls(term: str) -> dict:
    """Query UMLS REST API for concept definitions."""
    if not UMLS_API_KEY:
        return {}

    results: dict = {}
    try:
        resp = await http_client.get(
            f"{UMLS_BASE}/rest/search/current",
            params={
                "string": term,
                "apiKey": UMLS_API_KEY,
                "pageSize": 5,
                "searchType": "words",
            },
        )
        resp.raise_for_status()
        search_data = resp.json()
        concepts = search_data.get("result", {}).get("results", [])

        concept_list = []
        for concept in concepts[:3]:
            cui = concept.get("ui", "")
            entry = {"cui": cui, "name": concept.get("name", "")}

            try:
                def_resp = await http_client.get(
                    f"{UMLS_BASE}/rest/content/current/CUI/{cui}/definitions",
                    params={"apiKey": UMLS_API_KEY},
                )
                def_resp.raise_for_status()
                def_data = def_resp.json()
                defs = def_data.get("result", [])
                if defs:
                    entry["definitions"] = [d.get("value", "") for d in defs[:2]]
            except Exception:
                pass

            concept_list.append(entry)

        if concept_list:
            results["concepts"] = concept_list
    except Exception:
        pass

    return results


async def _query_medlineplus(term: str) -> dict:
    """Query MedlinePlus Health Topics for consumer-friendly definitions.

    Single call per keyword — returns matching health topic summaries.
    """
    results: dict = {}
    try:
        resp = await http_client.get(
            "https://wsearch.nlm.nih.gov/ws/query",
            params={"db": "healthTopics", "term": term, "rettype": "topic", "retmax": 3},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        topics = []
        for doc in root.findall(".//document"):
            ht = doc.find(".//health-topic")
            if ht is None:
                continue
            title = ht.get("title", "")
            if not _term_matches(term, title):
                continue

            summary_el = ht.find("full-summary")
            summary = ""
            if summary_el is not None:
                # full-summary text contains raw HTML as text content
                raw = summary_el.text or ""
                summary = re.sub(r"<[^>]+>", "", raw).strip()
                # Collapse multiple whitespace/newlines
                summary = re.sub(r"\s+", " ", summary)
                if len(summary) > 2000:
                    summary = summary[:2000] + "..."

            also_called = [ac.text for ac in ht.findall("also-called") if ac.text]

            entry: dict = {"title": title}
            if summary:
                entry["summary"] = summary
            if also_called:
                entry["also_called"] = also_called
            topics.append(entry)

        if topics:
            results["topics"] = topics
    except Exception:
        pass

    return results


# ---------------------------------------------------------------------------
# Lookup a single keyword across all sources
# ---------------------------------------------------------------------------
async def _lookup_single(keyword: str) -> dict:
    """Search all sources for a single keyword and return combined results."""
    result: dict = {"keyword": keyword}
    conn = _get_db()

    # Local DB: abbreviations
    abbr = await _query_abbreviations(keyword, conn)
    if abbr:
        result["abbreviations"] = abbr

    # Local DB: custom terms
    custom = await _query_custom_terms(keyword, conn)
    if custom:
        result["custom_definitions"] = custom

    conn.close()

    # NLM: conditions + ICD-10
    conditions = await _query_conditions(keyword)
    if conditions:
        result["conditions"] = conditions

    # MedlinePlus: health topic definitions (single call per keyword)
    medline = await _query_medlineplus(keyword)
    if medline:
        result["definitions"] = medline

    # RxNorm + OpenFDA: drugs
    drugs = await _query_drugs(keyword)
    if drugs:
        result["drugs"] = drugs

    # UMLS
    umls = await _query_umls(keyword)
    if umls:
        result["umls"] = umls

    # Check if anything was found beyond the keyword itself
    has_results = any(k != "keyword" for k in result)
    if not has_results:
        result["message"] = f"No data found for '{keyword}'."

    return result


# ---------------------------------------------------------------------------
# Tool 1: lookup_keyword
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def lookup_keyword(
    keywords: Annotated[
        list[str],
        Field(description=(
            "One or more medical keywords to look up. "
            "Accepts a list like [\"ABG\", \"diabetes\"] or a single item [\"Atorvastatin\"]. "
            "Each keyword is searched across all sources: abbreviation database (4600+ acronyms), "
            "NLM conditions & ICD-10 codes, drug information (RxNorm + OpenFDA prescribing data), "
            "and UMLS medical concepts."
        )),
    ],
) -> str:
    """Look up one or more medical keywords across all available sources.

    For each keyword, searches:
    - Local database of 4600+ medical abbreviations and acronyms
    - Custom user-added definitions
    - NLM conditions database and ICD-10 diagnosis codes
    - MedlinePlus health topic definitions (plain-language descriptions of conditions and diseases)
    - Drug information: RxNorm (formulations, classifications) and OpenFDA (indications,
      dosage, warnings, contraindications, adverse reactions, drug interactions, mechanism of action)
    - UMLS medical concepts and definitions (when API key is configured)

    Returns a list of result objects, one per keyword, each containing all data found.
    If nothing is found for a keyword, its result will contain: "No data found for '{keyword}'."
    """
    results = []
    for kw in keywords:
        kw = kw.strip()
        if kw:
            results.append(await _lookup_single(kw))
    return json.dumps(results)


# ---------------------------------------------------------------------------
# Tool 2: add_new_keyword
# ---------------------------------------------------------------------------
class EntryType(str, Enum):
    abbreviation = "abbreviation"
    term = "term"


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def add_new_keyword(
    entry_type: Annotated[
        EntryType,
        Field(description=(
            "Type of keyword to add. Use 'abbreviation' for medical acronyms and shorthand "
            "(e.g. ROSC, NPO, TID). Use 'term' for medical terms, conditions, or definitions "
            "(e.g. Troponin I, Metabolic Acidosis)."
        )),
    ],
    keyword: Annotated[
        str,
        Field(description="The abbreviation or term to add (e.g. 'ROSC' or 'Troponin I')."),
    ],
    definition: Annotated[
        str,
        Field(description="The meaning or definition (e.g. 'Return of Spontaneous Circulation')."),
    ],
) -> str:
    """Save a new abbreviation or term definition to the local database.

    Use this when lookup_keyword returns no results and you know the correct definition.
    The new entry will appear in future lookup_keyword searches.
    Duplicate entries (same keyword + same definition) are rejected.
    """
    keyword = keyword.strip()
    definition = definition.strip()
    if not keyword or not definition:
        return json.dumps({"success": False, "message": "Both keyword and definition must be non-empty."})

    conn = _get_db()

    et = entry_type.value if isinstance(entry_type, EntryType) else entry_type
    if et == "abbreviation":
        existing = conn.execute(
            "SELECT id FROM abbreviations WHERE abbreviation = ? COLLATE NOCASE AND meaning = ? COLLATE NOCASE",
            (keyword, definition),
        ).fetchone()
        if existing:
            conn.close()
            return json.dumps({"success": False, "message": f"Entry already exists: {keyword} → {definition}"})

        conn.execute(
            "INSERT INTO abbreviations (abbreviation, meaning, source) VALUES (?, ?, 'custom')",
            (keyword, definition),
        )
    else:
        existing = conn.execute(
            "SELECT id FROM custom_terms WHERE term = ? COLLATE NOCASE AND definition = ? COLLATE NOCASE",
            (keyword, definition),
        ).fetchone()
        if existing:
            conn.close()
            return json.dumps({"success": False, "message": f"Entry already exists: {keyword} → {definition}"})

        conn.execute(
            "INSERT INTO custom_terms (term, definition, source) VALUES (?, ?, 'custom')",
            (keyword, definition),
        )

    conn.commit()
    conn.close()
    return json.dumps({
        "success": True,
        "entry_type": et,
        "keyword": keyword,
        "definition": definition,
        "message": f"Added {et}: {keyword} → {definition}",
    })


# ---------------------------------------------------------------------------
# Tool 3: remove_keyword
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def remove_keyword(
    entry_type: Annotated[
        EntryType,
        Field(description=(
            "Type of keyword to remove. Use 'abbreviation' for medical acronyms and shorthand "
            "(e.g. ROSC, NPO, TID). Use 'term' for medical terms, conditions, or definitions "
            "(e.g. Troponin I, Metabolic Acidosis)."
        )),
    ],
    keyword: Annotated[
        str,
        Field(description="The abbreviation or term to remove (e.g. 'ROSC' or 'Troponin I')."),
    ],
    definition: Annotated[
        str,
        Field(description="The definition to remove (must match exactly what was added)."),
    ],
) -> str:
    """Remove a custom-added abbreviation or term definition from the local database.

    Only removes entries previously added via add_new_keyword (source='custom').
    Built-in entries from the seeded abbreviation database cannot be removed.
    Requires an exact match on keyword + definition.
    """
    keyword = keyword.strip()
    definition = definition.strip()
    if not keyword or not definition:
        return json.dumps({"success": False, "message": "Both keyword and definition must be non-empty."})

    conn = _get_db()

    et = entry_type.value if isinstance(entry_type, EntryType) else entry_type
    if et == "abbreviation":
        row = conn.execute(
            "SELECT id, source FROM abbreviations WHERE abbreviation = ? COLLATE NOCASE AND meaning = ? COLLATE NOCASE",
            (keyword, definition),
        ).fetchone()
        if not row:
            conn.close()
            return json.dumps({"success": False, "message": f"Entry not found: {keyword} → {definition}"})
        if row["source"] != "custom":
            conn.close()
            return json.dumps({"success": False, "message": "Cannot remove built-in entries. Only custom-added entries can be removed."})
        conn.execute("DELETE FROM abbreviations WHERE id = ?", (row["id"],))
    else:
        row = conn.execute(
            "SELECT id, source FROM custom_terms WHERE term = ? COLLATE NOCASE AND definition = ? COLLATE NOCASE",
            (keyword, definition),
        ).fetchone()
        if not row:
            conn.close()
            return json.dumps({"success": False, "message": f"Entry not found: {keyword} → {definition}"})
        if row["source"] != "custom":
            conn.close()
            return json.dumps({"success": False, "message": "Cannot remove built-in entries. Only custom-added entries can be removed."})
        conn.execute("DELETE FROM custom_terms WHERE id = ?", (row["id"],))

    conn.commit()
    conn.close()
    return json.dumps({
        "success": True,
        "entry_type": et,
        "keyword": keyword,
        "definition": definition,
        "message": f"Removed {et}: {keyword} → {definition}",
    })


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
_init_db()

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8010")),
        stateless_http=True,
    )
