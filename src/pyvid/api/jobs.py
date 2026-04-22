"""Job manager: async queue + worker pool calling pyvid.core.orchestrator.

Playwright launches a real Chromium per job, which is heavy (hundreds of MB
RAM per browser). Keep concurrency low by default — 1 is safe, raise only
if you have the resources.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.orchestrator import download_video
from .models import JobState, JobView


@dataclass
class Job:
    url: str
    opts: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobState = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_path: str | None = None
    error: str | None = None

    def view(self) -> JobView:
        size = None
        name = None
        if self.output_path:
            p = Path(self.output_path)
            if p.exists():
                size = p.stat().st_size
                name = p.name
        return JobView(
            id=self.id,
            url=self.url,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            output_filename=name,
            file_size_bytes=size,
            error=self.error,
        )


class JobManager:
    def __init__(self, storage_root: Path, concurrency: int = 1):
        self.storage = storage_root
        self.storage.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.concurrency = concurrency
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        for i in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"pyvid-worker-{i}"))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker(self, idx: int) -> None:
        while True:
            try:
                job = await self.queue.get()
            except asyncio.CancelledError:
                return
            print(f"[api-worker-{idx}] picked job {job.id}: {job.url}", file=sys.stderr)
            await self._run_job(job)

    async def _run_job(self, job: Job) -> None:
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        out_dir = self.storage / job.id
        try:
            result = await download_video(job.url, out_dir, **job.opts)
            job.output_path = result.output_path
            job.status = "completed"
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
            job.status = "failed"
            print(f"[api-worker] job {job.id} failed: {job.error}", file=sys.stderr)
        finally:
            job.finished_at = datetime.now(timezone.utc)

    def submit(self, url: str, opts: dict[str, Any]) -> Job:
        job = Job(url=url, opts=opts)
        self.jobs[job.id] = job
        self.queue.put_nowait(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return False
        if job.output_path:
            try:
                shutil.rmtree(Path(job.output_path).parent, ignore_errors=True)
            except Exception:
                pass
        return True

    def list(self) -> list[Job]:
        return list(self.jobs.values())
