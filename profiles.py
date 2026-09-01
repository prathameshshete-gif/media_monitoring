"""Entity profiles: who we monitor, what we search for, how the report reads.

A profile bundles the three things a run needs — the subject line the reranker
scores against, the search terms discovery matches on, and the system prompt
that writes the report for that entity.

Every profile carries its own brief, in full. There is no fallback: what you
see in the settings dialog is exactly what gets sent. New profiles start from
the stock Marathi brief and are edited from there.

Stored as one JSON file so a profile can be edited by hand or checked into
version control. The seed set is written on first load and never re-applied, so
edits and deletions stick.

Search-term rules, learned the hard way (see README):
  - Every name needs its Devanagari form. Marathi matching is substring-based,
    so spelling variants (बावनकुळे / बावनकुले) each need their own entry.
  - A surname alone is only safe when it is distinctive. `पाटील`, `चव्हाण` and
    `भोसले` pull in dozens of unrelated people; `बावनकुळे` or `अडसड` do not.
    Low-profile figures need the loose surname to be found at all, so the
    distinction is per name, not a blanket rule.
  - `मोहोळ` looks distinctive but is also a Solapur taluka, so Murlidhar Mohol
    is full-name only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

STORE = Path("profiles.json")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def default_prompt() -> str:
    """The stock Marathi brief every new profile starts from.

    Imported lazily: report_llm pulls in LangChain, and nothing here needs it
    until a profile is actually being seeded or reset.
    """
    from report_llm import SYSTEM_PROMPT
    return SYSTEM_PROMPT


def _p(name: str, subject: str, terms: list[str]) -> dict:
    return {"id": _slug(name), "name": name, "subject": subject,
            "terms": terms, "system_prompt": ""}


# Seeded on first load only. Editing or deleting these in the UI is permanent.
SEED = [
    _p("Devendra Fadnavis",
       "Devendra Fadnavis, Chief Minister of Maharashtra",
       ["Devendra Fadnavis", "Fadnavis", "देवेंद्र फडणवीस", "फडणवीस"]),
    _p("Ravindra Chavan",
       "Ravindra Chavan, Maharashtra BJP state president",
       ["Ravindra Chavan", "Chavan Ravindra", "रवींद्र चव्हाण", "रविंद्र चव्हाण"]),
    _p("Chandrashekhar Bawankule",
       "Chandrashekhar Bawankule, Revenue Minister of Maharashtra",
       ["Chandrashekhar Bawankule", "Chandrasekhar Bawankule", "Bawankule",
        "चंद्रशेखर बावनकुळे", "बावनकुळे", "बावनकुले"]),
    _p("Murlidhar Mohol",
       "Murlidhar Mohol, Union Minister of State and MP from Pune",
       ["Murlidhar Mohol", "Muralidhar Mohol", "मुरलीधर मोहोळ",
        "मुरलीधर मोहोल"]),
    _p("Atul Bhosale",
       "Atul Bhosale, BJP MLA from Karad South",
       ["Atul Bhosale", "Atul Bhosle", "अतुल भोसले"]),
    _p("Siddharth Shirole",
       "Siddharth Shirole, BJP MLA from Shivajinagar, Pune",
       ["Siddharth Shirole", "Siddharth Sirole", "Shirole",
        "सिद्धार्थ शिरोळे", "शिरोळे", "शिरोले"]),
    _p("Hemant Rasane",
       "Hemant Rasane, BJP MLA from Kasba Peth, Pune",
       ["Hemant Rasane", "Rasane", "हेमंत रासने", "रासने"]),
    _p("Ranajagjitsinha Patil",
       "Ranajagjitsinha Patil, BJP MLA from Dharashiv",
       ["Ranajagjitsinha Patil", "Rana Jagjitsinha Patil", "Ranajagjitsinha",
        "राणाजगजितसिंह पाटील", "राणा जगजितसिंह पाटील", "राणाजगजितसिंह",
        "राणाजगजीतसिंह"]),
    _p("Pratap Adsad",
       "Pratap Adsad, BJP MLA from Dhamangaon Railway",
       ["Pratap Adsad", "Adsad", "प्रताप अडसड", "अडसड"]),
    _p("Kunal Patil",
       "Kunal Patil, MLA from Dhule Rural",
       ["Kunal Patil", "कुणाल पाटील", "कुनाल पाटील"]),
    _p("Rahul Kalate",
       "Rahul Kalate, politician from Chinchwad, Pimpri-Chinchwad",
       ["Rahul Kalate", "Kalate", "राहुल कलाटे", "कलाटे"]),
    _p("Pankaj Bhoyar",
       "Pankaj Bhoyar, BJP MLA from Wardha and Minister of State",
       ["Pankaj Bhoyar", "Bhoyar", "पंकज भोयर", "भोयर"]),
]


def _seeded() -> list[dict]:
    """SEED with every brief filled in, so no profile ships without one."""
    brief = default_prompt()
    return [dict(p, system_prompt=brief) for p in SEED]


def load() -> list[dict]:
    if not STORE.exists():
        seeded = _seeded()
        save(seeded)
        return seeded
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _seeded()
    if not isinstance(data, list):
        return _seeded()

    # Profiles written before briefs were stored per entity carry an empty
    # one. Fill those in once so every profile has a brief you can read and
    # edit, rather than an invisible default applied at send time.
    missing = [p for p in data if not (p.get("system_prompt") or "").strip()]
    if missing:
        brief = default_prompt()
        for p in missing:
            p["system_prompt"] = brief
        save(data)
    return data


def save(profiles: list[dict]) -> None:
    STORE.write_text(json.dumps(profiles, indent=1, ensure_ascii=False),
                     encoding="utf-8")


def get(profile_id: str) -> dict | None:
    return next((p for p in load() if p["id"] == profile_id), None)


def upsert(profile: dict) -> dict:
    """Create or update by id, falling back to a slug of the name."""
    name = (profile.get("name") or "").strip()
    if not name:
        raise ValueError("A profile needs a name")
    terms = [t.strip() for t in profile.get("terms", []) if t.strip()]
    if not terms:
        raise ValueError("A profile needs at least one search term")
    brief = (profile.get("system_prompt") or "").strip()
    if not brief:
        raise ValueError("A profile needs a report brief — use Reset to default")

    pid = (profile.get("id") or "").strip() or _slug(name)
    record = {
        "id": pid,
        "name": name,
        "subject": (profile.get("subject") or name).strip(),
        "terms": terms,
        "system_prompt": brief,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    profiles = load()
    for i, p in enumerate(profiles):
        if p["id"] == pid:
            profiles[i] = record
            break
    else:
        profiles.append(record)
    save(profiles)
    return record


def delete(profile_id: str) -> bool:
    profiles = load()
    kept = [p for p in profiles if p["id"] != profile_id]
    if len(kept) == len(profiles):
        return False
    save(kept)
    return True
