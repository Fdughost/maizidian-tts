"""FastAPI 入口：路由、lifespan、安全中间件、TTSConfig 构建。

并发模型见 `webapp.jobs`：
- POST 入口立即非阻塞 `JOB_SEMAPHORE.acquire`，覆盖 pending+running。
- permit 在创建 Job 后转交给 `run_job` 的 finally；中途出错由 POST 自行 release。
- 启动一个 async cleanup loop 每 60 秒扫一次 `cleanup_stuck` + `cleanup_expired`。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import tts_core
from webapp import auth, ingest, jobs

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
)
logger = logging.getLogger("webapp")

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", 3))


def _build_config() -> tts_core.TTSConfig:
    appid = os.environ.get("VOLCENGINE_APPID", "")
    access_token = os.environ.get("VOLCENGINE_ACCESS_TOKEN", "")
    cluster = os.environ.get("VOLCENGINE_CLUSTER", "volcano_icl")
    voice_type = os.environ.get("VOLCENGINE_VOICE_TYPE", "")
    missing = [
        name
        for name, v in [
            ("VOLCENGINE_APPID", appid),
            ("VOLCENGINE_ACCESS_TOKEN", access_token),
            ("VOLCENGINE_VOICE_TYPE", voice_type),
        ]
        if not v
    ]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return tts_core.TTSConfig(
        appid=appid,
        access_token=access_token,
        cluster=cluster,
        voice_type=voice_type,
    )


CFG = _build_config()

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()

    async def cleanup_loop():
        while not stop.is_set():
            try:
                jobs.cleanup_stuck()
                jobs.cleanup_expired()
            except Exception:
                logger.exception("cleanup loop error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        stop.set()
        await task


app = FastAPI(lifespan=lifespan, title="HuoshanTTS Web")
app.state.limiter = limiter

app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/verify")
@limiter.limit("10/minute")
async def verify(
    request: Request,
    x_access_code: Optional[str] = Header(default=None, alias="X-Access-Code"),
):
    if not auth.verify_access_code(x_access_code):
        raise HTTPException(status_code=401, detail="Invalid access code")
    return {"ok": True}


@app.post("/api/jobs")
@limiter.limit(f"{RATE_LIMIT_PER_MINUTE}/minute")
async def create_job(
    request: Request,
    background_tasks: BackgroundTasks,
    x_access_code: Optional[str] = Header(default=None, alias="X-Access-Code"),
    source_type: str = Form(...),
    text: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
):
    if not auth.verify_access_code(x_access_code):
        raise HTTPException(status_code=401, detail="Invalid access code")

    if not jobs.JOB_SEMAPHORE.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Server busy, try again later")

    # 在 Job 创建前，permit 由本路径负责释放；创建后转交 run_job 的 finally。
    try:
        paragraphs = ingest.ingest(source_type, file, text)
        segments = ingest.split_and_validate(paragraphs)
        tmp_dir = jobs.make_tmp_dir()
        job = jobs.new_job(segments=segments, tmp_dir=tmp_dir)
    except Exception:
        jobs.JOB_SEMAPHORE.release()
        raise

    try:
        background_tasks.add_task(jobs.run_job, job.id, CFG)
    except Exception:
        jobs.release_job_permit(job)
        raise

    logger.info(
        '"job_created" job_id=%s code_hash=%s chars=%d segments=%d',
        job.id[:8],
        auth.hash_access_code(x_access_code),
        sum(len(s) for s in segments),
        len(segments),
    )

    return {"job_id": job.id, "job_token": job.token}


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    x_job_token: Optional[str] = Header(default=None, alias="X-Job-Token"),
):
    job = auth.verify_job_token(job_id, x_job_token)
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "failed_segments": list(job.failed_segments),
        "error": job.error,
    }


@app.get("/api/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    x_job_token: Optional[str] = Header(default=None, alias="X-Job-Token"),
):
    job = auth.verify_job_token(job_id, x_job_token)
    if job.status not in ("done", "done_with_warnings"):
        raise HTTPException(status_code=409, detail=f"Not ready: {job.status}")
    if not job.result_path or not Path(job.result_path).exists():
        raise HTTPException(status_code=410, detail="Result already cleaned")
    return FileResponse(
        job.result_path,
        media_type="audio/mpeg",
        filename=f"tts_{job.id[:8]}.mp3",
        headers={"Cache-Control": "no-store"},
    )
