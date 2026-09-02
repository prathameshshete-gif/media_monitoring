"""Load a profile set into the configured store.

    python3 import_profiles.py --file profiles.json
    python3 import_profiles.py --file profiles.json --default-brief brief.md

Used once to move an existing `profiles.json` into MongoDB, and afterwards to
restore a backup or seed a second environment. Writes through `profiles`, so
it targets Atlas when MONGODB_URI is set and the JSON file otherwise.

Existing profiles with the same id are replaced. Nothing is deleted: a profile
in the store but absent from the file is left alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import profiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True,
                    help="JSON file holding a list of profiles")
    ap.add_argument("--default-brief",
                    help="text file holding the house brief new profiles start "
                         "from; stored in the settings collection (Mongo only)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{args.file} is not a list of profiles")

    where = "MongoDB" if profiles.using_mongo() else str(profiles.STORE)
    print(f"{len(data)} profiles from {args.file} -> {where}")
    for p in data:
        brief = len(p.get("system_prompt") or "")
        print(f"  {p.get('id') or '(no id)':<28} {p.get('name','?'):<28} "
              f"{len(p.get('terms', []))} terms, {brief} char brief")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    profiles.save(data)
    print(f"\nwrote {len(data)} profiles")

    if args.default_brief:
        text = Path(args.default_brief).read_text(encoding="utf-8")
        profiles.set_default_prompt(text)
        print(f"stored house brief ({len(text)} chars)")

    back = profiles.load()
    print(f"store now holds {len(back)}: {[p['name'] for p in back[:3]]}...")


if __name__ == "__main__":
    main()
