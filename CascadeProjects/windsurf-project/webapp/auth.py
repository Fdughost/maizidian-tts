"""访问码与一次性任务 token 校验。"""
from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional

from fastapi import HTTPException

from webapp.jobs import Job, jobs_get


def _parse_codes() -> set[str]:
    raw = os.environ.get("ACCESS_CODES", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


ACCESS_CODES: set[str] = _parse_codes()
if not ACCESS_CODES:
    raise RuntimeError(
        "ACCESS_CODES is empty; refuse to start. Set ACCESS_CODES=code1,code2 in .env"
    )


def verify_access_code(supplied: Optional[str]) -> bool:
    code = (supplied or "").strip()
    return bool(code) and code in ACCESS_CODES


def hash_access_code(supplied: Optional[str]) -> str:
    """Short hash for audit logs; never logs the raw code."""
    code = (supplied or "").strip()
    if not code:
        return "-"
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


def verify_job_token(job_id: str, supplied: Optional[str]) -> Job:
    job = jobs_get(job_id)
    if not job or not secrets.compare_digest(job.token, supplied or ""):
        # 故意返回 404 而非 401/403，避免暴露 job_id 是否存在
        raise HTTPException(status_code=404, detail="Job not found")
    return job
