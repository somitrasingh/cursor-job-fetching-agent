"""
Fetch AI/ML job listings via Adzuna or JSearch (RapidAPI), merge into Excel with deduplication.
Optionally filter to recent postings, then score relevance and suggest resume tweaks via OpenAI.
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

from src.resume_llm import (
    enrich_rows_with_llm,
    filter_dataframe_by_recency,
    filter_rows_by_recency,
    strip_html,
)

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"
EXCEL_MAX_CELL = 32767

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
    "description_snippet",
    "relevance_score",
    "relevance_rationale",
    "tailored_resume",
    "resume_tailoring",
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


def truncate_cell(text: str, max_len: int = EXCEL_MAX_CELL) -> str:
    if not text:
        return ""
    s = str(text)
    if len(s) <= max_len:
        return s
    return s[: max_len - 24] + "\n...[truncated]"


def extract_text_from_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if getattr(reader, "is_encrypted", False) and reader.decrypt("") == 0:
        raise ValueError(f"PDF is password-protected: {path}")
    chunks: list[str] = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks).strip()


def load_resume_text(cfg: dict[str, Any]) -> str:
    inline = os.environ.get("RESUME_TEXT", "").strip()
    if inline:
        return inline
    resume_cfg = cfg.get("resume") or {}
    txt_rel = str(resume_cfg.get("path") or "data/resume.txt")
    pdf_rel = str(resume_cfg.get("pdf_path") or "data/resume.pdf")
    txt_path = REPO_ROOT / txt_rel
    pdf_path = REPO_ROOT / pdf_rel

    if txt_path.is_file():
        text = txt_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text

    if pdf_path.is_file():
        text = extract_text_from_pdf(pdf_path)
        if text:
            return text
        LOG.warning("PDF had no extractable text: %s", pdf_path)

    raise FileNotFoundError(
        f"No usable resume: add non-empty {txt_path}, or a text-based {pdf_path}, "
        "or set RESUME_TEXT env / secret."
    )


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
    desc = strip_html(str(item.get("description") or ""))
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
        "description_snippet": "",
        "relevance_score": "",
        "relevance_rationale": "",
        "tailored_resume": "",
        "resume_tailoring": "",
        "_desc_for_llm": desc,
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
    raw_desc = item.get("job_description") or ""
    if not raw_desc:
        h = item.get("job_highlights")
        if isinstance(h, list):
            raw_desc = " ".join(str(x) for x in h)
        elif h:
            raw_desc = str(h)
    desc = strip_html(str(raw_desc))
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
        "description_snippet": "",
        "relevance_score": "",
        "relevance_rationale": "",
        "tailored_resume": "",
        "resume_tailoring": "",
        "_desc_for_llm": desc,
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


def finalize_rows_for_excel(
    rows: list[dict[str, Any]],
    *,
    snippet_max: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        body = row.pop("_desc_for_llm", "") or ""
        row["description_snippet"] = body[:snippet_max]
        for k in list(row.keys()):
            if k.startswith("_"):
                row.pop(k, None)
        for col in COLUMNS:
            if col not in row:
                row[col] = ""
        for text_col in (
            "relevance_rationale",
            "tailored_resume",
            "resume_tailoring",
            "description_snippet",
        ):
            if text_col in row and row[text_col]:
                row[text_col] = truncate_cell(str(row[text_col]))
        out.append({c: row.get(c, "") for c in COLUMNS})
    return out


def merge_into_excel(
    new_rows: list[dict[str, Any]],
    excel_path: Path,
    sheet_name: str,
    max_rows: int,
    *,
    now_utc: datetime,
    max_age_hours: int,
    exclude_if_no_posted_at: bool,
) -> None:
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(new_rows)
    if new_df.empty:
        LOG.warning("No new rows from API after processing.")
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
    old_df = filter_dataframe_by_recency(
        old_df,
        now_utc=now_utc,
        max_age_hours=max_age_hours,
        exclude_if_no_posted_at=exclude_if_no_posted_at,
    )
    old_df["_dedupe"] = [dedupe_key(r) for r in old_df.to_dict("records")]

    combined = pd.concat([new_df, old_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["_dedupe"], keep="first")
    combined = combined.drop(columns=["_dedupe"])

    def _score_val(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("-inf")

    combined["_rs"] = combined["relevance_score"].map(_score_val)
    combined = combined.sort_values(
        by=["_rs", "posted_at", "title"],
        ascending=[False, False, True],
    )
    combined = combined.drop(columns=["_rs"])
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

    filters_cfg = cfg.get("filters") or {}
    max_age_hours = int(filters_cfg.get("max_age_hours") or 48)
    exclude_if_no_posted_at = bool(filters_cfg.get("exclude_if_no_posted_at", True))

    llm_cfg = cfg.get("llm") or {}
    llm_enabled = bool(llm_cfg.get("enabled", False))
    snippet_max = int(llm_cfg.get("description_snippet_max_chars") or 3000)

    retention = cfg.get("retention") or {}
    max_rows = int(retention.get("max_rows") or 5000)
    excel_cfg = cfg.get("excel") or {}
    excel_path = REPO_ROOT / str(excel_cfg.get("path") or "data/jobs.xlsx")
    sheet_name = str(excel_cfg.get("sheet") or "jobs")

    now_utc = datetime.now(timezone.utc)
    fetched_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

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

    rows = filter_rows_by_recency(
        rows,
        now_utc=now_utc,
        max_age_hours=max_age_hours,
        exclude_if_no_posted_at=exclude_if_no_posted_at,
    )

    if llm_enabled:
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            LOG.error("llm.enabled is true but OPENAI_API_KEY is not set")
            return 1
        try:
            resume_text = load_resume_text(cfg)
        except FileNotFoundError as e:
            LOG.error("%s", e)
            return 1
        if not resume_text.strip():
            LOG.error("Resume text is empty")
            return 1
        enrich_rows_with_llm(
            rows,
            resume_text=resume_text,
            model=str(llm_cfg.get("model") or "gpt-4o-mini"),
            max_description_chars=int(llm_cfg.get("max_description_chars") or 12000),
            max_jobs_per_run=int(llm_cfg.get("max_jobs_per_run") or 40),
            delay_seconds=float(llm_cfg.get("request_delay_seconds") or 0.4),
        )
    else:
        for row in rows:
            row.setdefault("relevance_score", "")
            row.setdefault("relevance_rationale", "")
            row.setdefault("tailored_resume", "")
            row.setdefault("resume_tailoring", "")

    rows = finalize_rows_for_excel(rows, snippet_max=snippet_max)
    merge_into_excel(
        rows,
        excel_path,
        sheet_name,
        max_rows,
        now_utc=now_utc,
        max_age_hours=max_age_hours,
        exclude_if_no_posted_at=exclude_if_no_posted_at,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
