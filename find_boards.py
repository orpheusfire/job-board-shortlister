#!/usr/bin/env python3
"""Probe the public Greenhouse / Lever / Ashby job-board APIs for each company in companies.txt.

Writes boards.yaml (working slugs) and boards_failed.txt (companies where nothing worked).
Only public, unauthenticated GET endpoints are used.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
UA = "job-board-shortlister/1.0 (public job board check)"

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "lever_eu": "https://api.eu.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}

# Extra slug guesses where the company name alone won't find the board.
EXTRA_SLUGS = {
    "Warner Bros. Discovery": ["wbd", "warnerbrosdiscovery", "warnermedia", "wbdcareers"],
    "Condé Nast": ["condenast", "conde-nast", "condenastcareers"],
    "Dotdash Meredith": ["dotdashmeredith", "dotdash", "meredith"],
    "Vox Media": ["voxmedia", "vox"],
    "Getty Images": ["gettyimages", "getty"],
    "Riot Games": ["riotgames", "riot"],
    "Epic Games": ["epicgames", "epic"],
    "Paramount": ["paramount", "paramountglobal", "paramountcareers"],
    "Netflix": ["netflix", "netflixjobs"],
    "Adobe": ["adobe", "adobecareers"],
    "Canva": ["canva", "canvacareers"],
}


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"[^\w\s-]", "", name.lower().replace("é", "e").replace(".", ""))
    words = base.split()
    out = ["".join(words), "-".join(words), "_".join(words)]
    out += EXTRA_SLUGS.get(name, [])
    if len(words) > 1:
        out.append(words[0])
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # network / JSON problems
        return f"{type(e).__name__}", None


def probe(platform: str, slug: str):
    """Return dict describing a working board, or None."""
    status, data = fetch(ENDPOINTS[platform].format(slug=slug))
    if status != 200 or data is None:
        return None
    if platform == "greenhouse":
        jobs = data.get("jobs", [])
        sample = jobs[0]["title"] if jobs else None
        _, meta = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        board_name = (meta or {}).get("name")
    elif platform in ("lever", "lever_eu"):
        if not isinstance(data, list):
            return None
        jobs = data
        sample = jobs[0].get("text") if jobs else None
        board_name = None
    else:  # ashby
        jobs = data.get("jobs", [])
        sample = jobs[0].get("title") if jobs else None
        board_name = None
    return {"platform": "lever" if platform == "lever_eu" else platform,
            "region": "eu" if platform == "lever_eu" else "us",
            "slug": slug, "job_count": len(jobs), "board_name": board_name, "sample_title": sample}


def probe_company(name: str):
    found, tried = [], []
    for slug in slug_candidates(name):
        for platform in ENDPOINTS:
            tried.append(f"{platform}:{slug}")
            hit = probe(platform, slug)
            if hit:
                found.append(hit)
            time.sleep(0.05)
    return name, found, tried


def main():
    companies = [c.strip() for c in (ROOT / "companies.txt").read_text().splitlines() if c.strip()]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(probe_company, companies))

    boards, failed = {}, []
    for name, found, tried in results:
        if found:
            boards[name] = found
            for f in found:
                print(f"OK    {name:24s} {f['platform']:10s} {f['slug']:22s} jobs={f['job_count']:<4} "
                      f"name={f['board_name']!r} sample={f['sample_title']!r}")
        else:
            failed.append((name, tried))
            print(f"FAIL  {name}")

    (ROOT / "boards_raw.yaml").write_text(yaml.safe_dump(boards, sort_keys=False, allow_unicode=True))
    with open(ROOT / "boards_failed.txt", "w") as f:
        for name, tried in failed:
            f.write(f"{name}\ttried: {', '.join(tried)}\n")
    print(f"\n{len(boards)} companies with working board(s), {len(failed)} failed")


if __name__ == "__main__":
    main()
