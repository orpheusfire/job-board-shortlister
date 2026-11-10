#!/usr/bin/env python3
"""Apply the hard filters in criteria.yaml, score the survivors (100 pts), write shortlist.md.

Reads candidates.json (queued by discover.py), merges the jobs into scored.json (a cumulative store),
then re-evaluates EVERYTHING in the store against the current criteria.yaml, so edits to the criteria
take effect immediately. Only jobs still open on their board appear in shortlist.md.

    python score.py            # score + write shortlist.md / rejected.md
    python score.py --top 15   # also print the top N to the terminal
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).parent

NON_US = ["india", "canada", "united kingdom", "uk", "germany", "berlin", "london", "mexico", "brazil", "australia",
          "japan", "singapore", "ireland", "france", "spain", "poland", "netherlands", "sweden", "toronto",
          "vancouver", "montreal", "quebec", "dublin", "seoul", "tokyo", "korea", "china", "shanghai", "paris",
          "amsterdam", "stockholm", "warsaw", "sydney", "melbourne", "argentina", "colombia", "emea", "apac",
          "philippines", "israel", "tel aviv", "dubai", "uae", "italy", "portugal", "romania", "czech"]
US_MARKERS = re.compile(r"\b(united states|usa|u\.s\.|us)\b|,\s*[A-Z]{2}\b(?!\w)", re.I)
STATE_CODE = re.compile(r",\s*[A-Z]{2}(\b|$)")

OFFICE_BASED = re.compile(r"work from (or relocate to|the office)|willing to work from the office|relocate to|"
                          r"must (be able to )?(work|be) (from|in|located in) (our|the) .{0,30}office|"
                          r"you can work from [A-Z][a-z]+(?: [A-Z][a-z]+)?, [A-Z]{2}", re.I)
GEO_ADJUSTED = re.compile(r"not been adjusted for any applicable geographic|geographic (differential|location)|"
                          r"location[- ]based (pay|strategy|compensation)|pay zone|geo(graphic)? (tier|zone)", re.I)

SALARY_RX = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s*(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])")

# Description patterns must describe THIS role's employment type, not duties ("engaging freelance editors")
# or boilerplate ("employment or an independent contractor role, as applicable").
_ROLE = r"(role|position|opportunity|engagement|assignment|basis)"
DESC_EMPLOYMENT_REJECT = {
    "1099": r"\b1099\b",
    "contract": rf"\b((this|the) (is )?(an? )?(independent )?contract(or)? {_ROLE}|"
                rf"(is|as) an? (independent )?contract(or)? {_ROLE}|contract {_ROLE} (only|with)|"
                rf"\d+[- ]month contract|fixed[- ]term contract|employment type:? contract)\b",
    "contract-to-hire": r"contract[- ]to[- ](hire|perm)",
    "freelance": rf"\b(this|the) (is )?(an? )?freelance {_ROLE}|freelance (basis|contract)|\bfreelance only\b",
    "hourly": r"\b(hourly (rate|wage|pay|position)|paid hourly|per hour\b|/hr\b)",
    "part-time": r"\bpart[- ]time (position|role|employee)\b",
    "temporary": r"\btemporary (position|role|assignment|employee)\b",
    "intern": r"\binternship\b",
}
TITLE_EMPLOYMENT_REJECT = re.compile(r"\b(contract(or)?|freelance|temp(orary)?|intern(ship)?|part[- ]time|1099)\b", re.I)


def rx_any(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


class Scorer:
    def __init__(self, crit: dict, sizes: dict):
        self.c = crit
        self.hf = crit["hard_filters"]
        self.sc = crit["scoring"]
        self.unk = crit["unknown_handling"]
        self.neutral = self.unk.get("neutral_credit", 0.4)
        cs = crit["creative_signals"]
        self.cs = cs
        self.rx_core_title = rx_any([rf"\b{p}" for p in cs["core_title"]])
        self.rx_strong = rx_any([rf"\b{p}" for p in cs["strong"]])
        self.rx_weak = rx_any([rf"\b{p}" for p in cs["weak"]])
        self.rx_ben = {k: rx_any(v) for k, v in crit["benefits_keywords"].items()}
        self.rx_bonus = {k: rx_any(v) for k, v in crit["bonus_keywords"].items()}
        wm = crit["work_mode_keywords"]
        self.rx_remote = rx_any(wm["remote"])
        self.rx_hybrid = rx_any(wm["hybrid"])
        self.sizes = sizes
        ov = ROOT / "mode_overrides.yaml"
        ovd = (yaml.safe_load(ov.read_text()) or {}) if ov.exists() else {}
        self.company_modes = ovd.get("company_default_mode", {})
        self.job_facts = ovd.get("job_facts", {})
        hl = self.hf["hybrid_location"]
        self.chi_area = hl["area"]
        self.chi_terms = [t.lower() for t in hl["match"]]
        reject_terms = [str(t).lower() for t in self.hf["employment"]["reject_terms"]]
        self.rx_meta_reject = re.compile(
            r"contract|freelanc|intern|part|temp|short term|seasonal|1099|hourly|casual", re.I) if reject_terms else None
        self.desc_reject = {k: re.compile(v, re.I) for k, v in DESC_EMPLOYMENT_REJECT.items() if k in reject_terms}

    # ---- individual checks -------------------------------------------------------------
    def salary(self, job, text):
        """-> (low, high, source, note). Annualised USD. Raises Reject for hourly pay."""
        usd = []
        other = []
        for r in job.get("salary") or []:
            lo, hi = r.get("min"), r.get("max")
            if lo is None and hi is None:
                continue
            lo = lo if lo is not None else hi
            hi = hi if hi is not None else lo
            iv = r.get("interval", "year")
            mult = {"year": 1, "month": 12, "week": 52, "hour": 2080}.get(iv, 1)
            (usd if (r.get("currency") or "USD").upper() == "USD" else other).append(
                (lo * mult, hi * mult, iv, r.get("currency")))
        if usd:
            if all(u[2] == "hour" for u in usd):
                return None, None, "structured", "hourly"
            usd = [u for u in usd if u[2] != "hour"] or usd
            lo = min(u[0] for u in usd)
            hi = max(u[1] for u in usd)
            return lo, hi, "structured", None
        for m in SALARY_RX.finditer(job.get("description") or ""):
            vals = []
            for g in m.groups():
                g = g.replace(",", "").replace(" ", "")
                v = float(g[:-1]) * 1000 if g[-1] in "kK" else float(g)
                vals.append(v)
            if all(30000 <= v <= 1500000 for v in vals) and vals[0] <= vals[1]:
                return vals[0], vals[1], "text", None
        if other:
            cur = other[0][3]
            return None, None, "structured", f"salary posted in {cur}"
        return None, None, None, None

    def work_mode(self, job):
        """-> (mode, source). mode in remote|hybrid|onsite|unknown."""
        wp = (job.get("workplace_type") or "").lower()
        loc = (job.get("location") or "").lower()
        if wp:
            if "hybrid" in wp:
                return "hybrid", "listing"
            if "remote" in wp:
                return "remote", "listing"
            if re.search(r"on[- ]?site|in[- ]office|office", wp):
                return "onsite", "listing"
        # An explicit in-office requirement in the text outranks a "Remote, XX" label in the location field
        # (e.g. Pinterest: "Remote, CA" but "in the office 1-2 times per week ... commutable distance from Los Angeles").
        desc0 = (job.get("description") or "").lower()
        if re.search(r"commutable distance|in the office (for in-person collaboration )?\d(?:\s?-\s?\d)? times? (a|per) week|"
                     r"in[- ]person collaboration \d", desc0):
            return "hybrid", "text"
        if re.search(r"\bremote\b", loc):
            return "remote", "location"
        if "hybrid" in loc:
            return "hybrid", "location"
        desc = (job.get("description") or "").lower()
        if self.rx_hybrid.search(desc):
            return "hybrid", "text"
        if re.search(r"(this|the) (role|position|job) is (a )?(fully )?(remote|work[- ]from[- ]home)|\bfully remote\b|"
                     r"\bremote[- ](first|eligible|friendly)\b|\bremote (role|position|opportunity)\b|"
                     r"(located|based|work) remotely|\bwork remotely\b|\bremote within\b", desc):
            return "remote", "text"
        if re.search(r"(this|the) (role|position|job) is (an? )?(fully )?(on-?site|in-office|in office|office-based)|"
                     r"(five|5) days (a|per) week (in|at) (the|our) office|(requires?|must) (be )?(work(ing)? )?on-?site",
                     desc):
            return "onsite", "text"
        cm = self.company_modes.get(job["company"])
        if cm:
            return cm["mode"], "company policy"
        return "unknown", None

    def creative(self, job):
        """-> (passes, fraction, evidence_list, title_hit)"""
        title = job["title"]
        desc = (job.get("description") or "")
        title_hit = bool(self.rx_core_title.search(title))
        strong = [m.group(0).lower() for m in self.rx_strong.finditer(desc)]
        weak = [m.group(0).lower() for m in self.rx_weak.finditer(desc)]
        distinct = sorted(set(strong))
        mentions = len(strong)
        evid = [f"{t}×{strong.count(t)}" for t in sorted(set(strong), key=lambda t: -strong.count(t))[:4]]
        if title_hit:
            # creative word in the title: still need *some* supporting evidence in the description
            ok = len(distinct) >= 1
            frac = 0.7 + 0.3 * min(1.0, mentions / 8)
            return ok, frac if ok else 0, evid, True
        ok = (len(distinct) >= self.cs["min_strong_distinct"] and mentions >= self.cs["min_strong_mentions"])
        frac = 0.7 * min(1.0, mentions / (2 * self.cs["min_strong_mentions"]))
        return ok, frac, evid, False

    def employment(self, job, text):
        reasons = []
        meta = job.get("employment_type") or ""
        if meta and self.rx_meta_reject and self.rx_meta_reject.search(meta):
            reasons.append(f"employment type '{meta}'")
        if TITLE_EMPLOYMENT_REJECT.search(job["title"]):
            reasons.append("contract/temp/intern in title")
        for k, rx in self.desc_reject.items():
            m = rx.search(job.get("description") or "")
            if m:
                reasons.append(f"description says '{m.group(0).strip()[:30]}'")
        return reasons

    def benefits(self, text):
        parts = self.sc["benefits"]["parts"]
        total = self.sc["benefits"]["points"]
        got, missing, pts = [], [], 0.0
        hit = {k: bool(self.rx_ben[k].search(text)) for k in ("hsa_fsa", "dental", "vision", "medical")}
        strong_med = bool(self.rx_ben["strong_medical"].search(text))
        for k, p in parts.items():
            if hit[k]:
                if k == "medical":
                    pts += p if strong_med else p * 0.7
                else:
                    pts += p
                got.append(k)
            else:
                pts += p * self.neutral
                missing.append(k)
        listed = any(hit.values())
        return pts, got, missing, listed

    # ---- evaluate one job --------------------------------------------------------------
    def evaluate(self, job):
        facts = self.job_facts.get(job["key"])
        if facts:   # details verified by hand from the live posting page; the job-board API omitted them
            job = dict(job)
            if "salary" in facts:
                job["salary"] = [{"min": facts["salary"][0], "max": facts["salary"][1], "currency": "USD",
                                  "interval": "year", "label": "verified"}]
            if "workplace_type" in facts:
                job["workplace_type"] = facts["workplace_type"]
            if "location_extra" in facts:
                job["location"] = (job.get("location") or "") + "; " + facts["location_extra"]
        text = f"{job['title']}\n{job.get('description') or ''}"
        res = {"key": job["key"], "company": job["company"], "title": job["title"], "url": job.get("url"),
               "location": job.get("location") or "", "title_rank": job.get("title_rank", 99),
               "first_seen": job.get("first_seen"), "reject": [], "verify": [], "breakdown": {}}
        rej, ver = res["reject"], res["verify"]

        # 1. company size
        sz = self.sizes.get(job["company"])
        if sz is None:
            ver.append("company size unknown")
        elif sz["employees"] < self.hf["company_size_min"]:
            rej.append(f"company ~{sz['employees']} employees (< {self.hf['company_size_min']}, estimate)")

        # 2. employment
        rej += [f"not W2 full-time: {r}" for r in self.employment(job, text)]

        # 3. work mode
        mode, msrc = self.work_mode(job)
        res["work_mode"] = mode
        if mode == "onsite":
            rej.append("on-site only")
        loc_l = (job.get("location") or "").lower()
        in_chicagoland = any(t in loc_l for t in self.chi_terms)
        res["chicagoland"] = in_chicagoland
        if mode == "hybrid" and not in_chicagoland:
            rej.append(f"hybrid but outside {self.chi_area} ({(job.get('location') or 'no location')[:40]})")
        if mode == "unknown" and not in_chicagoland and OFFICE_BASED.search(job.get("description") or ""):
            rej.append(f"office-based outside {self.chi_area} (posting requires working from / relocating to the office: "
                       f"{(job.get('location') or '')[:30]})")
        elif mode == "unknown" and not in_chicagoland:
            ver.append(f"only viable if fully remote: if it is hybrid/on-site it is outside {self.chi_area} and gets dropped")
        if mode == "unknown":
            if OFFICE_BASED.search(job.get("description") or ""):
                ver.append("work mode not stated; posting implies office-based (relocation / 'work from the office')")
            else:
                ver.append("work mode not stated")
        elif msrc == "text":
            ver.append(f"work mode ({mode}) inferred from description")
        elif msrc == "company policy":
            ver.append(f"work mode ({mode}) from {job['company']}'s published policy, not the posting (see mode_overrides.yaml)")

        # 4. salary
        lo, hi, ssrc, snote = self.salary(job, text)
        res["salary_low"], res["salary_high"] = lo, hi
        if snote == "hourly":
            rej.append("hourly pay")
        elif lo is None:
            ver.append(snote or "salary not listed")
        elif hi < self.hf["salary_min"]:
            rej.append(f"salary tops out at ${hi:,.0f} (< ${self.hf['salary_min']:,})")
        elif lo < self.hf["salary_min"]:
            ver.append(f"range starts below floor (${lo:,.0f})")
        if lo is not None and GEO_ADJUSTED.search(job.get("description") or ""):
            ver.append("pay band is for high-cost metros; may be lower elsewhere")
        if ssrc == "text":
            ver.append("salary parsed from description text")

        # 5. creative
        ok, frac, evid, title_hit = self.creative(job)
        res["creative_evidence"] = evid
        if self.hf["creative_component_required"] and not ok:
            rej.append("no creative component" + (f" (weak evidence: {', '.join(evid)})" if evid else ""))

        # 6. location sanity (flag only)
        loc = (job.get("location") or "")
        country = (job.get("country") or "")
        loc_l = loc.lower()
        if (any(re.search(rf"\b{re.escape(n)}\b", loc_l) for n in NON_US) or (country and country.upper() not in ("US", "USA", "UNITED STATES"))) \
                and not re.search(r"\b(united states|usa)\b|\bus\b|, [A-Z]{2}\b", loc):
            ver.append(f"non-US location ({loc[:30]})")

        # ---- score ----
        s = self.sc
        bd = res["breakdown"]
        bd["creative"] = round(s["creative"]["points"] * frac, 1) if ok else 0.0
        wm_pts = s["work_mode"]["points"]
        bd["work_mode"] = round(wm_pts * {"remote": s["work_mode"]["remote"], "hybrid": s["work_mode"]["hybrid"]}.get(mode, 0), 1)
        ben_pts, got, missing, listed = self.benefits(text)
        bd["benefits"] = round(ben_pts, 1)
        if not listed:
            ver.append("benefits not listed")
        elif missing:
            ver.append("benefits unconfirmed: " + "/".join(m.replace("_", " ").replace("hsa fsa", "HSA/FSA") for m in missing))
        res["benefits_got"] = got
        sp = s["salary_headroom"]
        if lo is None:
            bd["salary"] = round(sp["points"] * self.neutral, 1)
        else:
            mid = (lo + hi) / 2
            span = sp["full_points_at"] - self.hf["salary_min"]
            bd["salary"] = round(sp["points"] * max(0.0, min(1.0, (mid - self.hf["salary_min"]) / span)), 1)
        bp = s["bonuses"]["parts"]
        bonus_hit = [k for k in bp if self.rx_bonus[k].search(text)]
        if mode == "remote" and "location_flexibility" not in bonus_hit and re.search(r"\bremote\b.{0,40}\b(anywhere|any state|nationwide)\b", text, re.I):
            bonus_hit.append("location_flexibility")
        bd["bonuses"] = float(sum(bp[k] for k in bonus_hit))
        res["bonus_hits"] = bonus_hit
        res["score"] = round(sum(bd.values()), 1)
        res["status"] = "reject" if rej else "pass"
        if facts:
            ver.append(f"details verified on the live posting page {facts.get('checked', '')}: {facts.get('note', '')}".strip())
        res["reason"] = self.reason_line(res, mode, lo, hi, evid, bonus_hit, got)
        return res

    @staticmethod
    def reason_line(res, mode, lo, hi, evid, bonus_hit, got):
        bits = [{"remote": "Remote", "hybrid": "Hybrid", "unknown": "Work mode ?"}.get(mode, mode)]
        if lo is not None:
            bits.append(f"${lo/1000:.0f}–{hi/1000:.0f}k" if hi != lo else f"${lo/1000:.0f}k")
        if evid:
            bits.append("creative: " + ", ".join(evid[:3]))
        if got:
            bits.append("benefits: " + ", ".join(g.replace("hsa_fsa", "HSA/FSA") for g in got))
        if bonus_hit:
            bits.append("bonus: " + ", ".join(b.replace("_", " ") for b in bonus_hit))
        return " · ".join(bits)


# ---------------------------------------------------------------- output
def money(lo, hi):
    if lo is None:
        return "salary not listed"
    return f"${lo:,.0f}–${hi:,.0f}" if hi != lo else f"${lo:,.0f}"


def write_shortlist(passes, rejects, today, n_open_total):
    L = [f"# Job shortlist — {today}", "",
         f"**{len(passes)}** jobs passed all hard filters (of {n_open_total} title-matched open jobs; "
         f"{len(rejects)} auto-rejected — see [rejected.md](rejected.md)). Ranked by score out of 100.",
         "⚠ **verify** = data missing or inferred; check before relying on it. Nothing here has been applied to.", ""]
    if not passes:
        L.append("_No jobs passed the hard filters today._")
    for i, r in enumerate(passes, 1):
        new = " 🆕" if r.get("first_seen") == today else ""
        L.append(f"{i}. **{r['score']:.0f}** — [{r['title']}]({r['url']}) — {r['company']}{new}")
        L.append(f"   - {r['location'][:70]} · {money(r['salary_low'], r['salary_high'])} · title priority #{r['title_rank'] if r['title_rank'] != 99 else '—'}")
        L.append(f"   - {r['reason']}")
        b = r["breakdown"]
        L.append(f"   - points: creative {b['creative']:.0f}/35 · work mode {b['work_mode']:.0f}/25 · benefits {b['benefits']:.0f}/20 · "
                 f"salary {b['salary']:.0f}/10 · bonuses {b['bonuses']:.0f}/10")
        if r["verify"]:
            L.append("   - ⚠ **verify:** " + "; ".join(r["verify"]))
        L.append("")
    (ROOT / "shortlist.md").write_text("\n".join(L))

    R = [f"# Rejected jobs — {today}", "", "Hard-filter failures, for spot-checking. Company sizes are estimates "
         "(company_sizes.yaml).", ""]
    for r in sorted(rejects, key=lambda r: (r["company"], r["title"])):
        R.append(f"- **{r['company']}** — [{r['title']}]({r['url']}) ({r['location'][:40]})")
        for why in r["reject"]:
            R.append(f"  - ✗ {why}")
    (ROOT / "rejected.md").write_text("\n".join(R) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0, help="print the top N to the terminal")
    args = ap.parse_args()
    today = date.today().isoformat()

    crit = yaml.safe_load((ROOT / "criteria.yaml").read_text())
    sizes = yaml.safe_load((ROOT / "company_sizes.yaml").read_text())
    scorer = Scorer(crit, sizes)

    store_path, cand_path, seen_path = ROOT / "scored.json", ROOT / "candidates.json", ROOT / "seen.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else {"jobs": {}}
    cands = json.loads(cand_path.read_text()) if cand_path.exists() else []
    for c in cands:
        store["jobs"][c["key"]] = c
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {"meta": {}, "jobs": {}}
    meta = seen.get("meta", {})

    def is_open(key, job):
        s = seen["jobs"].get(key)
        if not s:
            return True
        return s["last_seen"] == meta.get("last_run") or job["company"] in meta.get("failed_boards", [])

    results = [scorer.evaluate(j) for k, j in store["jobs"].items() if is_open(k, j)]
    passes = sorted((r for r in results if r["status"] == "pass"),
                    key=lambda r: (-r["score"], r["title_rank"], -(r["salary_high"] or 0)))
    rejects = [r for r in results if r["status"] == "reject"]

    store["results"] = {r["key"]: {k: r[k] for k in ("status", "score", "reject", "verify")} for r in results}
    # drop jobs no longer open from the store so it doesn't grow forever
    store["jobs"] = {k: j for k, j in store["jobs"].items() if is_open(k, j)}
    store_path.write_text(json.dumps(store, indent=1, ensure_ascii=False))
    cand_path.write_text("[]")

    write_shortlist(passes, rejects, today, len(results))
    print(f"{len(results)} open title-matched jobs: {len(passes)} passed, {len(rejects)} rejected "
          f"-> shortlist.md, rejected.md")
    for i, r in enumerate(passes[:args.top], 1):
        v = f"  ⚠ {'; '.join(r['verify'])}" if r["verify"] else ""
        print(f"{i:>2}. {r['score']:5.1f}  {r['company']:<12} {r['title'][:55]:<55} {r['reason']}{v}")


if __name__ == "__main__":
    main()
