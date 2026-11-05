"""SWJ Search Job Watcher.

Runs each weekday morning on GitHub Actions. Collects new job ads from
Indeed UK and LinkedIn (via python-jobspy) and Reed (official API),
removes duplicates and agency ads, flags the best leads and writes:

  data/latest_jobs.csv       every clean job found this run
  data/latest_companies.csv  companies ranked by lead score
  data/history.csv           first and last seen dates, used to spot reposts
  data/run_log.txt           what each source returned, for troubleshooting
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TODAY = date.today()
LOG = []


def log(msg):
    line = f"{datetime.utcnow():%H:%M:%S} {msg}"
    print(line)
    LOG.append(line)


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()).strip()


CONFIG = json.loads((ROOT / "config.json").read_text())
AGENCY_NAMES = [norm(a) for a in CONFIG.get("agency_names", [])]
AGENCY_WORDS = [a.lower() for a in CONFIG.get("agency_keywords", [])]
TARGETS = [norm(t) for t in CONFIG.get("target_companies", [])]


def is_agency(company):
    c = norm(company)
    if not c:
        return True
    if any(c == a or c.startswith(a + " ") for a in AGENCY_NAMES):
        return True
    return any(w in c for w in AGENCY_WORDS)


def is_target(company):
    c = norm(company)
    return any(t and (t in c or c in t) for t in TARGETS)


# ---------- sources ----------

def from_jobspy(title):
    sites = [s for s in CONFIG["sites"] if s in ("indeed", "linkedin")]
    rows = []
    try:
        from jobspy import scrape_jobs
    except Exception as e:  # library missing or broken
        log(f"jobspy import failed: {e}")
        return rows
    for site in sites:
        try:
            df = scrape_jobs(
                site_name=[site],
                search_term=f'"{title}"',
                location=CONFIG["location"],
                results_wanted=CONFIG["results_per_search"],
                hours_old=CONFIG["hours_old"],
                country_indeed="UK",
                linkedin_fetch_description=False,
            )
            for _, r in df.iterrows():
                rows.append({
                    "company": r.get("company"),
                    "title": r.get("title"),
                    "location": r.get("location"),
                    "site": site,
                    "url": r.get("job_url"),
                    "date_posted": str(r.get("date_posted") or ""),
                    "salary": salary_text(r.get("min_amount"), r.get("max_amount")),
                    "search": title,
                })
            log(f"{site} '{title}': {len(df)}")
        except Exception as e:
            log(f"{site} '{title}' failed: {str(e)[:200]}")
        time.sleep(4)
    return rows


def from_reed(title):
    key = os.environ.get("REED_API_KEY")
    if "reed" not in CONFIG["sites"]:
        return []
    if not key:
        log("reed skipped: no REED_API_KEY secret")
        return []
    rows = []
    try:
        resp = requests.get(
            "https://www.reed.co.uk/api/1.0/search",
            params={"keywords": title, "resultsToTake": 100},
            auth=(key, ""),
            timeout=30,
        )
        resp.raise_for_status()
        cutoff = TODAY - timedelta(days=max(1, CONFIG["hours_old"] // 24))
        for j in resp.json().get("results", []):
            try:
                posted = datetime.strptime(j.get("date", ""), "%d/%m/%Y").date()
            except ValueError:
                posted = None
            if posted and posted < cutoff:
                continue
            rows.append({
                "company": j.get("employerName"),
                "title": j.get("jobTitle"),
                "location": j.get("locationName"),
                "site": "reed",
                "url": j.get("jobUrl"),
                "date_posted": posted.isoformat() if posted else "",
                "salary": salary_text(j.get("minimumSalary"), j.get("maximumSalary")),
                "search": title,
            })
        log(f"reed '{title}': {len(rows)}")
    except Exception as e:
        log(f"reed '{title}' failed: {str(e)[:200]}")
    return rows


def salary_text(lo, hi):
    def fmt(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        if v != v or v <= 0:  # NaN or empty
            return ""
        return f"£{v:,.0f}"
    lo, hi = fmt(lo), fmt(hi)
    if lo and hi and lo != hi:
        return f"{lo} to {hi}"
    return lo or hi


# ---------- main ----------

def main():
    rows = []
    for title in CONFIG["job_titles"]:
        rows += from_jobspy(title)
        rows += from_reed(title)

    jobs = pd.DataFrame(rows, columns=[
        "company", "title", "location", "site", "url", "date_posted", "salary", "search"])
    raw = len(jobs)
    jobs = jobs.dropna(subset=["company", "title"])
    jobs = jobs[~jobs["company"].apply(is_agency)]
    after_agency = len(jobs)

    jobs["town"] = jobs["location"].fillna("").str.split(",").str[0].str.strip()
    jobs["key"] = (jobs["company"].map(norm) + "|" + jobs["title"].map(norm)
                   + "|" + jobs["town"].map(norm))
    jobs = jobs.drop_duplicates("key")
    log(f"raw {raw}, after agency filter {after_agency}, after dedupe {len(jobs)}")

    # repost detection from history
    hist_path = DATA / "history.csv"
    hist = pd.read_csv(hist_path) if hist_path.exists() else pd.DataFrame(
        columns=["key", "company", "title", "first_seen", "last_seen"])
    first_seen = dict(zip(hist["key"], hist["first_seen"]))
    repost_cutoff = (TODAY - timedelta(days=14)).isoformat()

    jobs["repost"] = jobs["key"].map(lambda k: str(first_seen.get(k, "9999")) <= repost_cutoff)
    jobs["target"] = jobs["company"].apply(is_target)
    jobs["fresh"] = True  # every run only collects the last 24 hours
    counts = jobs.groupby(jobs["company"].map(norm))["key"].transform("count")
    jobs["roles_open"] = counts
    jobs["multi"] = counts >= 3

    def flags(r):
        f = []
        if r.repost: f.append("Repost")
        if r.target: f.append("Target company")
        if r.multi: f.append("3+ roles open")
        if r.fresh: f.append("Fresh")
        return ", ".join(f)

    jobs["flags"] = jobs.apply(flags, axis=1)

    # company ranking
    comp = jobs.groupby(jobs["company"].map(norm)).agg(
        company=("company", "first"),
        roles_open=("key", "count"),
        titles=("title", lambda s: "; ".join(sorted(set(s))[:6])),
        towns=("town", lambda s: "; ".join(sorted(set(x for x in s if x))[:4])),
        sites=("site", lambda s: ", ".join(sorted(set(s)))),
        repost=("repost", "any"),
        target=("target", "any"),
        example_url=("url", "first"),
    ).reset_index(drop=True)
    comp["score"] = (comp["target"] * 40 + (comp["roles_open"] >= 3) * 25
                     + comp["repost"] * 20 + 10 + comp["roles_open"].clip(upper=6) * 5)
    comp = comp.sort_values(["score", "roles_open"], ascending=False)
    comp.insert(0, "run_date", TODAY.isoformat())

    out_cols = ["company", "title", "town", "location", "site", "url", "date_posted",
                "salary", "flags", "roles_open", "search", "key"]
    jobs = jobs[out_cols]
    jobs.insert(0, "run_date", TODAY.isoformat())
    jobs.to_csv(DATA / "latest_jobs.csv", index=False)
    comp.to_csv(DATA / "latest_companies.csv", index=False)

    # update history
    new = jobs[["key", "company", "title"]].copy()
    new["first_seen"] = new["key"].map(lambda k: first_seen.get(k, TODAY.isoformat()))
    new["last_seen"] = TODAY.isoformat()
    hist = pd.concat([hist[~hist["key"].isin(new["key"])], new])
    hist = hist[hist["last_seen"] >= (TODAY - timedelta(days=90)).isoformat()]
    hist.to_csv(hist_path, index=False)

    log(f"companies {len(comp)}, jobs {len(jobs)}")
    (DATA / "run_log.txt").write_text("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
