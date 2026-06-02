"""任务模型、内存存储、permit、清理。

并发模型：
- `run_job` 是**同步**函数，由 FastAPI BackgroundTasks 调度到默认线程池执行。
- 因此并发限流用 `threading.BoundedSemaphore`，**不**用 `asyncio.Semaphore`。
- Permit 在 POST 入口 acquire（覆盖 pending + running），由 `release_job_permit` 原子释放。

清理模型：
- 运行中任务 `expires_at = None`，**不**会被按 TTL 清理。
- 任务进入终态（done / done_with_warnings / error / timeout）时设 `expires_at = now + JOB_TTL_SECONDS`。
- 超过 `JOB_MAX_RUNTIME_SECONDS` 仍在跑的任务被 `cleanup_stuck()` 标记为 timeout。
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional

import tts_core

logger = logging.getLogger(__name__)

JobStatus = Literal[
    "pending", "running", "done", "done_with_warnings", "error", "timeout",
]

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", 2))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 3600))
JOB_MAX_RUNTIME_SECONDS = int(os.environ.get("JOB_MAX_RUNTIME_SECONDS", 1800))
SILENCE_BETWEEN_MS = int(os.environ.get("SILENCE_BETWEEN_MS", 500))
INTER_SEGMENT_SLEEP = float(os.environ.get("INTER_SEGMENT_SLEEP", 0.2))


@dataclass
class Job:
    id: str
    token: str = field(repr=False)
    segments: list[str] = field(default_factory=list, repr=False)
    status: JobStatus = "pending"
    progress: int = 0
    total: int = 0
    failed_segments: list[int] = field(default_factory=list)
    result_path: Optional[str] = None
    error: Optional[str] = None
    tmp_dir: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    expires_at: Optional[float] = None
    _permit_released: bool = field(default=False, repr=False)


# ---- 全局状态（仅单 worker 进程内有效）------------------------------------

JOBS: dict[str, Job] = {}
_jobs_lock = threading.Lock()
JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)


# ---- JOBS 字典 helper（所有读写都走这里，避免裸字典访问）------------------

def jobs_put(job: Job) -> None:
    with _jobs_lock:
        JOBS[job.id] = job


def jobs_get(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return JOBS.get(job_id)


def jobs_pop(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return JOBS.pop(job_id, None)


def jobs_snapshot() -> list[Job]:
    with _jobs_lock:
        return list(JOBS.values())


# ---- Permit 释放（原子）---------------------------------------------------

def release_job_permit(job: Job) -> None:
    """幂等释放：内部持锁检查并翻转标志，再释放 semaphore。

    无论 run_job 正常结束、超时清理、还是 POST 错误回滚都走它，避免双重 release。
    """
    with _jobs_lock:
        if job._permit_released:
            return
        job._permit_released = True
    JOB_SEMAPHORE.release()


# ---- 任务创建 -------------------------------------------------------------

def new_job(segments: list[str], tmp_dir: str) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        token=secrets.token_urlsafe(32),
        segments=segments,
        total=len(segments),
        tmp_dir=tmp_dir,
        created_at=time.time(),
    )
    jobs_put(job)
    return job


# ---- 后台任务（同步函数，跑在线程池中）------------------------------------

def run_job(job_id: str, cfg: tts_core.TTSConfig) -> None:
    job = jobs_get(job_id)
    if job is None:
        logger.warning("run_job: job %s not found (already cleaned)", job_id)
        return

    try:
        job.status = "running"
        job.started_at = time.time()

        segment_files: list[Path] = []
        tmp_dir = Path(job.tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        for i, text in enumerate(list(job.segments)):
            try:
                audio_bytes, _duration = tts_core.synthesize_text(text, cfg)
                seg_path = tmp_dir / f"segment_{i + 1:03d}.mp3"
                seg_path.write_bytes(audio_bytes)
                segment_files.append(seg_path)
            except RuntimeError as e:
                logger.warning("job=%s seg=%d failed: %s", job.id[:8], i, e)
                job.failed_segments.append(i)
            finally:
                job.progress = i + 1
                time.sleep(INTER_SEGMENT_SLEEP)

        if segment_files:
            merged_path = tmp_dir / "output.mp3"
            if tts_core.merge_audio_files(
                [str(p) for p in segment_files],
                str(merged_path),
                SILENCE_BETWEEN_MS,
            ):
                job.result_path = str(merged_path)

        failed = len(job.failed_segments)
        if failed == 0:
            job.status = "done"
        elif failed < job.total:
            job.status = "done_with_warnings"
        else:
            job.status = "error"
            job.error = "all segments failed"

    except Exception as e:  # noqa: BLE001
        logger.exception("run_job unexpected error: job=%s", job_id[:8])
        job.status = "error"
        job.error = str(e)
    finally:
        job.finished_at = time.time()
        job.expires_at = job.finished_at + JOB_TTL_SECONDS
        job.segments.clear()
        release_job_permit(job)


# ---- 清理 -----------------------------------------------------------------

_TERMINAL_STATES = {"done", "done_with_warnings", "error", "timeout"}


def cleanup_stuck(now: Optional[float] = None) -> int:
    """把超过 JOB_MAX_RUNTIME_SECONDS 仍在跑的任务标记为 timeout 并释放 permit。"""
    now = now if now is not None else time.time()
    count = 0
    for job in jobs_snapshot():
        if job.status in ("pending", "running") and job.started_at:
            if now - job.started_at > JOB_MAX_RUNTIME_SECONDS:
                job.status = "timeout"
                job.error = "exceeded max runtime"
                job.finished_at = now
                job.expires_at = now + JOB_TTL_SECONDS
                job.segments.clear()
                release_job_permit(job)
                count += 1
                logger.warning(
                    "cleanup_stuck: job=%s marked timeout after %ds",
                    job.id[:8], int(now - job.started_at),
                )
    return count


def cleanup_expired(now: Optional[float] = None) -> int:
    """清理已设 expires_at 且过期的终态任务；不动 expires_at is None 的任务。"""
    now = now if now is not None else time.time()
    expired: list[Job] = []
    for job in jobs_snapshot():
        if job.expires_at is not None and job.expires_at < now:
            expired.append(job)

    for job in expired:
        # 防御性 release：终态任务理论上 permit 已释放，这里幂等兜底
        release_job_permit(job)
        if job.tmp_dir:
            shutil.rmtree(job.tmp_dir, ignore_errors=True)
        jobs_pop(job.id)
        logger.info("cleanup_expired: job=%s status=%s removed", job.id[:8], job.status)
    return len(expired)


def make_tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="tts-job-")
