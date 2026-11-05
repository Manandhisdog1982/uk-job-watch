"""SWJ Search Job Watcher, version 2.

Runs each weekday morning on GitHub Actions. Collects job ads from Indeed UK
(python-jobspy) and Reed (official API, direct employers only), then:

  keeps only ads with a salary
  removes agency ads and very large employers
  extracts a postcode from the ad, or looks up the town's central postcode
  scores each ad: the sweet spot is an ad that has been live 10 to 21 days
  writes the best N companies (one ad each) for Claude to load into the CRM

Outputs in data/:
  leads.csv            today's shortlist, one row per company
  latest_jobs.csv      every ad that passed the filters
  history.csv          first and last seen dates per ad, for repost detection
  towns.json           cached town to postcode lookups
  run_log.txt          what each source returned
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
CONFIG = json.loads((ROOT / "config.json").read_text())

POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)(?:\s*([0-9][A-Z]{2}))?\b")


def log(msg):
    line = f"{datetime.utcnow():%H:%M:%S} {msg}"
    print(line)
    LOG.append(line)


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()).strip()


AGENCY_NAMES = [norm(a) for a in CONFIG["agency_names"]]
AGENCY_WORDS = [a.lower() for a in CONFIG["agency_keywords"]]
BIG_NAMES = [b.lower() for b in CONFIG["big_employer_names"]]
TARGETS = [norm(t) for t in CONFIG["target_companies"]]


# ---------- classification ----------

def is_agency(company, industry="", description=""):
    c = norm(company)
    if not c:
        return True
    if any(c == a or c.startswith(a + " ") for a in AGENCY_NAMES):
        return True
    if any(w in c for w in AGENCY_WORDS):
        return True
    ind = str(industry or "").lower()
    if any(w in ind for w in ("staffing", "recruit", "employment agenc")):
        return True
    d = str(description or "").lower()[:1500]
    if any(p in d for p in ("our client", "on behalf of our client", "my client",
                            "the client is", "are recruiting on behalf")):
        return True
    return False


def is_big(company):
    c = " " + str(company or "").lower() + " "
    return any(b.lower() in c for b in BIG_NAMES)


def is_target(company):
    c = norm(company)
    return any(t and (t in c or c in t) for t in TARGETS)


def annual(amount, interval):
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:
        return None
    i = str(interval or "yearly").lower()
    if i.startswith("hour"):
        return v * 37.5 * 52
    if i.startswith("day") or i.startswith("dai"):
        return v * 5 * 52
    if i.startswith("week"):
        return v * 52
    if i.startswith("month"):
        return v * 12
    return v


def salary_text(lo, hi, interval=None):
    unit = ""
    i = str(interval or "yearly").lower()
    if i.startswith("hour"):
        unit = " an hour"
    elif i.startswith("day") or i.startswith("dai"):
        unit = " a day"

    def fmt(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        if v != v or v <= 0:
            return ""
        return f"£{v:,.2f}" if unit == " an hour" else f"£{v:,.0f}"

    lo, hi = fmt(lo), fmt(hi)
    if lo and hi and lo != hi:
        return f"{lo} to {hi}{unit}"
    return (lo or hi) + unit if (lo or hi) else ""


# ---------- postcodes ----------

TOWNS_PATH = DATA / "towns.json"
TOWNS = json.loads(TOWNS_PATH.read_text()) if TOWNS_PATH.exists() else {}


def split_location(location):
    """Return (town, postcode, postcode_type) from a job board location string."""
    loc = str(location or "").replace("Hybrid work in ", "").replace("Remote in ", "")
    loc = loc.split(",")[0].strip()
    m = POSTCODE_RE.search(loc.upper())
    if m and not loc.upper().startswith(m.group(0)):  # avoid matching town names
        pc = (m.group(1) + (" " + m.group(2) if m.group(2) else "")).strip()
        town = loc[: m.start()].strip(" -")
        return town or loc, pc, "from advert" if m.group(2) else "area from advert"
    return loc, "", ""


def town_postcode(town):
    """Central postcode for a town via postcodes.io (free, cached)."""
    key = norm(town)
    if not key:
        return ""
    if key in TOWNS:
        return TOWNS[key]
    pc = ""
    try:
        r = requests.get("https://api.postcodes.io/places", params={"q": town, "limit": 1}, timeout=10)
        places = r.json().get("result") or []
        if places:
            lat, lon = places[0]["latitude"], places[0]["longitude"]
            r2 = requests.get("https://api.postcodes.io/postcodes",
                              params={"lat": lat, "lon": lon, "limit": 1}, timeout=10)
            res = r2.json().get("result") or []
            if res:
                pc = res[0]["postcode"]
    except Exception as e:
        log(f"postcode lookup failed for {town}: {str(e)[:80]}")
    TOWNS[key] = pc
    return pc


# ---------- sources ----------

def from_indeed(title):
    rows = []
    if "indeed" not in CONFIG["sites"]:
        return rows
    try:
        from jobspy import scrape_jobs
        df = scrape_jobs(
            site_name=["indeed"],
            search_term=f'"{title}"',
            location=CONFIG["location"],
            results_wanted=CONFIG["results_per_search"],
            hours_old=CONFIG["days_old"] * 24,
            country_indeed="UK",
        )
        for _, r in df.iterrows():
            rows.append({
                "company": r.get("company"),
                "title": r.get("title"),
                "location": r.get("location"),
                "site": "indeed",
                "url": r.get("job_url"),
                "date_posted": str(r.get("date_posted") or "")[:10],
                "salary": salary_text(r.get("min_amount"), r.get("max_amount"), r.get("interval")),
                "annual": annual(r.get("min_amount") or r.get("max_amount"), r.get("interval")),
                "industry": r.get("company_industry"),
                "description": r.get("description"),
                "search": title,
            })
        log(f"indeed '{title}': {len(df)}")
    except Exception as e:
        log(f"indeed '{title}' failed: {str(e)[:160]}")
    return rows


def from_reed(title):
    rows = []
    key = os.environ.get("REED_API_KEY")
    if "reed" not in CONFIG["sites"]:
        return rows
    if not key:
        log("reed skipped: no REED_API_KEY secret")
        return rows
    try:
        resp = requests.get(
            "https://www.reed.co.uk/api/1.0/search",
            params={"keywords": f'"{title}"', "resultsToTake": 100,
                    "postedByDirectEmployer": "true", "permanent": "true"},
            auth=(key, ""), timeout=30,
        )
        resp.raise_for_status()
        cutoff = TODAY - timedelta(days=CONFIG["days_old"])
        for j in resp.json().get("results", []):
            try:
                posted = datetime.strptime(j.get("date", ""), "%d/%m/%Y").date()
            except ValueError:
                posted = None
            if posted and posted < cutoff:
                continue
            lo, hi = j.get("minimumSalary"), j.get("maximumSalary")
            interval = "hourly" if (lo and lo < 100) else "yearly"
            rows.append({
                "company": j.get("employerName"),
                "title": j.get("jobTitle"),
                "location": j.get("locationName"),
                "site": "reed",
                "url": j.get("jobUrl"),
                "date_posted": posted.isoformat() if posted else "",
                "salary": salary_text(lo, hi, interval),
                "annual": annual(lo or hi, interval),
                "industry": "",
                "description": j.get("jobDescription"),
                "search": title,
            })
        log(f"reed '{title}': {len(rows)}")
    except Exception as e:
        log(f"reed '{title}' failed: {str(e)[:160]}")
    return rows


# ---------- main ----------

def main():
    rows = []
    for title in CONFIG["job_titles"]:
        rows += from_indeed(title)
        rows += from_reed(title)
        time.sleep(2)

    cols = ["company", "title", "location", "site", "url", "date_posted", "salary",
            "annual", "industry", "description", "search"]
    jobs = pd.DataFrame(rows, columns=cols)
    n = {"raw": len(jobs)}
    jobs = jobs.dropna(subset=["company", "title"])

    # salary rule
    if CONFIG.get("require_salary"):
        jobs = jobs[jobs["salary"].fillna("") != ""]
    floor = CONFIG.get("min_annual_salary") or 0
    if floor:
        jobs = jobs[jobs["annual"].fillna(0) >= floor]
    n["with_salary"] = len(jobs)

    # agency and big employer rules
    jobs = jobs[~jobs.apply(lambda r: is_agency(r.company, r.industry, r.description), axis=1)]
    n["not_agency"] = len(jobs)
    jobs = jobs[~jobs["company"].apply(is_big)]
    n["not_big"] = len(jobs)

    # location, postcode, dedupe
    parts = jobs["location"].apply(split_location)
    jobs["town"] = [p[0] for p in parts]
    jobs["postcode"] = [p[1] for p in parts]
    jobs["postcode_type"] = [p[2] for p in parts]
    jobs["key"] = (jobs["company"].map(norm) + "|" + jobs["title"].map(norm)
                   + "|" + jobs["town"].map(norm))
    jobs = jobs.drop_duplicates("key")
    n["deduped"] = len(jobs)

    # drop employers with too many ads in one sweep (another "big" signal)
    counts = jobs.groupby(jobs["company"].map(norm))["key"].transform("count")
    jobs["roles_open"] = counts
    jobs = jobs[counts <= CONFIG["max_ads_per_company"]]
    n["not_mass_hiring"] = len(jobs)
    log("funnel " + ", ".join(f"{k} {v}" for k, v in n.items()))

    # history and repost detection
    hist_path = DATA / "history.csv"
    hist = pd.read_csv(hist_path) if hist_path.exists() else pd.DataFrame(
        columns=["key", "company", "title", "first_seen", "last_seen"])
    first_seen = dict(zip(hist["key"], hist["first_seen"]))

    def age_days(r):
        d = r.date_posted or first_seen.get(r.key, TODAY.isoformat())
        try:
            return (TODAY - date.fromisoformat(str(d)[:10])).days
        except ValueError:
            return 0

    jobs["age_days"] = jobs.apply(age_days, axis=1)
    jobs["repost"] = jobs["key"].map(
        lambda k: k in first_seen and str(first_seen[k]) <= (TODAY - timedelta(days=28)).isoformat())
    jobs["target"] = jobs["company"].apply(is_target)

    lo, hi = CONFIG["sweet_spot_days"]

    def score(r):
        s = 10
        if lo <= r.age_days <= hi:
            s += 30                      # live long enough that they probably need help
        elif r.age_days > hi:
            s += 15
        elif r.age_days >= 5:
            s += 10
        if r.repost: s += 20
        if r.target: s += 40
        if 2 <= r.roles_open <= 4: s += 10
        if r.postcode_type == "from advert": s += 3
        if r.site == "reed": s += 2       # Reed rows are confirmed direct employers
        return s

    jobs["score"] = jobs.apply(score, axis=1)

    def flags(r):
        f = []
        if lo <= r.age_days <= hi: f.append(f"Live {r.age_days} days")
        elif r.age_days > hi: f.append(f"Live {r.age_days} days, older")
        else: f.append(f"Live {r.age_days} days, fresh")
        if r.repost: f.append("Repost")
        if r.target: f.append("Target company")
        if r.roles_open >= 2: f.append(f"{r.roles_open} roles open")
        return "; ".join(f)

    jobs["flags"] = jobs.apply(flags, axis=1)
    jobs = jobs.sort_values(["score", "age_days"], ascending=[False, False])

    # shortlist: one ad per company, fill postcodes only for these (saves lookups)
    jobs["company_key"] = jobs["company"].map(norm)
    leads = jobs.drop_duplicates("company_key").head(CONFIG["daily_leads"]).copy()
    filled = []
    for _, r in leads.iterrows():
        if r.postcode and r.postcode_type == "from advert":
            filled.append((r.postcode, r.postcode_type))
        else:
            pc = town_postcode(r.town)
            filled.append((pc or r.postcode, "town centre, approximate" if pc else r.postcode_type))
    leads["postcode"] = [f[0] for f in filled]
    leads["postcode_type"] = [f[1] for f in filled]
    TOWNS_PATH.write_text(json.dumps(TOWNS, indent=0, sort_keys=True))

    out = ["company", "title", "town", "postcode", "postcode_type", "salary", "age_days",
           "date_posted", "site", "url", "flags", "score", "search", "key"]
    leads = leads[out]
    leads.insert(0, "run_date", TODAY.isoformat())
    leads.to_csv(DATA / "leads.csv", index=False)

    jobs_out = jobs[[c for c in out if c in jobs.columns]]
    jobs_out.insert(0, "run_date", TODAY.isoformat())
    jobs_out.to_csv(DATA / "latest_jobs.csv", index=False)

    new = jobs[["key", "company", "title"]].copy()
    new["first_seen"] = new["key"].map(lambda k: first_seen.get(k, TODAY.isoformat()))
    new["last_seen"] = TODAY.isoformat()
    hist = pd.concat([hist[~hist["key"].isin(new["key"])], new])
    hist = hist[hist["last_seen"] >= (TODAY - timedelta(days=120)).isoformat()]
    hist.to_csv(hist_path, index=False)

    log(f"leads {len(leads)}, jobs kept {len(jobs)}, towns cached {len(TOWNS)}")
    (DATA / "run_log.txt").write_text("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
