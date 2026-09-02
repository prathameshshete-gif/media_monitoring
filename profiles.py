"""Entity profiles: who is monitored, what is searched for, how the report reads.

A profile bundles the three things a run needs — the subject line the reranker
scores against, the search terms discovery matches on, and the system prompt
that writes the report for that entity.

Every profile carries its own brief, in full. There is no fallback: what you
see in the settings dialog is exactly what gets sent.

Storage. A profile list is operational configuration — who a client watches and
the editorial lens applied to them — so it does not belong in the repository.
With `MONGODB_URI` set, profiles live in MongoDB Atlas. Without it they fall
back to a local JSON file, which is what a laptop with no cluster gets. Either
way the collection is seeded from `profiles.example.json` when it is empty, so
a fresh checkout has something to look at.

Search-term rules, learned the hard way (see README):
  - Every name needs its Devanagari form. Marathi matching is substring-based,
    so spelling variants each need their own entry — a name transliterated with
    ळ in one outlet and ल in another will not match itself.
  - A surname alone is only safe when it is distinctive. Common ones such as
    पाटील, चव्हाण and भोसले pull in dozens of unrelated people; a rare surname
    does not. Low-profile figures need the loose surname to be found at all, so
    the distinction is per name, not a blanket rule.
  - Check a distinctive-looking surname against place names before trusting it.
    Several Marathi surnames are also talukas, and those need full-name-only
    matching or the run fills with datelines.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

# Set to an Atlas SRV string to store profiles there. Empty means file storage.
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "media_monitoring")
PROFILES_COLLECTION = os.getenv("MONGODB_PROFILES_COLLECTION", "profiles")
SETTINGS_COLLECTION = os.getenv("MONGODB_SETTINGS_COLLECTION", "settings")

# The file backend. In a container without Atlas this points into the data
# volume so UI edits survive a redeploy.
STORE = Path(os.getenv("PROFILES_PATH", "profiles.json"))

# Shipped in the repo, deliberately generic: two placeholder entities that show
# the shape of a profile without naming anyone real.
EXAMPLE = Path(os.getenv("PROFILES_EXAMPLE", "profiles.example.json"))

_client = None


def using_mongo() -> bool:
    return bool(MONGODB_URI)


def _db():
    """One client per process, opened on first use.

    MongoClient is lazy about connecting, so building it costs nothing until a
    query runs; that keeps import time flat for callers that never touch
    profiles, such as the CLI pipeline.
    """
    global _client
    if _client is None:
        from pymongo import MongoClient
        _client = MongoClient(MONGODB_URI, appname="media-monitor",
                              serverSelectionTimeoutMS=10000,
                              connectTimeoutMS=10000)
    return _client[MONGODB_DB]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def default_prompt() -> str:
    """The brief a new profile starts from.

    An operator's real brief is their own editorial IP, so it is stored
    alongside the profiles rather than in the source. `report_llm.SYSTEM_PROMPT`
    is the generic template that ships with the repo, used until someone saves
    one of their own.
    """
    if using_mongo():
        doc = _db()[SETTINGS_COLLECTION].find_one({"_id": "default_prompt"})
        if doc and (doc.get("text") or "").strip():
            return doc["text"]
    from report_llm import SYSTEM_PROMPT
    return SYSTEM_PROMPT


def set_default_prompt(text: str) -> None:
    """Store the house brief, so new profiles start from it rather than the
    generic template. Mongo only: with file storage there is nowhere to put it
    that is not the repository."""
    if not using_mongo():
        raise RuntimeError("Set MONGODB_URI to store a house brief")
    _db()[SETTINGS_COLLECTION].replace_one(
        {"_id": "default_prompt"},
        {"_id": "default_prompt", "text": text,
         "updated": datetime.now().isoformat(timespec="seconds")},
        upsert=True)


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def _example_profiles() -> list[dict]:
    """The placeholder set, with every brief filled in."""
    if not EXAMPLE.exists():
        return []
    try:
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    brief = None
    for p in data:
        if not (p.get("system_prompt") or "").strip():
            if brief is None:
                from report_llm import SYSTEM_PROMPT
                brief = SYSTEM_PROMPT
            p["system_prompt"] = brief
    return data


# --------------------------------------------------------------------------
# Read and write
# --------------------------------------------------------------------------

def _normalise(doc: dict) -> dict:
    """Mongo's _id is the profile id; the API has always called it `id`."""
    out = dict(doc)
    out["id"] = out.pop("_id", out.get("id"))
    out.pop("created", None)
    return out


def load() -> list[dict]:
    if using_mongo():
        coll = _db()[PROFILES_COLLECTION]
        if coll.count_documents({}, limit=1) == 0:
            seeded = _example_profiles()
            if seeded:
                save(seeded)
                return seeded
            return []
        # `created` keeps the list in the order profiles were added, which is
        # the order the file backend gave for free.
        return [_normalise(d) for d in coll.find().sort([("created", 1),
                                                         ("name", 1)])]

    if not STORE.exists():
        seeded = _example_profiles()
        if seeded:
            save(seeded)
        return seeded
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _example_profiles()
    if not isinstance(data, list):
        return _example_profiles()

    # Profiles written before briefs were stored per entity carry an empty one.
    # Fill those in once so every profile has a brief you can read and edit,
    # rather than an invisible default applied at send time.
    missing = [p for p in data if not (p.get("system_prompt") or "").strip()]
    if missing:
        brief = default_prompt()
        for p in missing:
            p["system_prompt"] = brief
        save(data)
    return data


def save(profiles: list[dict]) -> None:
    """Replace the whole set. Used for seeding and migration; ordinary edits go
    through upsert/delete so two editors cannot clobber each other's work."""
    if using_mongo():
        coll = _db()[PROFILES_COLLECTION]
        now = datetime.now().isoformat(timespec="seconds")
        for i, p in enumerate(profiles):
            doc = dict(p)
            pid = doc.pop("id", None) or _slug(doc.get("name", ""))
            doc["_id"] = pid
            # Preserve list order across the move into Mongo.
            doc.setdefault("created", f"{now}#{i:04d}")
            coll.replace_one({"_id": pid}, doc, upsert=True)
        return
    STORE.write_text(json.dumps(profiles, indent=1, ensure_ascii=False),
                     encoding="utf-8")


def get(profile_id: str) -> dict | None:
    if using_mongo():
        doc = _db()[PROFILES_COLLECTION].find_one({"_id": profile_id})
        return _normalise(doc) if doc else None
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

    if using_mongo():
        coll = _db()[PROFILES_COLLECTION]
        doc = dict(record)
        doc["_id"] = doc.pop("id")
        # A new profile sorts to the end of the list; an existing one keeps the
        # position it already had.
        existing = coll.find_one({"_id": pid}, {"created": 1})
        doc["created"] = (existing or {}).get("created") or record["updated"]
        coll.replace_one({"_id": pid}, doc, upsert=True)
        return record

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
    if using_mongo():
        return _db()[PROFILES_COLLECTION].delete_one(
            {"_id": profile_id}).deleted_count > 0
    profiles = load()
    kept = [p for p in profiles if p["id"] != profile_id]
    if len(kept) == len(profiles):
        return False
    save(kept)
    return True
