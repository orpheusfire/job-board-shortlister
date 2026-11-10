#!/usr/bin/env python3
"""Pull jobs from every board in boards.yaml, keep title matches from titles.yaml,
dedupe against seen.json, and queue the NEW ones in candidates.json for score.py.

    python discover.py          # normal daily run
    python discover.py --all    # ignore seen.json and re-queue every title match
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
UA = "job-board-shortlister/1.0 (public job board check)"
DELAY = 0.3  # seconds between board requests


# ---------------------------------------------------------------- fetching
def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def to_text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse sends it) -> plain text."""
    if not raw:
        return ""
    s = html.unescape(raw)
    s = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h\d|/ul)\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def interval_norm(s: str | None) -> str:
    s = (s or "").lower()
    if "hour" in s:
        return "hour"
    if "month" in s:
        return "month"
    if "week" in s:
        return "week"
    return "year"


def fetch_greenhouse(b):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs?content=true&pay_transparency=true")
    for j in data.get("jobs", []):
        meta = {m.get("name", ""): m.get("value") for m in (j.get("metadata") or [])}
        wp = next((str(v) for k, v in meta.items() if "workplace" in k.lower() or "work type" in k.lower()
                   or "remote" in k.lower()), None)
        emp = next((str(v) for k, v in meta.items() if "employment" in k.lower() or "job type" in k.lower()
                    or "worker type" in k.lower()), None)
        sal = [{"min": r["min_cents"] / 100 if r.get("min_cents") is not None else None,
                "max": r["max_cents"] / 100 if r.get("max_cents") is not None else None,
                "currency": r.get("currency_type"), "interval": "year", "label": r.get("title")}
               for r in (j.get("pay_input_ranges") or [])]
        yield {
            "id": str(j["id"]), "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"), "department": ", ".join(d["name"] for d in j.get("departments", [])),
            "description": to_text(j.get("content")), "employment_type": emp, "workplace_type": wp,
            "salary": sal, "posted": j.get("first_published") or j.get("updated_at"),
        }


def fetch_lever(b):
    host = "api.eu.lever.co" if b.get("region") == "eu" else "api.lever.co"
    for j in get_json(f"https://{host}/v0/postings/{b['slug']}?mode=json"):
        cat = j.get("categories") or {}
        sr = j.get("salaryRange")
        sal = [{"min": sr.get("min"), "max": sr.get("max"), "currency": sr.get("currency"),
                "interval": interval_norm(sr.get("interval")), "label": None}] if sr else []
        desc = j.get("descriptionPlain") or to_text(j.get("description"))
        for lst in j.get("lists") or []:
            desc += "\n" + (lst.get("text") or "") + "\n" + to_text(lst.get("content"))
        desc += "\n" + (j.get("additionalPlain") or to_text(j.get("additional")))
        yield {
            "id": j["id"], "title": (j.get("text") or "").strip(),
            "location": cat.get("location") or ", ".join(cat.get("allLocations") or []),
            "url": j.get("hostedUrl"), "department": cat.get("department") or "",
            "description": desc.strip(), "employment_type": cat.get("commitment"),
            "workplace_type": j.get("workplaceType"), "salary": sal,
            "posted": j.get("createdAt"), "country": j.get("country"),
        }


def fetch_ashby(b):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}?includeCompensation=true")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        sal = []
        for c in (j.get("compensation") or {}).get("summaryComponents") or []:
            if c.get("compensationType") == "Salary":
                sal.append({"min": c.get("minValue"), "max": c.get("maxValue"), "currency": c.get("currencyCode"),
                            "interval": interval_norm(c.get("interval")), "label": None})
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc = j.get("location") or ""
        if j.get("secondaryLocations"):
            loc += "; " + "; ".join(s.get("location", "") for s in j["secondaryLocations"])
        wp = j.get("workplaceType") or ("Remote" if j.get("isRemote") else None)
        yield {
            "id": j["id"], "title": (j.get("title") or "").strip(), "location": loc,
            "url": j.get("jobUrl"), "department": j.get("department") or j.get("team") or "",
            "description": j.get("descriptionPlain") or to_text(j.get("descriptionHtml")),
            "employment_type": j.get("employmentType"), "workplace_type": wp, "salary": sal,
            "posted": j.get("publishedAt"), "country": addr.get("addressCountry"),
        }


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---------------------------------------------------------------- title matching
def keyword_regex(kw: str) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s-]+", kw.strip().lower()) if p]
    return re.compile(r"\b" + r"[\s-]?".join(parts), re.I)


class TitleMatcher:
    def __init__(self, cfg: dict):
        self.targets = [(t["rank"], t["name"], [re.compile(p, re.I) for p in t["patterns"]]) for t in cfg["targets"]]
        self.keywords = [(k, keyword_regex(k)) for k in cfg.get("keywords", [])]
        self.exclude = [re.compile(p, re.I) for p in cfg.get("exclude", [])]

    def match(self, title: str):
        """Return (rank, reason) or None. rank 99 = keyword-only match."""
        if any(p.search(title) for p in self.exclude):
            return None
        for rank, name, pats in sorted(self.targets, key=lambda t: t[0]):
            if any(p.search(title) for p in pats):
                return rank, f"title target #{rank}: {name}"
        for kw, rx in self.keywords:
            if rx.search(title):
                return 99, f"title keyword: {kw}"
        return None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore seen.json; re-queue every title match")
    args = ap.parse_args()

    today = date.today().isoformat()
    matcher = TitleMatcher(yaml.safe_load((ROOT / "titles.yaml").read_text()))
    boards = yaml.safe_load((ROOT / "boards.yaml").read_text())["boards"]

    seen_path, cand_path = ROOT / "seen.json", ROOT / "candidates.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {"meta": {}, "jobs": {}}
    queue = {c["key"]: c for c in json.loads(cand_path.read_text())} if cand_path.exists() else {}

    failed_boards, total_fetched, total_matched, new_count = [], 0, 0, 0
    for b in boards:
        label = f"{b['company']} ({b['platform']}:{b['slug']})"
        try:
            jobs = list(FETCHERS[b["platform"]](b))
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError, OSError) as e:
            print(f"  ! {label}: fetch failed ({type(e).__name__}: {e})", file=sys.stderr)
            failed_boards.append(b["company"])
            continue
        n_match = n_new = 0
        for j in jobs:
            m = matcher.match(j["title"])
            if not m:
                continue
            n_match += 1
            key = f"{b['platform']}:{b['slug']}:{j['id']}"
            rec = seen["jobs"].get(key)
            is_new = rec is None
            if is_new:
                seen["jobs"][key] = {"first_seen": today, "last_seen": today, "company": b["company"],
                                     "title": j["title"]}
            else:
                rec["last_seen"] = today
            if is_new or args.all:
                n_new += 1
                queue[key] = {**j, "key": key, "company": b["company"], "platform": b["platform"],
                              "slug": b["slug"], "title_rank": m[0], "title_reason": m[1],
                              "first_seen": seen["jobs"][key]["first_seen"]}
        total_fetched += len(jobs)
        total_matched += n_match
        new_count += n_new
        print(f"  {label}: {len(jobs)} jobs, {n_match} title matches, {n_new} new")
        time.sleep(DELAY)

    seen["meta"] = {"last_run": today, "failed_boards": failed_boards}
    seen_path.write_text(json.dumps(seen, indent=1, ensure_ascii=False))
    cand_path.write_text(json.dumps(list(queue.values()), indent=1, ensure_ascii=False))
    print(f"\n{total_fetched} jobs fetched, {total_matched} title matches, {new_count} new -> "
          f"{len(queue)} queued in candidates.json" + (f" ({len(failed_boards)} boards failed)" if failed_boards else ""))


if __name__ == "__main__":
    main()
