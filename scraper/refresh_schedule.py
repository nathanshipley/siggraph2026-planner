#!/usr/bin/env python3
"""
refresh_schedule.py — check/refresh the SIGGRAPH 2026 schedule CSV cheaply.

WHY THIS EXISTS
---------------
fetch_descriptions.py hits ~878 dynamic session pages, which is what got us
rate-banned. It turns out the site publishes its entire program as five STATIC
text files (one per day) that the Full Program page lazy-loads:

    /wp-content/linklings_snippets/wp_program_view_all_2026-07-19.txt?v=<ts>

Those five files contain every session: times, title, track, room, speakers,
keywords, interest areas and registration categories. One more request to
/contributors/ yields every person with their institution and the sessions
they appear in.

So a COMPLETE freshness check costs 6 requests instead of ~900, and they are
static files rather than dynamic WordPress pages. Only genuinely new sessions
need their description page fetched.

USAGE
-----
    python3 scraper/refresh_schedule.py --check     # 6 requests, report only
    python3 scraper/refresh_schedule.py             # refresh + fetch new descriptions
    python3 scraper/refresh_schedule.py --out new.csv

The `?v=<timestamp>` on the snippet URLs is the program's last-regenerated
time — printed on every run, so you can see at a glance whether the site's data
has moved since your CSV was built.
"""

import argparse
import csv
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CSV_PATH = REPO / "data" / "siggraph2026_schedule_with_descriptions.csv"
CACHE_PATH = HERE / "descriptions_cache.csv"

BASE = "https://s2026.conference-schedule.org"
FULL_PROGRAM = BASE + "/?post_type=page&p=11"
CONTRIBUTORS = BASE + "/contributors/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Conference week is firmly inside PDT (UTC-7); no DST transition to worry about.
PDT = timezone(timedelta(hours=-7))

FIELDS = ["day_and_date", "start_time", "end_time", "title", "program_track",
          "location", "speakers_or_contributors", "affiliations", "keywords_tags",
          "interest_area", "registration_category", "session_url", "description"]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


# --------------------------------------------------------------------------
# Minimal forgiving DOM
# --------------------------------------------------------------------------
class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = None

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def text(self):
        out = []
        stack = [self]
        # depth-first, preserving document order
        def walk(n):
            for c in n.children:
                if isinstance(c, str):
                    out.append(c)
                else:
                    walk(c)
        walk(self)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def find_all(self, tag=None, cls=None):
        found = []
        def walk(n):
            for c in n.children:
                if isinstance(c, str):
                    continue
                if (tag is None or c.tag == tag) and (cls is None or cls in c.classes):
                    found.append(c)
                walk(c)
        walk(self)
        return found

    def find(self, tag=None, cls=None):
        r = self.find_all(tag, cls)
        return r[0] if r else None


class DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # forgiving: unwind to the nearest matching open tag, if any
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(text):
    d = DOM()
    d.feed(text)
    return d.root


# --------------------------------------------------------------------------
# Polite fetching
# --------------------------------------------------------------------------
class Banned(RuntimeError):
    pass


def fetch(url, delay=1.5, retries=2):
    """One request, slowly, with a hard stop if the site starts refusing us."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(delay)
            return body
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                raise Banned(f"HTTP {e.code} from {url} — back off and try later")
            if attempt == retries:
                raise
        except Exception:
            if attempt == retries:
                raise
        time.sleep(delay * (attempt + 2))
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# Program snippets
# --------------------------------------------------------------------------
def snippet_sources(full_program_html):
    """The Full Program page names the five per-day snippet files + version."""
    return re.findall(r'<div class="post-load tablesched" date="([^"]+)" source="([^"]+)"',
                      full_program_html)


def _tag_group(row, cls):
    holder = row.find("div", "ptrack-list")
    if not holder:
        return ""
    grp = next((d for d in holder.find_all("div") if cls in d.classes and "tag-group-list" in d.classes), None)
    if not grp:
        return ""
    return "; ".join(t.text() for t in grp.find_all("div", "program-track") if t.text())


def _people(node):
    return "; ".join(p.text() for p in node.find_all("div", "presenter-name") if p.text())


def _fmt(dt):
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def parse_day(html_text):
    """Parse one day snippet into CSV-shaped rows.

    Two row shapes:
      * session rows  (span.presentation-title) — carry room + event type
      * nested talks  (td.title-speakers-td)    — inherit room + type from parent
    """
    root = parse_html(html_text)
    rows = []
    current = None
    for tr in root.find_all("tr"):
        if "agenda-item" not in tr.classes:
            continue
        s_utc, e_utc = tr.attrs.get("s_utc"), tr.attrs.get("e_utc")
        if not (s_utc and e_utc):
            continue
        start = datetime.strptime(s_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(PDT)
        end = datetime.strptime(e_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(PDT)

        title_span = tr.find("span", "presentation-title")
        if title_span is not None:
            link = title_span.find("a")
            href = (link.attrs.get("href") if link else None)
            # Group headers toggle open in-page and have no href; the original
            # scrape recorded these as ".../null". Keep that convention.
            url = (BASE + href) if href and href.startswith("/") else (href or BASE + "/null")
            type_node = tr.find("span", "presentation-type") or tr.find("div", "event-type-name")
            loc_node = tr.find("span", "presentation-location")
            spk_node = tr.find("span", "presentation-speaker")
            row = {
                "title": title_span.text().replace("\xa0", " ").strip(),
                "program_track": type_node.text() if type_node else "",
                "location": loc_node.text() if loc_node else "",
                "speakers_or_contributors": _people(spk_node) if spk_node else "",
            }
            current = row
        else:
            td = tr.find("td", "title-speakers-td")
            if td is None:
                continue
            link = td.find("a")
            href = link.attrs.get("href") if link else None
            url = (BASE + href) if href and href.startswith("/") else (href or BASE + "/null")
            speakers_holder = td.find("div", "speakers-line")
            row = {
                "title": (link.text() if link else td.text()).replace("\xa0", " ").strip(),
                # nested talks show under their parent's room / program track
                "program_track": current["program_track"] if current else "",
                "location": current["location"] if current else "",
                "speakers_or_contributors": _people(speakers_holder) if speakers_holder else "",
            }

        row.update({
            "day_and_date": start.strftime("%A, %-d %B %Y"),
            "start_time": _fmt(start),
            "end_time": _fmt(end),
            "keywords_tags": _tag_group(tr, "keyword"),
            "interest_area": _tag_group(tr, "interest-area"),
            "registration_category": _tag_group(tr, "registration-category"),
            "session_url": url,
            "_utc": s_utc,
        })
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Contributors -> affiliations
# --------------------------------------------------------------------------
def parse_contributors(html_text):
    """-> ({session_url: {institutions}}, {session_url: {names}}, person_count)"""
    root = parse_html(html_text)
    by_url_inst, by_url_name = {}, {}
    n = 0
    for entry in root.find_all("div", "presenter-entry"):
        name_node = entry.find("div", "presenter-name")
        inst_node = entry.find("div", "presenter-institution")
        if not name_node:
            continue
        n += 1
        name = name_node.text()
        inst = inst_node.text() if inst_node else ""
        for pres in entry.find_all("div", "presentation"):
            link = pres.find("a")
            if not link:
                continue
            href = link.attrs.get("href", "")
            url = BASE + href if href.startswith("/") else href
            if inst:
                by_url_inst.setdefault(url, set()).add(inst)
            if name:
                by_url_name.setdefault(url, set()).add(name)
    return by_url_inst, by_url_name, n


# --------------------------------------------------------------------------
# Descriptions (only for rows we don't already have)
# --------------------------------------------------------------------------
ABSTRACT_RE = re.compile(r'<span class="abstract">(.*?)</span></div>', re.S)
SESSION_RE = re.compile(r'<div class="info-section session-description">.*?</span>(.*?)</div>', re.S)


def strip_html(fragment):
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = re.sub(r"</p\s*>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    import html as _h
    t = _h.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract_description(page):
    m = ABSTRACT_RE.search(page) or SESSION_RE.search(page)
    return strip_html(m.group(1)) if m else ""


def load_cache():
    cache = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                cache[r["url"]] = r["description"]
    return cache


def save_cache(cache):
    with open(CACHE_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "description"])
        for u, d in sorted(cache.items()):
            w.writerow([u, d])


# --------------------------------------------------------------------------
def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def row_key(r):
    """Identity of a session across refreshes.

    Prefer the session URL — titles and times both drift, the URL doesn't.
    Group-header rows have no page (".../null"), so fall back to day+title.
    """
    u = r.get("session_url", "")
    if u and not u.endswith("/null"):
        return ("url", u)
    return ("hdr", r["day_and_date"], norm(r["title"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report differences only; write nothing, fetch no descriptions")
    ap.add_argument("--out", type=Path, default=CSV_PATH, help="output CSV (default: overwrite data CSV)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default 1.5)")
    args = ap.parse_args()

    old = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            old = list(csv.DictReader(f))
    print(f"current CSV: {len(old)} rows")

    try:
        print("• fetching Full Program page …")
        fp = fetch(FULL_PROGRAM, args.delay)
        sources = snippet_sources(fp)
        if not sources:
            sys.exit("Could not find snippet sources — the site's markup may have changed.")
        version = re.search(r"[?&]v=(\d+)", sources[0][1])
        if version:
            ts = datetime.fromtimestamp(int(version.group(1)))
            print(f"  program last regenerated: {ts:%Y-%m-%d %H:%M} (v={version.group(1)})")

        rows = []
        for date, src in sources:
            url = BASE + src if src.startswith("/") else src
            print(f"• fetching day snippet {date} …")
            rows.extend(parse_day(fetch(url, args.delay)))

        print("• fetching contributors index …")
        inst_by_url, names_by_url, n_people = parse_contributors(fetch(CONTRIBUTORS, args.delay))
        print(f"  {n_people} contributors indexed, {len(inst_by_url)} sessions have affiliations")
    except Banned as e:
        sys.exit(f"\nBACKED OFF: {e}\nWait a while (the ban is time-based and per-IP) and re-run.")

    # de-dupe: multi-day sessions appear in more than one day table
    seen, new_rows = set(), []
    for r in rows:
        k = (r["session_url"], r["_utc"], r["title"])
        if k in seen:
            continue
        seen.add(k)
        r.pop("_utc")
        new_rows.append(r)

    # attach affiliations, and backfill speakers where the grid had none
    for r in new_rows:
        r["affiliations"] = "; ".join(sorted(inst_by_url.get(r["session_url"], ())))
        if not r["speakers_or_contributors"]:
            r["speakers_or_contributors"] = "; ".join(sorted(names_by_url.get(r["session_url"], ())))

    new_rows.sort(key=lambda r: (r["day_and_date"], r["start_time"], r["title"]))

    old_by_key = {row_key(r): r for r in old}
    new_by_key = {row_key(r): r for r in new_rows}
    added = [r for k, r in new_by_key.items() if k not in old_by_key]
    removed = [r for k, r in old_by_key.items() if k not in new_by_key]

    moved, reroomed, retitled = [], [], []
    for k, r in new_by_key.items():
        o = old_by_key.get(k)
        if not o:
            continue
        if (o["day_and_date"], o["start_time"], o["end_time"]) != (r["day_and_date"], r["start_time"], r["end_time"]):
            moved.append((o, r))
        if norm(o["location"]) != norm(r["location"]):
            reroomed.append((o, r))
        if norm(o["title"]) != norm(r["title"]):
            retitled.append((o, r))

    def show(label, items, fmt, limit=30):
        print(f"\n{label} ({len(items)})")
        for it in items[:limit]:
            print(fmt(it))
        if len(items) > limit:
            print(f"      … and {len(items) - limit} more")

    print(f"\n{'='*66}\nDIFF vs current CSV — {len(new_rows)} sessions live now (was {len(old)})\n{'='*66}")
    show("  ADDED", added, lambda r: f"      {r['day_and_date'][:9]:9s} {r['start_time']:>7s}  {r['title'][:60]}")
    show("  NO LONGER LISTED", removed, lambda r: f"      {r['day_and_date'][:9]:9s} {r['start_time']:>7s}  {r['title'][:60]}")
    show("  RESCHEDULED", moved, lambda p: (f"      {p[1]['title'][:60]}\n"
                                            f"        {p[0]['day_and_date'][:9]} {p[0]['start_time']}-{p[0]['end_time']}"
                                            f"  ->  {p[1]['day_and_date'][:9]} {p[1]['start_time']}-{p[1]['end_time']}"))
    show("  ROOM CHANGED", reroomed, lambda p: f"      {p[1]['title'][:44]:44s} {p[0]['location']!r} -> {p[1]['location']!r}")
    show("  RETITLED", retitled, lambda p: f"      {p[0]['title'][:56]!r}\n        -> {p[1]['title'][:56]!r}", 10)

    if args.check:
        print(f"\n--check: nothing written. Requests used this run: {2 + len(sources)}")
        return

    # descriptions: reuse cache + old CSV, fetch only what's genuinely missing
    cache = load_cache()
    for r in old:
        if r.get("description") and r.get("session_url"):
            cache.setdefault(r["session_url"], r["description"])

    need = sorted({r["session_url"] for r in new_rows
                   if r["session_url"] not in cache
                   and not r["session_url"].endswith("/null")
                   and ("p=15" in r["session_url"] or "p=16" in r["session_url"])})
    print(f"\n• {len(need)} session pages need a description fetch")
    try:
        for i, u in enumerate(need, 1):
            print(f"  [{i}/{len(need)}] {u[-40:]}")
            cache[u] = extract_description(fetch(u, args.delay))
    except Banned as e:
        print(f"\nBACKED OFF while fetching descriptions: {e}")
        print("Progress is cached — re-run later to finish.")
    finally:
        save_cache(cache)

    for r in new_rows:
        r["description"] = cache.get(r["session_url"], "")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in new_rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    filled = sum(1 for r in new_rows if r["description"])
    withaff = sum(1 for r in new_rows if r["affiliations"])
    print(f"\nwrote {args.out} — {len(new_rows)} rows, {filled} with descriptions, {withaff} with affiliations")


if __name__ == "__main__":
    main()
