#!/usr/bin/env python3
"""
fetch_descriptions.py
---------------------
Adds a `description` column to the SIGGRAPH 2026 schedule CSV by reading each
session's abstract straight from its sub-page.

NO BROWSER NEEDED. The handoff assumed s2026.conference-schedule.org renders
abstracts client-side (requiring Playwright). That is not the case: the pages
are server-rendered plain HTML, so a normal HTTP GET returns the abstract. This
script therefore uses only the Python standard library -- no pip installs, no
Chromium, ~1-2 min for all ~900 pages.

Two page templates, both carry the text server-side:
  * p=15 presentation pages -> <span class="abstract">...</span>
  * p=16 session pages       -> <div class="info-section session-description">...
Rows whose URL is blank or the literal ".../null" (session-group headers) get
left blank, as do poster-group session pages that genuinely have no abstract.

Run:
    python3 fetch_descriptions.py
    python3 fetch_descriptions.py --limit 10      # quick test
    python3 fetch_descriptions.py --workers 12    # more parallelism

Results cache to descriptions_cache.json, so re-runs / interrupted runs resume.
"""

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

ABSTRACT_RE = re.compile(r'<span class="abstract">(.*?)</span></div>', re.S)
SESSION_RE = re.compile(
    r'<div class="info-section session-description">.*?</span>(.*?)</div>', re.S)


def strip_html(fragment: str) -> str:
    """Turn an HTML fragment into clean text, keeping paragraph breaks."""
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = re.sub(r"</p\s*>", "\n\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract(page_html: str):
    """Return (description, kind). kind is for diagnostics only."""
    m = ABSTRACT_RE.search(page_html)
    if m:
        return strip_html(m.group(1)), "presentation"
    m = SESSION_RE.search(page_html)
    if m:
        return strip_html(m.group(1)), "session"
    if "could not find the page" in page_html.lower():
        return "", "error_notfound"
    return "", "no_description"


def fetch_one(url: str, delay: float = 0.6, retries: int = 4):
    """Fetch + extract one URL, with retry + exponential backoff on throttling.

    The conference site rate-limits bursts of connections (connections start
    timing out with HTTP 000). We keep concurrency low, pause `delay` seconds
    after each request, and back off hard when a request fails.
    Returns (url, desc, kind).
    """
    last = "error_fetch"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                page = r.read().decode("utf-8", "replace")
            desc, kind = extract(page)
            # a warm page that momentarily returned the JS-shell error is worth a retry
            if kind == "error_notfound" and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            time.sleep(delay)
            return url, desc, kind
        except Exception as e:  # noqa: BLE001
            last = f"error_fetch:{type(e).__name__}"
            time.sleep(3.0 * (attempt + 1))  # 3s, 6s, 9s backoff
    return url, "", last


def is_real_url(u: str) -> bool:
    u = (u or "").strip()
    if not u:
        return False
    return urlparse(u).path != "/null"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="siggraph2026_schedule.csv")
    ap.add_argument("--out", dest="outp",
                    default="siggraph2026_schedule_with_descriptions.csv")
    ap.add_argument("--cache", default="descriptions_cache.json")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.6,
                    help="seconds to pause after each request (politeness)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only fetch first N unique URLs (0 = all)")
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if "session_url" not in fieldnames:
        sys.exit("ERROR: expected a 'session_url' column in the input CSV.")

    # unique, real URLs to fetch
    urls = []
    seen = set()
    for r in rows:
        u = r["session_url"].strip()
        if is_real_url(u) and u not in seen:
            seen.add(u)
            urls.append(u)
    if args.limit:
        urls = urls[:args.limit]

    cache = {}
    if Path(args.cache).exists():
        cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    todo = [u for u in urls if u not in cache]
    print(f"{len(rows)} rows | {len(urls)} unique real URLs | "
          f"{len(cache)} cached | {len(todo)} to fetch")

    def save_cache():
        Path(args.cache).write_text(
            json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    kinds = {}
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(fetch_one, u, args.delay): u for u in todo}
            for fut in as_completed(futs):
                url, desc, kind = fut.result()
                cache[url] = desc
                kinds[kind] = kinds.get(kind, 0) + 1
                done += 1
                if done % 20 == 0 or done == len(todo):
                    errs = sum(v for k, v in kinds.items() if k.startswith("error"))
                    print(f"  ...{done}/{len(todo)} fetched "
                          f"(errors so far: {errs})", flush=True)
                    save_cache()  # incremental: resumable if interrupted
    finally:
        save_cache()

    # write output CSV: same rows/order + description column
    out_fields = list(fieldnames) + ["description"]
    with open(args.outp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for r in rows:
            u = r["session_url"].strip()
            r["description"] = cache.get(u, "") if is_real_url(u) else ""
            w.writerow(r)

    filled = sum(1 for r in rows if r["description"])
    print(f"\nDone -> {args.outp}")
    print(f"{filled} of {len(rows)} rows have a description "
          f"({len(rows) - filled} blank).")
    if kinds:
        print("fetch outcomes this run:", kinds)


if __name__ == "__main__":
    main()
