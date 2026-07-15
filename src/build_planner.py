#!/usr/bin/env python3
"""Build ../planner.html from ../data/siggraph2026_schedule_with_descriptions.csv.

Re-run this script whenever the CSV changes:
    python3 src/build_planner.py
"""
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE.parent / "data" / "siggraph2026_schedule_with_descriptions.csv"
TEMPLATE_PATH = HERE / "planner_template.html"
OUT_PATH = HERE.parent / "planner.html"

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", re.I)


def to_minutes(t: str):
    m = TIME_RE.match(t.strip())
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if h == 12:
        h = 0
    if ap == "pm":
        h += 12
    return h * 60 + mins


def to_iso_date(day_and_date: str):
    try:
        return datetime.strptime(day_and_date.strip(), "%A, %d %B %Y").date().isoformat()
    except ValueError:
        return ""


def make_id(row) -> str:
    key = f"{row['day_and_date']}|{row['start_time']}|{row['title']}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


# Must-attend sessions, matched against exact titles (or unambiguous prefixes).
SEED_MATCHERS = [
    # Optional: pre-check sessions on a user's first load, e.g.:
    # ("NVIDIA Keynote", lambda r: r["title"].startswith("Sponsored Keynote by NVIDIA")),
]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-seeds", action="store_true", help="build with no pre-checked sessions (for public sharing)")
    ap.add_argument("--out", type=Path, default=OUT_PATH, help="output HTML path")
    args = ap.parse_args()

    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    sessions = []
    null_url = []  # per-row: True if URL was the site's literal ".../null" (session-group headers)
    ids_seen = {}
    for row in rows:
        sid = make_id(row)
        # Guard against hash collisions / duplicate rows
        if sid in ids_seen:
            ids_seen[sid] += 1
            sid = f"{sid}-{ids_seen[sid]}"
        else:
            ids_seen[sid] = 0
        sm = to_minutes(row["start_time"])
        em = to_minutes(row["end_time"])
        url = row["session_url"].strip()
        null_url.append(url.endswith("/null"))
        if null_url[-1]:
            url = ""  # dead link on the official site — don't render it
        sessions.append({
            "id": sid,
            "day": row["day_and_date"].strip(),
            "date": to_iso_date(row["day_and_date"]),
            "sm": sm,
            "em": em,
            "st": row["start_time"].strip(),
            "et": row["end_time"].strip(),
            "title": row["title"].strip(),
            "track": row["program_track"].strip(),
            "loc": row["location"].strip(),
            "sp": row["speakers_or_contributors"].strip(),
            "kw": row["keywords_tags"].strip(),
            "rc": row["registration_category"].strip(),
            "url": url,
            "desc": row["description"].strip(),
        })

    # Parent/child grouping: the site's session-group headers (Talks, Technical
    # Papers, Educator's Forum, Art Papers) have dead "/null" URLs; their
    # sub-presentations are separate rows nested inside the parent's time slot
    # in the same room. Only null-URL rows may act as parents.
    from collections import defaultdict
    byloc = defaultdict(list)
    for i, s in enumerate(sessions):
        if s["loc"] and s["sm"] is not None and s["em"] is not None:
            byloc[(s["day"], s["loc"])].append(i)
    n_groups = 0
    for key, idxs in byloc.items():
        parents_here = [i for i in idxs if null_url[i]]
        if not parents_here:
            continue
        kids_of = defaultdict(list)
        for c in idxs:
            if null_url[c]:
                continue
            cs, ce = sessions[c]["sm"], sessions[c]["em"]
            cands = [p for p in parents_here
                     if sessions[p]["sm"] <= cs and ce <= sessions[p]["em"]
                     and (sessions[p]["em"] - sessions[p]["sm"]) > (ce - cs)]
            if cands:
                cands.sort(key=lambda p: sessions[p]["em"] - sessions[p]["sm"])
                kids_of[cands[0]].append(c)
        for p, kids in kids_of.items():
            kids.sort(key=lambda c: (sessions[c]["sm"], sessions[c]["title"]))
            sessions[p]["kids"] = [sessions[c]["id"] for c in kids]
            for c in kids:
                sessions[c]["pid"] = sessions[p]["id"]
            n_groups += 1
    n_kids = sum(len(s.get("kids", [])) for s in sessions)
    print(f"grouped {n_kids} presentations under {n_groups} session headers")

    # Resolve seeds
    seed_ids = []
    if not args.no_seeds:
        for label, match in SEED_MATCHERS:
            hits = [s for s, r in zip(sessions, rows) if match(r)]
            if len(hits) != 1:
                raise SystemExit(f"Seed '{label}' matched {len(hits)} rows — expected exactly 1:\n"
                                 + "\n".join("  " + h["title"] for h in hits))
            seed_ids.append(hits[0]["id"])
            print(f"  seed ok: {label} -> {hits[0]['title'][:70]}")

    # Stats
    timed = [s for s in sessions if s["sm"] is not None and s["em"] is not None]
    print(f"{len(sessions)} sessions, {len(timed)} with valid times")
    print(f"earliest start {min(s['sm'] for s in timed)//60}:00h, latest end {max(s['em'] for s in timed)/60:.1f}h")

    def embed(obj):
        # '<\/' is a legal JSON escape; prevents '</script>' from closing the tag
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in (("__DATA_JSON__", embed(sessions)), ("__SEED_JSON__", embed(seed_ids))):
        assert token in html, f"missing token {token}"
        html = html.replace(token, value)
    args.out.write_text(html, encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
