# Daily AI/ML job agent → Excel

Python script plus GitHub Actions workflow that runs **once per day**, queries a job API for AI/ML and entry-oriented roles, **deduplicates** by application URL, and writes **[data/jobs.xlsx](data/jobs.xlsx)** (sheet `jobs`).

## Providers

| Provider | Secrets | When to use |
|----------|---------|-------------|
| **Adzuna** (default) | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | [Countries supported by Adzuna](https://developer.adzuna.com/docs/search) (e.g. `us`, `gb`). |
| **JSearch** (RapidAPI) | `RAPIDAPI_KEY` | Broader “aggregator” style results; subscribe to **JSearch** on RapidAPI and use the same key. |

Set `provider` in [config.yaml](config.yaml) to `adzuna` or `jsearch`.

## GitHub Actions secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**

- **Adzuna:** `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` — from [Adzuna API signup](https://developer.adzuna.com/signup).
- **JSearch:** `RAPIDAPI_KEY` — from RapidAPI (JSearch app).

You can define all three; unused secrets are ignored for the active `provider`.

## Configuration

Edit [config.yaml](config.yaml):

- **`queries`** — each string runs as its own search; results are merged.
- **`adzuna.country`** / **`adzuna.where`** — API path country and optional location.
- **`jsearch.country`**, **`jsearch.location`** — passed to the API (location is also appended to the query text when set).
- **`retention.max_rows`** — oldest rows are dropped after merge if over this cap.

## Schedule (UTC)

The workflow [`.github/workflows/daily-jobs.yml`](.github/workflows/daily-jobs.yml) uses:

```yaml
cron: "0 8 * * *"
```

That is **08:00 UTC** every day. GitHub Actions does not support local timezones; change the cron expression to match your preferred UTC time.

## Commit vs artifact

- **Scheduled runs** default to **`COMMIT_EXCEL=true`**: if `data/jobs.xlsx` changes, the workflow commits and pushes it (needs `contents: write`, already set).
- **Manual run** (**Actions → Daily AI/ML job fetch → Run workflow**): toggle **commit_excel** to skip the commit and only keep the run’s **artifact** (`jobs-xlsx`), which expires per GitHub’s artifact retention policy.

Use a **private** repository if the workbook should not be public.

## Local run

From the repo root:

```bash
pip install -r requirements.txt
set ADZUNA_APP_ID=your_id
set ADZUNA_APP_KEY=your_key
python -m src.fetch_jobs
```

On Linux/macOS, use `export` instead of `set`. For JSearch, set `RAPIDAPI_KEY` and `provider: jsearch` in `config.yaml`.

Optional: `CONFIG_PATH` points to a different YAML file.

## Excel columns

| Column | Meaning |
|--------|---------|
| `fetched_at` | UTC time when this row was ingested |
| `title` | Job title |
| `company` | Employer name |
| `location` | Location string when the API provides it |
| `apply_url` | Application or listing link |
| `posted_at` | Post/creation time from the provider when available |
| `source` | `adzuna` or `jsearch` |
| `raw_id` | Provider job id |
| `search_query` | Which config query produced this row |

## First run on GitHub

Until `data/jobs.xlsx` exists, the commit step adds the new file. If API credentials are missing, the job fails and no file is produced—add secrets and re-run the workflow.
