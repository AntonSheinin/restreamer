import asyncio
import contextlib
from asyncio.subprocess import Process

from app.config import FFMPEG_PATH, Settings
from app.models import WorkerStatus
from app.services.files import FileService


class WorkerService:
    _backoff_seconds = (1, 2, 5, 10, 30)
    _stable_run_reset_seconds = 30

    def __init__(self, settings: Settings, file_service: FileService) -> None:
        self._settings = settings
        self._file_service = file_service
        self._process: Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._status = WorkerStatus()
        self._last_stderr_lines: list[str] = []

    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stop_event.clear()
        self._supervisor_task = asyncio.create_task(self._supervise(), name="ffmpeg-supervisor")

    async def stop(self) -> None:
        self._stop_event.set()
        await self._stop_process()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None

    def get_status(self) -> WorkerStatus:
        playlist_ready = self._file_service.playlist_path.is_file()
        return self._status.model_copy(update={"playlist_ready": playlist_ready})

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        attempt = 0
        while not self._stop_event.is_set():
            self._status = self._status.model_copy(
                update={
                    "state": "starting" if attempt == 0 else "restarting",
                    "last_error": self._status.last_error if attempt else None,
                    "pid": None,
                    "playlist_ready": False,
                }
            )

            await self._file_service.cleanup_outputs()
            self._last_stderr_lines = []

            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._build_ffmpeg_command(),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                self._status = self._status.model_copy(
                    update={
                        "state": "error",
                        "last_error": f"ffmpeg executable not found: {FFMPEG_PATH}",
                        "pid": None,
                    }
                )
                return
            except Exception as exc:
                self._status = self._status.model_copy(
                    update={"state": "error", "last_error": str(exc), "pid": None}
                )
                return

            self._status = self._status.model_copy(
                update={"state": "running", "last_error": None, "pid": self._process.pid}
            )

            started_at = loop.time()
            stderr_task = asyncio.create_task(self._consume_stderr(self._process))
            return_code = await self._process.wait()
            await stderr_task

            if self._stop_event.is_set():
                break

            run_duration = loop.time() - started_at
            if run_duration >= self._stable_run_reset_seconds:
                attempt = 0

            attempt += 1
            last_error = f"ffmpeg exited with code {return_code}"
            if self._last_stderr_lines:
                last_error = f"{last_error}: {' | '.join(self._last_stderr_lines)}"
            self._status = self._status.model_copy(
                update={
                    "state": "restarting",
                    "restart_count": self._status.restart_count + 1,
                    "consecutive_failures": attempt,
                    "last_error": last_error,
                    "pid": None,
                }
            )

            delay = self._backoff_seconds[min(attempt - 1, len(self._backoff_seconds) - 1)]
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    async def _consume_stderr(self, process: Process) -> None:
        if process.stderr is None:
            return

        last_lines: list[str] = []
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            last_lines.append(text)
            if len(last_lines) > 5:
                last_lines.pop(0)
            self._last_stderr_lines = last_lines.copy()

    async def _stop_process(self) -> None:
        if self._process is None:
            return

        process = self._process
        self._process = None

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        self._status = self._status.model_copy(update={"pid": None})

    def _build_ffmpeg_command(self) -> list[str]:
        return [
            str(FFMPEG_PATH),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "10",
            "-rw_timeout",
            "15000000",
            "-i",
            self._settings.source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            # Lower-CPU compromise: keep video copied, normalize audio for stable MPEG-TS HLS.
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-af",
            "aresample=async=1:first_pts=0",
            "-f",
            "hls",
            "-hls_time",
            str(self._settings.hls_segment_time),
            "-hls_list_size",
            str(self._settings.hls_list_size),
            "-hls_delete_threshold",
            "4",
            "-hls_allow_cache",
            "0",
            "-hls_flags",
            "delete_segments+temp_file+omit_endlist+independent_segments",
            "-hls_segment_filename",
            self._file_service.segment_path_pattern,
            str(self._file_service.playlist_path),
        ]
