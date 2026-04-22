from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


JobState = Literal["queued", "running", "completed", "failed"]


class DownloadRequest(BaseModel):
    url: str = Field(..., description="Page URL containing the video")
    headless: bool = Field(False, description="Run Chromium headless (many sites block this)")
    max_ms: int = Field(120_000, ge=1_000, le=900_000, description="Hard cap on capture duration")
    idle_ms: int = Field(4_000, ge=500, le=60_000, description="Idle threshold before capture ends")
    format: str | None = Field(None, description="Force a transport: progressive|hls|dash|vimeo|ump")


class JobView(BaseModel):
    id: str
    url: str
    status: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_filename: str | None = None
    file_size_bytes: int | None = None
    error: str | None = None
