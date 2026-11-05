# uk-job-watch

Daily UK job board watcher. Runs on GitHub Actions every weekday morning, free,
and leaves a ranked shortlist in `data/` for a CRM load.

## What it does

1. Searches Indeed UK and Reed (direct employers only) for the titles in
   `config.json`, ads 5 to 21 days old, up to 100 per title per board.
2. Keeps only ads whose own title fits (`title_must_contain` minus `title_exclude`),
   with a salary of at least £27,000 (hourly rates converted; day rates dropped).
3. Drops agencies, large employers (name list, Indeed size band over 500, plc),
   public bodies and sectors not covered (`exclude_company_words`).
4. Merges Ltd, Limited and plc variants of the same company across boards, drops
   anyone with more than 4 live ads, and anyone shortlisted in the last 60 days.
5. Scores every ad (seniority, core market, salary, days live, repost, target
   company) and picks 20 Engineering and 20 Office companies, one ad each.
6. Fills in a postcode for the town (or a town for the postcode) for the 40 only.

## Files in data/

| File | What |
|---|---|
| `leads.csv` | Today's shortlist, one row per company |
| `leads.json` | The same shortlist as ready made CRM lead documents |
| `latest_jobs.csv` | Every ad that passed the filters |
| `emitted.csv` | Companies already shortlisted and when (60 day cooldown) |
| `history.csv` | First and last seen per ad, for repost detection |
| `towns_v3.json` | Cached postcode lookups |
| `run_log.txt` | Counts per search and the funnel |

## Tuning

Edit `config.json` and commit. Nothing else changes.

- Too few leads: lower `min_annual_salary`, raise `days_old`, add titles.
- Junk titles getting through: add the word to `title_exclude`.
- A big firm getting through: add it to `big_employer_names`.
- An agency getting through: add it to `agency_names` or `agency_keywords`.
- A sector you do not want: add a word to `exclude_company_words`.

## Running by hand

Actions tab, Job Watcher, Run workflow. About 3 minutes. Results commit to `data/`.

Secret needed: `REED_API_KEY` (Reed developer key).
