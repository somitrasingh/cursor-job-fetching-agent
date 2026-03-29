"""Fail fast in GitHub Actions if secrets or resume are missing (see daily-jobs workflow)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    provider = (cfg.get("provider") or "adzuna").lower().strip()

    if provider == "adzuna":
        if not os.environ.get("ADZUNA_APP_ID", "").strip():
            print("::error::Missing secret ADZUNA_APP_ID")
            return 1
        if not os.environ.get("ADZUNA_APP_KEY", "").strip():
            print("::error::Missing secret ADZUNA_APP_KEY")
            return 1
    elif provider == "jsearch":
        if not os.environ.get("RAPIDAPI_KEY", "").strip():
            print("::error::Missing secret RAPIDAPI_KEY")
            return 1
    else:
        print(f"::error::Unknown provider in config.yaml: {provider!r}")
        return 1

    llm = cfg.get("llm") or {}
    if llm.get("enabled"):
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            print("::error::Missing secret OPENAI_API_KEY (llm.enabled is true in config.yaml)")
            return 1
        resume_path = REPO_ROOT / str((cfg.get("resume") or {}).get("path") or "data/resume.txt")
        text = ""
        if resume_path.is_file():
            text = resume_path.read_text(encoding="utf-8").strip()
        if not text:
            print(
                "::error::No resume text: commit non-empty "
                f"{resume_path.relative_to(REPO_ROOT)} (use a private repo) or set RESUME_TEXT secret."
            )
            return 1

    print("Prerequisites OK for provider=%s llm=%s" % (provider, llm.get("enabled")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
