from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class WorkerStatus(BaseModel):
    state: Literal["starting", "running", "restarting", "error"] = "starting"
    restart_count: int = 0
    consecutive_failures: int = 0
    playlist_ready: bool = False
    last_error: str | None = None
    pid: int | None = None
