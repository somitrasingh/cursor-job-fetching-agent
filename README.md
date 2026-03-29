# Daily AI/ML job agent → Excel

Python script plus GitHub Actions workflow that runs **once per day**, queries a job API for AI/ML and entry-oriented roles, **filters** to listings posted within a recent window (default **48 hours**), optionally **scores relevance** and **suggests resume tweaks** with OpenAI, **deduplicates** by application URL, and writes **[data/jobs.xlsx](data/jobs.xlsx)** (sheet `jobs`).

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
- **OpenAI (when `llm.enabled: true`):** `OPENAI_API_KEY` — from [OpenAI API keys](https://platform.openai.com/api-keys).
- **Resume on CI:** Commit **[data/resume.txt](data/resume.txt)** to a **private** repository so the job can read it after checkout. Optional: set **`RESUME_TEXT`** — if non-empty, the workflow overwrites `data/resume.txt` before the fetch step (useful if you do not want the file in git). A **validate** step fails fast when `llm.enabled` is true but resume text or `OPENAI_API_KEY` is missing.

You can define all job-provider secrets; unused ones are ignored for the active `provider`.

## Configuration

Edit [config.yaml](config.yaml):

- **`filters.max_age_hours`** — drop listings older than this many hours (relative to workflow start, UTC). Default `48`.
- **`filters.exclude_if_no_posted_at`** — if `true`, listings without a parseable `posted_at` are dropped.
- **`resume.path`** — plain-text resume (default `data/resume.txt`). If this file is missing or empty, the script tries **`resume.pdf_path`** (default `data/resume.pdf`) and extracts text with **pypdf** (works best on text-based PDFs; scanned images need OCR elsewhere).
- **`llm.enabled`** — set `false` to skip OpenAI calls (no `OPENAI_API_KEY` needed).
- **`llm.model`**, **`llm.max_description_chars`**, **`llm.max_jobs_per_run`**, **`llm.request_delay_seconds`** — control cost, pacing, and how much description text is sent to the model.
- **`llm.description_snippet_max_chars`** — how much description is stored in Excel for your review.
- **`queries`** — each string runs as its own search; results are merged.
- **`adzuna.country`** / **`adzuna.where`** — API path country and optional location.
- **`jsearch.country`**, **`jsearch.location`** — passed to the API (location is also appended to the query text when set).
- **`retention.max_rows`** — cap rows after merge/sort.

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
Either save your resume as **`data/resume.pdf`**, or use plain text:

`copy data\resume.example.txt data\resume.txt` then edit `data\resume.txt`.

set ADZUNA_APP_ID=your_id
set ADZUNA_APP_KEY=your_key
set OPENAI_API_KEY=your_openai_key
python -m src.fetch_jobs
```

On Linux/macOS, use `export` instead of `set` and `cp` instead of `copy`. For JSearch, set `RAPIDAPI_KEY` and `provider: jsearch` in `config.yaml`.

Optional: `CONFIG_PATH` points to a different YAML file. Optional: `RESUME_TEXT` env var instead of `data/resume.txt`.

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
| `description_snippet` | Truncated job description text (when the API provides it) |
| `relevance_score` | 0–100 from the LLM (empty if LLM off or row skipped) |
| `relevance_rationale` | Short explanation of the score |
| `tailored_resume` | Full résumé rewritten for that job, **keeping the same sections and order** as your source résumé (plain text; Excel may truncate past ~32k characters) |
| `resume_tailoring` | Short bullet list of what changed vs your original (quick scan) |

Rows are sorted with **higher relevance first** (when scores exist). Existing workbook rows are **pruned** on each run to the same **max_age_hours** window so the sheet does not accumulate stale postings.

## First run on GitHub

1. Push this project to a **private** repo (recommended).
2. Add Action secrets for your provider and, if `llm.enabled: true`, **`OPENAI_API_KEY`**.
3. **Commit `data/resume.txt`** with your résumé (no longer gitignored), or set **`RESUME_TEXT`** instead.
4. Run **Actions → Daily AI/ML job fetch → Run workflow**.

Until `data/jobs.xlsx` exists, the commit step creates it on first successful run. The **Validate secrets and resume** step prints clear errors if something required is missing.

## Notes

- The LLM is instructed to **mirror your layout** (headings, order, bullet style). Quality depends on how clearly that structure appears in `resume.txt` / extracted PDF text; a **text-based PDF** or `.txt` with obvious section headers works best.
- The LLM returns **suggestions** only; verify accuracy and do not invent experience on your real resume.
- **Cost:** each processed job uses one chat completion; use `max_jobs_per_run` to cap spend.
- **JSearch** descriptions depend on the API payload; if a listing has little text, tailoring quality may be lower.
