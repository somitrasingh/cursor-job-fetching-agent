"""Parse posting dates, filter by recency, and call OpenAI for relevance + resume tailoring."""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from dateutil import parser as date_parser

LOG = logging.getLogger(__name__)


def strip_html(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    t = html_lib.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_posted_at_utc(value: str | None) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        dt = date_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


def cell_posted_to_utc(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        t = value.to_pydatetime()
    elif isinstance(value, datetime):
        t = value
    else:
        return parse_posted_at_utc(str(value))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    else:
        t = t.astimezone(timezone.utc)
    return t


def filter_dataframe_by_recency(
    df: pd.DataFrame,
    *,
    now_utc: datetime,
    max_age_hours: int,
    exclude_if_no_posted_at: bool,
    posted_col: str = "posted_at",
) -> pd.DataFrame:
    if df.empty or max_age_hours <= 0:
        return df
    cutoff = now_utc - timedelta(hours=max_age_hours)
    keep: list[int] = []
    for idx, row in df.iterrows():
        dt = cell_posted_to_utc(row.get(posted_col))
        if dt is None:
            if exclude_if_no_posted_at:
                continue
            keep.append(idx)
            continue
        if dt >= cutoff:
            keep.append(idx)
    out = df.loc[keep].reset_index(drop=True)
    LOG.info("Excel recency filter: %s -> %s rows", len(df), len(out))
    return out


def filter_rows_by_recency(
    rows: list[dict[str, Any]],
    *,
    now_utc: datetime,
    max_age_hours: int,
    exclude_if_no_posted_at: bool,
) -> list[dict[str, Any]]:
    if max_age_hours <= 0:
        return rows
    cutoff = now_utc - timedelta(hours=max_age_hours)
    kept: list[dict[str, Any]] = []
    dropped_no_date = 0
    dropped_old = 0
    for row in rows:
        dt = parse_posted_at_utc(row.get("posted_at"))
        if dt is None:
            if exclude_if_no_posted_at:
                dropped_no_date += 1
                continue
            kept.append(row)
            continue
        if dt < cutoff:
            dropped_old += 1
            continue
        kept.append(row)
    LOG.info(
        "Recency filter: kept=%s dropped_old=%s dropped_no_posted_at=%s (cutoff=%s UTC)",
        len(kept),
        dropped_old,
        dropped_no_date,
        cutoff.isoformat(),
    )
    return kept


def enrich_row_openai(
    row: dict[str, Any],
    *,
    resume_text: str,
    model: str,
    max_desc_chars: int,
) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    desc = (row.get("_desc_for_llm") or "")[:max_desc_chars]
    prompt = f"""You are a career coach. Given the candidate's resume and a job posting, respond with ONLY valid JSON (no markdown) with these keys:
- relevance_score: integer 0-100 (how well the candidate fits this role)
- relevance_rationale: string, 2-4 sentences explaining the score
- tailored_resume: string — the COMPLETE resume rewritten for this job. CRITICAL: preserve the SAME structure as the candidate resume below — same section headings in the SAME order, same hierarchy (e.g. if they use bullets under jobs, keep bullets; if they use paragraphs, keep paragraphs). Match spacing/line breaks style when reasonable. Reword, reorder, and emphasize bullets to align with the job; do NOT invent employers, degrees, dates, certifications, or tools the candidate did not already list. If a section is empty of relevant content, keep the section header but leave minimal honest content.
- resume_tailoring: string — 3-6 short lines (each starting with "- ") summarizing what you changed vs the original for quick review

Candidate resume (this is the format and structure you must preserve):
{resume_text}

Job title: {row.get("title", "")}
Company: {row.get("company", "")}
Location: {row.get("location", "")}

Job description (may be partial):
{desc}
"""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Return only a single JSON object. No markdown fences. "
                    "The tailored_resume value must be plain text (newlines allowed), "
                    "not HTML or markdown tables."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.35,
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)
    score = data.get("relevance_score")
    try:
        score_int = int(score)
    except (TypeError, ValueError):
        score_int = 0
    score_int = max(0, min(100, score_int))
    return {
        "relevance_score": score_int,
        "relevance_rationale": str(data.get("relevance_rationale") or ""),
        "tailored_resume": str(data.get("tailored_resume") or ""),
        "resume_tailoring": str(data.get("resume_tailoring") or ""),
    }


def enrich_rows_with_llm(
    rows: list[dict[str, Any]],
    *,
    resume_text: str,
    model: str,
    max_description_chars: int,
    max_jobs_per_run: int,
    delay_seconds: float,
) -> None:
    if not rows:
        return
    limit = min(len(rows), max(0, max_jobs_per_run))
    for i, row in enumerate(rows[:limit]):
        try:
            out = enrich_row_openai(
                row,
                resume_text=resume_text,
                model=model,
                max_desc_chars=max_description_chars,
            )
            row["relevance_score"] = out["relevance_score"]
            row["relevance_rationale"] = out["relevance_rationale"]
            row["tailored_resume"] = out["tailored_resume"]
            row["resume_tailoring"] = out["resume_tailoring"]
            LOG.info(
                "LLM %s/%s title=%r score=%s",
                i + 1,
                limit,
                row.get("title"),
                row["relevance_score"],
            )
        except Exception as e:
            LOG.error("LLM failed for %r: %s", row.get("title"), e)
            row["relevance_score"] = ""
            row["relevance_rationale"] = ""
            row["tailored_resume"] = ""
            row["resume_tailoring"] = f"(error: {e})"
        if delay_seconds > 0 and i < limit - 1:
            time.sleep(delay_seconds)
    for row in rows[limit:]:
        row["relevance_score"] = ""
        row["relevance_rationale"] = ""
        row["tailored_resume"] = ""
        row["resume_tailoring"] = "(skipped: max_jobs_per_run)"
