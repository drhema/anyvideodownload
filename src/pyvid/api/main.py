"""FastAPI wrapper for pyvid.

Endpoints
---------
  POST   /download        submit a URL, returns job id + initial status
  GET    /jobs            list jobs (owner of the token sees their jobs; with
                          no auth, lists all)
  GET    /jobs/{id}       job status JSON
  GET    /jobs/{id}/file  stream the downloaded MP4 (once status=completed)
  DELETE /jobs/{id}       delete the job + its files
  GET    /health          liveness probe (no auth)

Config via env vars
-------------------
  PYVID_API_TOKENS     comma-separated bearer tokens. Empty = auth disabled.
  PYVID_STORAGE        output dir (default ./pyvid-storage)
  PYVID_CONCURRENCY    parallel download workers (default 1; Chromium is heavy)
  PYVID_RATE_LIMIT     requests/min per token (default 10, 0 = disabled)
  PYVID_HOST           bind host (default 127.0.0.1)
  PYVID_PORT           bind port (default 8000)

Run
---
  pyvid-api             # starts uvicorn
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .auth import TOKENS, require_token
from .jobs import JobManager
from .models import DownloadRequest, JobView
from .rate_limit import RateLimiter


STORAGE_ROOT = Path(os.environ.get("PYVID_STORAGE", "./pyvid-storage")).expanduser().resolve()
CONCURRENCY = int(os.environ.get("PYVID_CONCURRENCY", "1"))
RATE_LIMIT = int(os.environ.get("PYVID_RATE_LIMIT", "10"))


_jobs: JobManager | None = None
_limiter = RateLimiter(max_per_minute=RATE_LIMIT)


def _get_jobs() -> JobManager:
    assert _jobs is not None, "JobManager not initialized (lifespan not run)"
    return _jobs


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _jobs
    _jobs = JobManager(STORAGE_ROOT, concurrency=CONCURRENCY)
    await _jobs.start()
    try:
        yield
    finally:
        await _jobs.stop()


app = FastAPI(
    title="pyvid API",
    description="Browser-intercept video downloader as an HTTP service.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "storage": str(STORAGE_ROOT),
        "concurrency": CONCURRENCY,
        "auth_enabled": bool(TOKENS),
        "rate_limit_per_min": RATE_LIMIT,
    }


@app.post("/download", response_model=JobView, status_code=202)
async def submit_download(req: DownloadRequest, token: str = Depends(require_token)):
    _limiter.check(token)
    opts = {
        "headless": req.headless,
        "max_ms": req.max_ms,
        "idle_ms": req.idle_ms,
        "format_override": req.format,
    }
    job = _get_jobs().submit(req.url, opts)
    return job.view()


@app.get("/jobs", response_model=list[JobView])
async def list_jobs(token: str = Depends(require_token)):
    _limiter.check(token)
    return [j.view() for j in _get_jobs().list()]


@app.get("/jobs/{job_id}", response_model=JobView)
async def job_status(job_id: str, token: str = Depends(require_token)):
    _limiter.check(token)
    job = _get_jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.view()


@app.get("/jobs/{job_id}/file")
async def job_file(job_id: str, token: str = Depends(require_token)):
    _limiter.check(token)
    job = _get_jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == "queued" or job.status == "running":
        raise HTTPException(status_code=409, detail=f"Job still {job.status}")
    if job.status == "failed":
        raise HTTPException(status_code=410, detail=f"Job failed: {job.error}")
    if not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(status_code=410, detail="Output file missing (maybe deleted)")
    path = Path(job.output_path)
    return FileResponse(
        path,
        filename=f"{job_id}-{path.name}",
        media_type="video/mp4",
    )


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, token: str = Depends(require_token)):
    _limiter.check(token)
    ok = _get_jobs().delete(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"deleted": True}


def main() -> None:
    import uvicorn
    host = os.environ.get("PYVID_HOST", "127.0.0.1")
    port = int(os.environ.get("PYVID_PORT", "8000"))
    # Run in-process (no reload) so the shared JobManager singleton persists.
    uvicorn.run("pyvid.api.main:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
