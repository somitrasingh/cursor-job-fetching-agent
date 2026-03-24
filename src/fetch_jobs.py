"""
Fetch AI/ML job listings via Adzuna or JSearch (RapidAPI), merge into Excel with deduplication.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests
import yaml

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

COLUMNS = [
    "fetched_at",
    "title",
    "company",
    "location",
    "apply_url",
    "posted_at",
    "source",
    "raw_id",
    "search_query",
]


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_url(url: str | None) -> str:
    if not url or not str(url).strip():
        return ""
    parsed = urlparse(str(url).strip())
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or ""
    normalized = urlunparse(
        (
            (parsed.scheme or "https").lower(),
            netloc,
            path,
            "",
            parsed.query,
            "",
        )
    )
    return normalized


def dedupe_key(row: dict[str, Any]) -> str:
    url = normalize_url(row.get("apply_url") or "")
    if url:
        return f"url:{url}"
    source = row.get("source") or ""
    rid = row.get("raw_id") or ""
    return f"id:{source}:{rid}"


def fetch_adzuna_page(
    *,
    country: str,
    page: int,
    what: str,
    where: str,
    app_id: str,
    app_key: str,
    results_per_page: int,
) -> list[dict[str, Any]]:
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    params: dict[str, str | int] = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": results_per_page,
        "what": what,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("results") or []


def adzuna_result_to_row(
    item: dict[str, Any], *, search_query: str, fetched_at: str
) -> dict[str, Any]:
    loc = item.get("location") or {}
    if isinstance(loc, dict):
        location = loc.get("display_name") or ""
    else:
        location = str(loc)
    company = item.get("company") or {}
    if isinstance(company, dict):
        company_name = company.get("display_name") or ""
    else:
        company_name = str(company)
    return {
        "fetched_at": fetched_at,
        "title": item.get("title") or "",
        "company": company_name,
        "location": location,
        "apply_url": item.get("redirect_url") or "",
        "posted_at": item.get("created") or "",
        "source": "adzuna",
        "raw_id": str(item.get("id") or ""),
        "search_query": search_query,
    }


def fetch_adzuna_all(
    cfg: dict[str, Any],
    queries: list[str],
    *,
    app_id: str,
    app_key: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    a = cfg.get("adzuna") or {}
    country = a.get("country") or "us"
    where = a.get("where") or ""
    results_per_page = int(a.get("results_per_page") or 20)
    max_pages = int(a.get("max_pages_per_query") or 1)
    rows: list[dict[str, Any]] = []
    for what in queries:
        qcount = 0
        for page in range(1, max_pages + 1):
            try:
                results = fetch_adzuna_page(
                    country=country,
                    page=page,
                    what=what,
                    where=where,
                    app_id=app_id,
                    app_key=app_key,
                    results_per_page=results_per_page,
                )
            except requests.RequestException as e:
                LOG.error("Adzuna request failed query=%r page=%s: %s", what, page, e)
                break
            if not results:
                break
            for item in results:
                rows.append(adzuna_result_to_row(item, search_query=what, fetched_at=fetched_at))
            qcount += len(results)
            if len(results) < results_per_page:
                break
        LOG.info("Adzuna query=%r -> %s jobs (across pages)", what, qcount)
    return rows


def fetch_jsearch_page(
    *,
    query: str,
    page: int,
    rapidapi_key: str,
    num_pages: int,
    country: str,
    location: str,
) -> list[dict[str, Any]]:
    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "X-RapidAPI-Key": rapidapi_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    params: dict[str, str | int] = {
        "query": query,
        "page": page,
        "num_pages": num_pages,
    }
    if country:
        params["country"] = country
    if location:
        params["location"] = location
    r = requests.get(url, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("data") or []


def jsearch_result_to_row(
    item: dict[str, Any], *, search_query: str, fetched_at: str
) -> dict[str, Any]:
    parts = [
        item.get("job_city"),
        item.get("job_state"),
        item.get("job_country"),
    ]
    location = ", ".join(str(p) for p in parts if p)
    apply_link = item.get("job_apply_link") or item.get("job_google_link") or ""
    posted = (
        item.get("job_posted_at_datetime_utc")
        or item.get("job_posted_human_readable")
        or ""
    )
    return {
        "fetched_at": fetched_at,
        "title": item.get("job_title") or "",
        "company": item.get("employer_name") or "",
        "location": location,
        "apply_url": apply_link or "",
        "posted_at": str(posted),
        "source": "jsearch",
        "raw_id": str(item.get("job_id") or ""),
        "search_query": search_query,
    }


def fetch_jsearch_all(
    cfg: dict[str, Any],
    queries: list[str],
    *,
    rapidapi_key: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    j = cfg.get("jsearch") or {}
    country = str(j.get("country") or "us")
    location = str(j.get("location") or "")
    num_pages = int(j.get("num_pages") or 1)
    rows: list[dict[str, Any]] = []
    for base in queries:
        qtext = base
        if location:
            qtext = f"{base} {location}".strip()
        qcount = 0
        try:
            items = fetch_jsearch_page(
                query=qtext,
                page=1,
                rapidapi_key=rapidapi_key,
                num_pages=num_pages,
                country=country,
                location=location,
            )
        except requests.RequestException as e:
            LOG.error("JSearch request failed query=%r: %s", qtext, e)
            continue
        for item in items:
            rows.append(jsearch_result_to_row(item, search_query=base, fetched_at=fetched_at))
        qcount = len(items)
        LOG.info("JSearch query=%r -> %s jobs", qtext, qcount)
    return rows


def merge_into_excel(
    new_rows: list[dict[str, Any]],
    excel_path: Path,
    sheet_name: str,
    max_rows: int,
) -> None:
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(new_rows)
    if new_df.empty:
        LOG.warning("No new rows from API; existing file unchanged unless empty.")
    for col in COLUMNS:
        if col not in new_df.columns:
            new_df[col] = ""
    new_df = new_df[COLUMNS]
    new_df["_dedupe"] = [dedupe_key(r) for r in new_df.to_dict("records")]

    if excel_path.exists():
        try:
            old_df = pd.read_excel(excel_path, sheet_name=sheet_name, engine="openpyxl")
        except Exception as e:
            LOG.warning("Could not read existing Excel (%s); overwriting.", e)
            old_df = pd.DataFrame(columns=COLUMNS)
    else:
        old_df = pd.DataFrame(columns=COLUMNS)

    for col in COLUMNS:
        if col not in old_df.columns:
            old_df[col] = ""
    old_df = old_df[COLUMNS]
    old_df["_dedupe"] = [dedupe_key(r) for r in old_df.to_dict("records")]

    combined = pd.concat([new_df, old_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["_dedupe"], keep="first")
    combined = combined.drop(columns=["_dedupe"])
    combined = combined.sort_values(by=["fetched_at", "title"], ascending=[False, True])
    if len(combined) > max_rows:
        combined = combined.iloc[:max_rows].reset_index(drop=True)
        LOG.info("Trimmed to max_rows=%s", max_rows)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        combined.to_excel(writer, sheet_name=sheet_name, index=False)
    LOG.info("Wrote %s rows to %s", len(combined), excel_path)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    config_path = Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG))
    cfg = load_config(config_path)

    provider = (cfg.get("provider") or "adzuna").lower().strip()
    queries = cfg.get("queries") or []
    if not queries:
        LOG.error("config.yaml: no queries defined")
        return 1

    retention = cfg.get("retention") or {}
    max_rows = int(retention.get("max_rows") or 5000)
    excel_cfg = cfg.get("excel") or {}
    excel_path = REPO_ROOT / str(excel_cfg.get("path") or "data/jobs.xlsx")
    sheet_name = str(excel_cfg.get("sheet") or "jobs")

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if provider == "adzuna":
        app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
        app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
        if not app_id or not app_key:
            LOG.error("Set ADZUNA_APP_ID and ADZUNA_APP_KEY for provider=adzuna")
            return 1
        rows = fetch_adzuna_all(cfg, queries, app_id=app_id, app_key=app_key, fetched_at=fetched_at)
    elif provider == "jsearch":
        key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not key:
            LOG.error("Set RAPIDAPI_KEY for provider=jsearch")
            return 1
        rows = fetch_jsearch_all(cfg, queries, rapidapi_key=key, fetched_at=fetched_at)
    else:
        LOG.error("Unknown provider %r (use adzuna or jsearch)", provider)
        return 1

    merge_into_excel(rows, excel_path, sheet_name, max_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
