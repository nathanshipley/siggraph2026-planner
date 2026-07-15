# SIGGRAPH 2026 Session Planner (unofficial)

A single-file, offline **personal session planner** for [SIGGRAPH 2026](https://s2026.siggraph.org/)
(Los Angeles, 19–23 July 2026), plus the full conference schedule as CSV.

> **Unofficial community tool.** This project is not affiliated with, sponsored by, or endorsed by
> ACM SIGGRAPH. SIGGRAPH is a registered trademark of the Association for Computing Machinery.
> All session content belongs to its authors and ACM SIGGRAPH — see [Data & attribution](#data--attribution).

![My Schedule calendar view — conflicts get red outlines](docs/screenshot-calendar.png)

![Browse view — filter by day, track, and registration level; expand descriptions inline](docs/screenshot-browse.png)

## Quick start

Download **[`planner.html`](planner.html)** and double-click it. That's the whole app — no server,
no install, works offline. Your picks are saved in your browser (localStorage) and survive reloads.

What it does:

- **Browse & filter** all 981 sessions by day, program track, registration level (Full Conference
  Supporter / Full Conference / Experience / Discover), and free-text search over titles and abstracts.
- **My Schedule**: a five-day time-bar calendar of your checked sessions. Overlaps are drawn
  side-by-side with red outlines so conflicts jump out.
- **Session groups**: Talks / Technical Papers / Educator's Forum / Art Papers blocks are linked to
  their individual presentations, exactly like the twirl-downs on the official schedule site.
- **Export to Google Calendar**: one click generates an `.ics` of your picks with correct
  `America/Los_Angeles` times (import via Google Calendar → Settings → Import & export).
- Light and dark mode.

## What's in the repo

| Path | What it is |
|---|---|
| `planner.html` | The self-contained planner app (schedule data embedded) |
| `data/siggraph2026_schedule_with_descriptions.csv` | Full schedule, 981 rows, incl. session abstracts |
| `data/siggraph2026_schedule.csv` | Same schedule without the abstract text (facts only) |
| `data/siggraph2026_exhibitors.csv` | Exhibitor list |
| `src/` | Template + build script — regenerate `planner.html` from the CSV |
| `scraper/` | The scripts that fetched the abstracts, plus notes |

CSV columns: `day_and_date, start_time, end_time, title, program_track, location,
speakers_or_contributors, keywords_tags, interest_area, registration_category, session_url, description`.

## Data notes

- Scraped from the official schedule at
  [s2026.conference-schedule.org](https://s2026.conference-schedule.org/) on **15 July 2026**.
  The official schedule changes — always confirm times/rooms there before you commit your feet.
- All times are local Pacific (PDT).
- ~85 rows are **session-group headers** (e.g. a Talks block containing four 20-minute talks).
  On the official site these have no page of their own; in the CSV their `session_url` is the
  literal `…/null`. The planner detects these and nests their presentations automatically.
- A few rows (Exhibition, some poster groups) legitimately have no URL or abstract.

## Rebuilding the planner

```bash
python3 src/build_planner.py            # data/ CSV + src/ template -> planner.html
python3 src/build_planner.py --help     # --out, --no-seeds
```

Python 3.8+, stdlib only.

## Re-scraping

`scraper/fetch_descriptions.py` (or the PowerShell port) re-fetches session abstracts.
**Please fetch gently** — the schedule site rate-bans bursty clients. Use 1–2 workers with a
delay (the default settings); results are cached and resumable. See `scraper/SCRAPING_NOTES.md`.

## Data & attribution

- Schedule **facts** (titles, times, rooms, speakers) are factual data, included here to help
  attendees plan.
- Session **abstracts/descriptions** were written by their respective authors and are published by
  ACM SIGGRAPH (© 2026 SIGGRAPH / the individual authors). They are reproduced here, unmodified
  and with links back to the official session pages, solely to make the conference easier to
  navigate. If you are a rights holder and want content removed, **open an issue** and it will be
  taken down promptly.
- Code (planner, build script, scrapers) is [MIT licensed](LICENSE). The license does **not**
  cover the session descriptions.

## Credits

Built by an attendee, for attendees. Have fun at the show. 🎬
