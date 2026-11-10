# Job Board Shortlister

A small Python pipeline that pulls open jobs from the public job-board APIs of the companies you
choose, filters them against hard rules you define, scores what survives out of 100, and writes a
ranked `shortlist.md` every day. It only reads public data and never applies to anything.

Standard library plus `pyyaml`. No scraping, no logins, no accounts.

## How it works

```
companies.txt ──► find_boards.py ──► boards.yaml        (which ATS board each company uses)
                                          │
titles.yaml ───────────────────────► discover.py ──► candidates.json   (new, title-matched jobs)
                                          │
criteria.yaml ─────────────────────► score.py ─────► shortlist.md + rejected.md
```

1. **`find_boards.py`** probes Greenhouse, Lever (US and EU), and Ashby for each company name and
   records the working board slug. Companies with no public board are written to `boards_failed.txt`.
2. **`discover.py`** fetches every board, keeps jobs whose title matches the regex targets in
   `titles.yaml`, drops excluded titles, and de-duplicates against `seen.json` so each run only
   queues new postings.
3. **`score.py`** applies the hard filters, scores survivors on a 100-point scale, and writes the
   ranked shortlist. It re-evaluates the whole cumulative store on every run, so editing
   `criteria.yaml` takes effect immediately without refetching anything.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python find_boards.py    # once, or when you edit companies.txt
.venv/bin/python discover.py && .venv/bin/python score.py
```

Open `shortlist.md`. `rejected.md` lists every rejected job with the reason, so you can spot-check
the filters instead of trusting them. See `examples/shortlist.sample.md` for the output format.

## Configuration

| File | What you set |
|---|---|
| `criteria.yaml` | Hard filters (salary floor, employment type, work mode, hybrid location, company size), scoring weights, keyword lists |
| `titles.yaml` | Target titles as ranked regexes, extra keywords, excluded title words |
| `companies.txt` | One company per line |
| `company_sizes.yaml` | Estimated headcount per company, used by the size filter |
| `mode_overrides.yaml` | Optional hand-verified work-mode facts for postings that do not state one |

The sample configuration targets creative operations and production roles with a Chicago-area
hybrid rule. Change the keyword lists, location terms, and weights to fit your own search.

## Design decisions

- **Missing data means "verify", not "reject".** If salary, benefits, or work mode is not stated,
  the job stays in the list with a flag, and the unknown category gets partial credit.
- **Explicit text beats metadata.** A posting that says "in the office twice a week" outranks a
  "Remote" location label. This caught real cases where a listing said Remote but required an
  office day.
- **Regexes are written against false positives I actually hit.** For example, "hybrid role: part
  strategist, part maker" is not a hybrid work mode, and "independent contractor" boilerplate in a
  legal footer does not mean the job is contract work. Employment-type patterns only apply to text
  describing the role itself.
- **Polite by construction.** Small delay between requests, one request per board per run, public
  JSON endpoints only.
- **Hard rules are code, not suggestions.** A non-matching hybrid location is dropped in
  `score.py`, not left to a human to remember.

## Limits

- Supports Greenhouse, Lever, and Ashby only. Workday and Workable boards are not covered, so some
  companies show up in `boards_failed.txt`.
- Salary and work-mode parsing is heuristic, which is why uncertain results are flagged `verify`.
- Headcounts in `company_sizes.yaml` are estimates.

## Built with AI assistance

I designed the requirements, the filters, and the scoring model, tested the output against real
postings, and iterated on the failures. The code was written with Claude Code, and the false-positive
fixes above came from reviewing its output against live job pages.
