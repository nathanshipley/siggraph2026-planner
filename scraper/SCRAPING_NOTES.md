# SIGGRAPH 2026 schedule — add a `description` column (status + how-to)

## Goal
Add a `description` column to `siggraph2026_schedule.csv` containing each session's
abstract, written to `siggraph2026_schedule_with_descriptions.csv` (same rows/order
plus that column).

## What we learned (corrects the original handoff)
The original plan assumed session pages are empty JS shells that require a headless
browser (Playwright). **That is not true.** The pages are server-rendered plain HTML —
a normal HTTP GET returns the full abstract, including the exact `gensub_279` page the
old note claimed failed. So **no browser, no Playwright, no pip installs** are needed.
(The old `add_descriptions.py` is superseded by `fetch_descriptions.py`.)

Two page templates, both carry the text server-side:
- `?...p=15&id=X&sess=Y`  (presentation)  → `<span class="abstract">…</span>`
- `?...p=16&sess=Y`        (session)       → `<div class="info-section session-description">…</div>`

## Data on disk
- `siggraph2026_schedule.csv` — 981 rows, 11 columns incl. `session_url`.
- **878 unique fetchable URLs.** Join is per-row on the exact `session_url`.
- Rows that will (correctly) stay blank:
  - **4 rows** have no URL (3× "Exhibition", 1× "Technical Papers Closing").
  - **85 rows** whose URL is the literal `https://s2026.conference-schedule.org/null`
    (session-group headers, e.g. "Rendering and Materials") — no sub-page exists.
  - Some `p=16` poster-group pages (e.g. "Posters: 3D & Geometry") genuinely have no
    abstract on the site.

## The one real gotcha: rate limiting
`s2026.conference-schedule.org` is WordPress behind a firewall that **bans your IP**
if you hit it with a burst of concurrent connections. Symptom: requests start returning
HTTP 000 (connection timeout) and the ban persists for a while (10+ min of silence did
not clear it in testing). Repeated probing during a ban may extend it.

=> **Fetch gently.** Use 1–2 workers and a per-request delay. The script below does this
and caches incrementally, so an interrupted/blocked run resumes without re-fetching.

## How to run (fetch_descriptions.py — stdlib only)
    # gentle + resumable (recommended). ~15–25 min for all 878 URLs.
    python3 fetch_descriptions.py --workers 1 --delay 1.2

    # options:
    #   --limit 10     only the first 10 unique URLs (quick test)
    #   --workers N    parallel fetches (KEEP LOW — 1 or 2; higher gets you banned)
    #   --delay S      seconds to pause after each request (default 0.6; raise if blocked)
Results cache to `descriptions_cache.json`. Re-running skips cached URLs, so if you get
blocked partway, just wait and run the same command again to finish the rest.

## Windows / no-Python option (recommended for a locked-down work machine)
`Fetch-Descriptions.ps1` is a pure-PowerShell port — no Python, no installs, runs on the
built-in Windows PowerShell. It is deliberately slow and safe: ONE request at a time, a
pause after each, a longer pause every 40, and a **circuit breaker** that stops the moment
it sees the ban signature (several failures in a row) instead of digging in deeper. It is
resumable via `descriptions_cache.csv`. Copy `siggraph2026_schedule.csv` +
`Fetch-Descriptions.ps1` into the same folder, then from that folder:

    # quick test (5 URLs) — confirm it works first
    powershell -ExecutionPolicy Bypass -File .\Fetch-Descriptions.ps1 -Limit 5
    # full run (~50 min, unattended, resumable)
    powershell -ExecutionPolicy Bypass -File .\Fetch-Descriptions.ps1

## If you keep getting HTTP 000 (banned)
- Stop for a while (the ban is time-based).
- Or run from a **different network/IP** (e.g. another machine, tethering) — the ban is
  per-IP, and the script is turnkey with no dependencies.

## Done when
- Output CSV = original 981 rows, original order, plus `description`.
- Spot-check 3–5 talks against the live site.
- Expected fill: ~824 presentation rows + the p=16 session pages that have a
  description; the ~89 blanks above are expected.
