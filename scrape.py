"""UK job board watcher, version 3.2.

Runs each weekday morning on GitHub Actions. Collects job ads from Indeed UK
(python-jobspy) and Reed (official API, direct employers only), filters them
down to fillable roles at small and mid sized direct employers, and writes a
CRM ready shortlist (20 Engineering, 20 Office) for the daily CRM load.

All filtering happens here, in Python, so the Claude side only loads rows.

Outputs in data/:
  leads.csv        today's shortlist, one row per company
  leads.json       the same shortlist as ready made CRM lead documents
  latest_jobs.csv  every ad that passed the filters, with scores
  history.csv      first and last seen dates per ad, for repost detection
  emitted.csv      companies already sent to the CRM, so they are not resent
  towns.json       cached town to postcode lookups
  run_log.txt      what each source returned and the funnel
"""

import json
import os
import re
import time
import unicodedata
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

POSTCODE_FULL_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})$")
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")
COMPANY_SUFFIXES = {"ltd", "limited", "plc", "llp", "uk", "group", "the", "co",
                    "company", "inc", "holdings", "international", "gb", "europe"}


def log(msg):
    line = f"{datetime.utcnow():%H:%M:%S} {msg}"
    print(line)
    LOG.append(line)


def norm(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def company_key(name):
    """Company name without Ltd, plc, Group and so on, so boards merge."""
    words = [w for w in norm(name).split() if w not in COMPANY_SUFFIXES]
    return " ".join(words) or norm(name)


def crm_slug(text):
    """Same id rule as the CRM page: lowercase, accents stripped, non alphanumerics to hyphens."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", t))[:120]


AGENCY_NAMES = [norm(a) for a in CONFIG["agency_names"]]
AGENCY_WORDS = [a.lower() for a in CONFIG["agency_keywords"]]
BIG_NAMES = [norm(b) for b in CONFIG["big_employer_names"] if norm(b)]
TARGETS = [norm(t) for t in CONFIG["target_companies"]]
MUST = [m.lower() for m in CONFIG["title_must_contain"]]
TITLE_EXCLUDE = [x.lower() for x in CONFIG["title_exclude"]]
COMPANY_EXCLUDE = [x.lower() for x in CONFIG["exclude_company_words"]]
ENG_SEARCHES = set(CONFIG["engineering_titles"])
INDUSTRY_EXCLUDE = [x.lower() for x in CONFIG.get("industry_exclude", [])]
DESCRIPTION_EXCLUDE = [x.lower() for x in CONFIG.get("description_exclude", [])]
HOME_AREAS = set(CONFIG.get("home_postcode_areas", []))

ENG_WORDS = ("engineer", "technician", "electrician", "fitter", "maintenance", "production",
             "plant", "operations", "site manager", "warehouse", "logistics", "supply chain",
             "quality", "health and safety", "h s", "hse", "facilities", "technical",
             "continuous improvement", "shift manager", "manufacturing", "process",
             "project", "depot", "transport", "workshop", "general manager", "managing director")
OFFICE_WORDS = ("hr ", "human resources", "people", "finance", "financial", "accountant",
                "accounts", "controller", "payroll", "sales", "business development", "account manager",
                "marketing", "content", "ecommerce", "e commerce", "digital", "customer service",
                "office manager", "credit", "commercial")


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
    c = " " + norm(company) + " "
    if " plc " in c:
        return True
    return any(f" {b} " in c for b in BIG_NAMES)


def size_too_big(size_text):
    """Indeed gives a size band for many employers, e.g. '501 to 1,000' or '10,000+'."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(size_text or ""))]
    return bool(nums) and min(nums) > CONFIG["max_company_size"]


def excluded_company(company):
    c = " " + norm(company) + " "
    return any(f" {w.strip()} " in c for w in COMPANY_EXCLUDE)


def _word_re(terms):
    return re.compile(r"\b(?:" + "|".join(re.escape(t.strip()) for t in terms) + r")\b")


MUST_RE = _word_re(MUST)
EXCLUDE_RE = _word_re(TITLE_EXCLUDE)


def title_ok(title):
    t = norm(title)
    return bool(MUST_RE.search(t)) and not EXCLUDE_RE.search(t)


def clean_title(title):
    """Drop location and status tails: 'Head of Finance, Manchester', 'X | Eastbourne', 'Y - Full Time'."""
    t = re.split(r"\s*[|]\s*", str(title or ""))[0]
    t = re.sub(r"\s*\(?[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\)?\s*$", "", t)  # trailing postcode
    t = re.sub(r",\s*[A-Z][A-Za-z .&'-]+$", "", t)
    t = re.sub(r"\s*[-–]\s*(full time|permanent|hybrid|immediate start)[\w /]*$", "", t, flags=re.I)
    return t.strip(" -–")


def bad_company_name(company):
    """Reed sometimes puts the job title or a vague phrase in the employer field."""
    c = norm(company)
    return bool(MUST_RE.search(c)) or any(w in c for w in ("organisation", "confidential", "leading", "client"))


def wrong_sector(industry, description):
    ind = str(industry or "").lower()
    if any(w in ind for w in INDUSTRY_EXCLUDE):
        return True
    d = str(description or "").lower()[:2000]
    return sum(1 for w in DESCRIPTION_EXCLUDE if w in d) >= 2


def track_for(title, search):
    t = " " + norm(title) + " "
    office = any(w in t for w in OFFICE_WORDS)
    eng = any(w in t for w in ENG_WORDS)
    if office:  # "People Operations Manager", "Sales Operations Manager" are office roles
        return "Office"
    if eng:
        return "Engineering"
    return "Engineering" if search in ENG_SEARCHES else "Office"


def is_target(company):
    c = norm(company)
    return any(t and (t in c or c in t) for t in TARGETS)


# ---------- salary ----------

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


def guess_interval(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "yearly"
    if v < 100:
        return "hourly"
    if v < 1000:
        return "daily"
    return "yearly"


def salary_text(lo, hi, interval=None):
    i = str(interval or "yearly").lower()
    unit = " an hour" if i.startswith("hour") else " a day" if i.startswith("da") else ""

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

TOWNS_PATH = DATA / "towns_v3.json"
TOWNS = json.loads(TOWNS_PATH.read_text()) if TOWNS_PATH.exists() else {}


def fmt_postcode(outward, inward):
    return f"{outward} {inward}"


def split_location(location):
    """Return (town, postcode, postcode_type) from a job board location string."""
    full = str(location or "").replace("Hybrid work in ", "").replace("Remote in ", "").strip()
    parts = [p.strip() for p in full.split(",")]
    loc, hint = parts[0], " ".join(parts[1:])
    m = POSTCODE_FULL_RE.match(loc.upper().replace(" ", ""))
    if m:  # the whole location is a postcode (common on Reed)
        return "", fmt_postcode(m.group(1), m.group(2)), "from advert", hint
    m = POSTCODE_RE.search(full.upper())
    if m:  # a postcode anywhere in the string, e.g. "Wigan, WN5 0AB"
        town = loc if m.start() > len(loc) else loc[: m.start()].strip(" -")
        return town, fmt_postcode(m.group(1), m.group(2)), "from advert", hint
    return loc, "", "", hint


def postcode_town(pc):
    """Town for a full postcode via postcodes.io (free, cached)."""
    key = "pc:" + pc.replace(" ", "")
    if key in TOWNS:
        return TOWNS[key]
    town = ""
    try:
        r = requests.get(f"https://api.postcodes.io/postcodes/{pc.replace(' ', '')}", timeout=10)
        res = r.json().get("result") or {}
        town = ""
        for k in ("admin_district", "parish"):
            v = str(res.get(k) or "")
            if v and not v.endswith("unparished area") and not vague_town(v):
                town = v
                break
    except Exception as e:
        log(f"postcode lookup failed for {pc}: {str(e)[:80]}")
    TOWNS[key] = town
    return town


def town_postcode(town, hint=""):
    """Central postcode for a town via postcodes.io. Same named places are settled by the
    advert's county hint, then by place type, then England before the rest (the core market)."""
    key = norm(town)
    if not key:
        return ""
    cache_key = key + ("|" + norm(hint) if hint else "")
    if cache_key in TOWNS:
        return TOWNS[cache_key]
    pc = ""
    rank = {"City": 0, "Town": 1, "Suburban Area": 2, "Village": 3}
    h = norm(hint)

    def order(p):
        where = norm(" ".join(str(p.get(k) or "") for k in ("county_unitary", "district_borough", "region")))
        hit = 0 if (h and (h in where or where and where in h)) else 1
        country = 0 if p.get("country") == "England" else 1
        return (hit, rank.get(p.get("local_type"), 9), country)

    try:
        r = requests.get("https://api.postcodes.io/places", params={"q": town, "limit": 20}, timeout=10)
        places = [p for p in (r.json().get("result") or []) if norm(p.get("name_1")) == key] \
            or (r.json().get("result") or [])
        places.sort(key=order)
        if places:
            lat, lon = places[0]["latitude"], places[0]["longitude"]
            r2 = requests.get("https://api.postcodes.io/postcodes",
                              params={"lat": lat, "lon": lon, "limit": 1}, timeout=10)
            res = r2.json().get("result") or []
            if res:
                pc = res[0]["postcode"]
    except Exception as e:
        log(f"town lookup failed for {town}: {str(e)[:80]}")
    TOWNS[cache_key] = pc
    return pc


VAGUE_RE = re.compile(r"(shire|district|borough|county|region)$|^(north|south|east|west|greater|central) |^(england|scotland|wales|uk|united kingdom|london)$")


def vague_town(town):
    """County, region or district names make poor email subject lines."""
    return bool(VAGUE_RE.search(norm(town)))


def postcode_area(pc):
    m = re.match(r"^([A-Z]{1,2})", str(pc or "").upper())
    return m.group(1) if m else ""


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
            interval = r.get("interval")
            rows.append({
                "company": r.get("company"),
                "title": r.get("title"),
                "location": r.get("location"),
                "site": "indeed",
                "url": r.get("job_url"),
                "date_posted": str(r.get("date_posted") or "")[:10],
                "salary": salary_text(r.get("min_amount"), r.get("max_amount"), interval),
                "annual": annual(r.get("min_amount") or r.get("max_amount"), interval),
                "industry": r.get("company_industry"),
                "size": r.get("company_num_employees"),
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
                    "postedByDirectEmployer": "true", "permanent": "true", "fullTime": "true"},
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
            interval = guess_interval(lo or hi)
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
                "size": "",
                "description": j.get("jobDescription"),
                "search": title,
            })
        log(f"reed '{title}': {len(rows)}")
    except Exception as e:
        log(f"reed '{title}' failed: {str(e)[:160]}")
    return rows


# ---------- scoring ----------

SENIOR = ("director", "head of", "general manager", "operations manager", "plant manager",
          "production manager", "engineering manager", "maintenance manager", "site manager",
          "financial controller", "quality manager", "technical manager", "supply chain manager")
NICHE = ("maintenance", "multi skilled", "electrical", "electrician", "mechanical", "technician",
         "production", "manufacturing", "plant", "engineer")


def score_row(r, sweet):
    lo, hi = sweet
    t = " " + norm(r["title"]) + " "
    s = 0
    if any(w in t for w in SENIOR):
        s += 25
    elif "manager" in t:
        s += 15
    else:
        s += 10
    if any(w in t for w in NICHE):
        s += 10  # core market: engineering and maintenance
    s += min(25, int((r["annual"] or 0) / 3000))  # bigger salary, bigger fee
    if lo <= r["age_days"] <= hi:
        s += 15  # live long enough that they probably need help
    if r["repost"]:
        s += 20
    if r["target"]:
        s += 40
    if 2 <= r["roles_open"] <= 4:
        s += 5
    if r["home"]:
        s += 5
    if r["postcode_type"] == "from advert":
        s += 2
    if r["site"] == "reed":
        s += 2  # confirmed direct employer
    return s


def flags_for(r, sweet):
    f = [f"Live {r['age_days']} days"]
    if r["repost"]:
        f.append("Repost")
    if r["target"]:
        f.append("Target company")
    if r["roles_open"] >= 2:
        f.append(f"{r['roles_open']} roles open")
    size = str(r["size"] or "").strip()
    if size and size.lower() != "nan":
        f.append(f"Size {size}")
    return "; ".join(f)


# ---------- main ----------

def funnel_step(n, name, jobs):
    n[name] = len(jobs)
    return jobs


def main():
    rows = []
    for title in CONFIG["engineering_titles"] + CONFIG["office_titles"]:
        rows += from_indeed(title)
        rows += from_reed(title)
        time.sleep(2)

    cols = ["company", "title", "location", "site", "url", "date_posted", "salary",
            "annual", "industry", "size", "description", "search"]
    jobs = pd.DataFrame(rows, columns=cols)
    n = {}
    jobs = funnel_step(n, "raw", jobs.dropna(subset=["company", "title"]))
    jobs = funnel_step(n, "title_fit", jobs[jobs["title"].apply(title_ok)])

    if CONFIG.get("require_salary"):
        jobs = jobs[jobs["salary"].fillna("") != ""]
    jobs = jobs[~jobs["salary"].fillna("").str.endswith(" a day")]  # day rates are contractors
    jobs = funnel_step(n, "pay_27k_plus", jobs[jobs["annual"].fillna(0) >= CONFIG["min_annual_salary"]])

    jobs = funnel_step(n, "not_agency", jobs[~jobs.apply(
        lambda r: is_agency(r["company"], r["industry"], r["description"]), axis=1)])
    jobs = jobs[~jobs["company"].apply(is_big)]
    jobs = jobs[~jobs["size"].apply(size_too_big)]
    jobs = jobs[~jobs["company"].apply(bad_company_name)]
    jobs = funnel_step(n, "not_big", jobs[~jobs["company"].apply(excluded_company)])
    jobs = funnel_step(n, "right_sector", jobs[~jobs.apply(
        lambda r: wrong_sector(r["industry"], r["description"]), axis=1)])

    parts = jobs["location"].apply(split_location)
    jobs["town"] = [p[0] for p in parts]
    jobs["postcode"] = [p[1] for p in parts]
    jobs["postcode_type"] = [p[2] for p in parts]
    jobs["hint"] = [p[3] for p in parts]
    jobs["title"] = jobs["title"].map(clean_title)
    jobs["company_key"] = jobs["company"].map(company_key)
    jobs["key"] = jobs["company_key"] + "|" + jobs["title"].map(norm) + "|" + jobs["town"].map(norm)
    # same ad on both boards: same title and same pay, or same company and title
    jobs["sig"] = jobs["company_key"].str.split().str[0] + "|" + jobs["title"].map(norm) + "|" + jobs["annual"].fillna(0).round(-2).astype(int).astype(str)
    jobs = jobs.sort_values("site", ascending=False)  # prefer the Reed copy (direct employer, clean link)
    jobs = jobs.drop_duplicates("key").drop_duplicates(["company_key", "title"]).drop_duplicates("sig")
    jobs = funnel_step(n, "deduped", jobs)

    counts = jobs.groupby("company_key")["key"].transform("count")
    jobs["roles_open"] = counts
    jobs = funnel_step(n, "not_mass_hiring", jobs[counts <= CONFIG["max_ads_per_company"]])

    hist_path = DATA / "history.csv"
    hist = pd.read_csv(hist_path) if hist_path.exists() else pd.DataFrame(
        columns=["key", "company", "title", "first_seen", "last_seen"])
    first_seen = dict(zip(hist["key"], hist["first_seen"]))

    def age_days(r):
        d = r["date_posted"] or first_seen.get(r["key"], TODAY.isoformat())
        try:
            return (TODAY - date.fromisoformat(str(d)[:10])).days
        except ValueError:
            return 0

    jobs["age_days"] = jobs.apply(age_days, axis=1)
    jobs = funnel_step(n, "age_ok", jobs[jobs["age_days"] >= CONFIG["min_age_days"]])
    jobs["repost"] = jobs["key"].map(
        lambda k: k in first_seen and str(first_seen[k]) <= (TODAY - timedelta(days=28)).isoformat())
    jobs["target"] = jobs["company"].apply(is_target)
    jobs["home"] = jobs["postcode"].map(lambda p: postcode_area(p) in HOME_AREAS)
    jobs["track"] = [track_for(t, s) for t, s in zip(jobs["title"], jobs["search"])]

    sweet = CONFIG["sweet_spot_days"]
    jobs["score"] = jobs.apply(lambda r: score_row(r, sweet), axis=1)
    jobs["flags"] = jobs.apply(lambda r: flags_for(r, sweet), axis=1)
    jobs = jobs.sort_values(["score", "age_days"], ascending=[False, False])

    # companies already sent to the CRM recently are not sent again
    emitted_path = DATA / "emitted.csv"
    emitted = pd.read_csv(emitted_path) if emitted_path.exists() else pd.DataFrame(
        columns=["company_key", "company", "first_sent", "last_sent"])
    recent_cut = (TODAY - timedelta(days=CONFIG["resurface_after_days"])).isoformat()
    recent = set(emitted.loc[(emitted["last_sent"] >= recent_cut)
                             & (emitted["last_sent"] != TODAY.isoformat()), "company_key"])
    fresh = jobs[~jobs["company_key"].isin(recent)].drop_duplicates("company_key")
    n["new_companies"] = len(fresh)

    # shortlist: 20 Engineering and 20 Office; a short track fills from the other
    split = CONFIG["track_split"]
    picks = []
    for track, want in split.items():
        picks.append(fresh[fresh["track"] == track].head(want))
    leads = pd.concat(picks)
    short = CONFIG["daily_leads"] - len(leads)
    if short > 0:
        leads = pd.concat([leads, fresh[~fresh.index.isin(leads.index)].head(short)])
    leads = leads.sort_values(["track", "score"], ascending=[True, False]).copy()

    # postcodes and towns only for the shortlist (saves lookups)
    towns, pcs, types = [], [], []
    for _, r in leads.iterrows():
        town, pc, ptype = r["town"], r["postcode"], r["postcode_type"]
        if pc and (not town or vague_town(town)):
            town = postcode_town(pc) or town
        if not pc and town:
            pc = town_postcode(town, r["hint"])
            ptype = "town centre, approximate" if pc else ""
        towns.append(town)
        pcs.append(pc)
        types.append(ptype)
    leads["town"], leads["postcode"], leads["postcode_type"] = towns, pcs, types
    TOWNS_PATH.write_text(json.dumps(TOWNS, indent=0, sort_keys=True))

    # CRM ready fields: the lead doc id matches the CRM page's own rule
    leads["doc_id"] = [crm_slug(f"{c}-{t}") for c, t in zip(leads["company"], leads["title"])]
    leads["salary_lower"] = leads["annual"].fillna(0).round(-2).astype(int)
    leads["company"] = leads["company"].str.strip()
    out = ["doc_id", "company", "title", "town", "postcode", "postcode_type", "salary",
           "salary_lower", "track", "age_days", "date_posted", "site", "url", "flags", "score",
           "search", "company_key"]
    leads = leads[out]
    leads.insert(0, "run_date", TODAY.isoformat())
    leads.to_csv(DATA / "leads.csv", index=False)
    docs = []
    for _, r in leads.iterrows():
        note = f"Job Watcher {r['score']}: {r['flags']}" + (f"; postcode {r['postcode']}" if r["postcode"] else "")
        docs.append({"doc_id": r["doc_id"], "data": {
            "company": r["company"], "role": r["title"], "location": r["town"] or r["postcode"],
            "salaryText": r["salary"], "salary": int(r["salary_lower"]), "advertUrl": r["url"],
            "source": "Job Watcher", "status": "New", "contactId": "", "track": r["track"],
            "notes": note, "addedAt": TODAY.isoformat()}})
    (DATA / "leads.json").write_text(json.dumps({"run_date": TODAY.isoformat(), "leads": docs}, indent=1))

    jobs_out = jobs[[c for c in out if c in jobs.columns and c not in ("doc_id", "salary_lower")]]
    jobs_out.insert(0, "run_date", TODAY.isoformat())
    jobs_out.to_csv(DATA / "latest_jobs.csv", index=False)

    sent = leads[["company_key", "company"]].copy()
    sent["first_sent"] = sent["company_key"].map(
        dict(zip(emitted["company_key"], emitted["first_sent"]))).fillna(TODAY.isoformat())
    sent["last_sent"] = TODAY.isoformat()
    emitted = pd.concat([emitted[~emitted["company_key"].isin(sent["company_key"])], sent])
    emitted = emitted[emitted["last_sent"] >= (TODAY - timedelta(days=180)).isoformat()]
    emitted.to_csv(emitted_path, index=False)

    new = jobs[["key", "company", "title"]].copy()
    new["first_seen"] = new["key"].map(lambda k: first_seen.get(k, TODAY.isoformat()))
    new["last_seen"] = TODAY.isoformat()
    hist = pd.concat([hist[~hist["key"].isin(new["key"])], new])
    hist = hist[hist["last_seen"] >= (TODAY - timedelta(days=120)).isoformat()]
    hist.to_csv(hist_path, index=False)

    log("funnel " + ", ".join(f"{k} {v}" for k, v in n.items()))
    by_track = leads["track"].value_counts().to_dict()
    log(f"leads {len(leads)} ({', '.join(f'{k} {v}' for k, v in by_track.items())}), "
        f"jobs kept {len(jobs)}, towns cached {len(TOWNS)}")
    (DATA / "run_log.txt").write_text("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
