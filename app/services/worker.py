import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from asyncio.subprocess import Process

from app.config import ChannelConfig, FFMPEG_PATH, Settings
from app.models import ChannelStatus
from app.services.files import FileService

logger = logging.getLogger("uvicorn.error")


class ActiveStreamConflict(Exception):
    pass


class BaseChannelWorker(ABC):
    _backoff_seconds = (1, 2, 5, 10, 30)
    _stable_run_reset_seconds = 30

    def __init__(
        self,
        channel: ChannelConfig,
        settings: Settings,
        file_service: FileService,
    ) -> None:
        self.channel = channel
        self._settings = settings
        self._file_service = file_service
        self._process: Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._status = ChannelStatus(channel=channel.name, output_format=channel.output_format)
        self._last_stderr_lines: list[str] = []

    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stop_event.clear()
        self._supervisor_task = asyncio.create_task(
            self._supervise(),
            name=f"{self.channel.name}-ffmpeg-supervisor",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        await self._stop_process()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None

    def get_status(self) -> ChannelStatus:
        return self._status

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        attempt = 0

        while not self._stop_event.is_set():
            self._status = self._status.model_copy(
                update={
                    "state": "starting" if attempt == 0 else "restarting",
                    "last_error": self._status.last_error if attempt else None,
                    "pid": None,
                }
            )
            self._last_stderr_lines = []

            try:
                await self._before_start()
                command = self._build_ffmpeg_command()
                logger.info(
                    "starting ffmpeg worker for %s: %s",
                    self.channel.name,
                    " ".join(command),
                )
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=self._stdout_target(),
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

            process = self._process
            self._status = self._status.model_copy(
                update={"state": "running", "last_error": None, "pid": process.pid}
            )
            logger.info("ffmpeg worker for %s started with pid=%s", self.channel.name, process.pid)

            started_at = loop.time()
            stderr_task = asyncio.create_task(self._consume_stderr(process))
            extra_tasks = await self._create_process_tasks(process, loop)
            return_code = await process.wait()
            for task in extra_tasks:
                task.cancel()
            for task in extra_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await stderr_task
            await self._after_process_stop()

            if self._stop_event.is_set():
                break

            run_duration = loop.time() - started_at
            if run_duration >= self._stable_run_reset_seconds:
                attempt = 0

            attempt += 1
            last_error = f"ffmpeg exited with code {return_code}"
            if self._last_stderr_lines:
                last_error = f"{last_error}: {' | '.join(self._last_stderr_lines)}"
            logger.warning("ffmpeg worker for %s stopped: %s", self.channel.name, last_error)
            self._status = self._status.model_copy(
                update={
                    "state": "restarting",
                    "restart_count": self._status.restart_count + 1,
                    "last_error": last_error,
                    "pid": None,
                }
            )

            delay = self._backoff_seconds[min(attempt - 1, len(self._backoff_seconds) - 1)]
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                logger.info(
                    "restarting ffmpeg worker for %s after %ss backoff",
                    self.channel.name,
                    delay,
                )
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
            logger.info("ffmpeg-%s: %s", self.channel.name, text)
            last_lines.append(text)
            if len(last_lines) > 5:
                last_lines.pop(0)
            self._last_stderr_lines = last_lines.copy()

    async def _before_start(self) -> None:
        return None

    async def _create_process_tasks(
        self,
        process: Process,
        loop: asyncio.AbstractEventLoop,
    ) -> list[asyncio.Task[None]]:
        return []

    async def _after_process_stop(self) -> None:
        return None

    async def _stop_process(self) -> None:
        if self._process is None:
            return

        process = self._process
        self._process = None

        if process.returncode is None:
            logger.info("stopping ffmpeg worker for %s pid=%s", self.channel.name, process.pid)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "ffmpeg worker for %s pid=%s did not stop in time; killing",
                    self.channel.name,
                    process.pid,
                )
                process.kill()
                await process.wait()

        await self._after_process_stop()
        self._status = self._status.model_copy(update={"pid": None})

    def _common_ffmpeg_args(self) -> list[str]:
        input_args: list[str] = []
        tshttp = self.channel.tshttp
        if tshttp is not None and tshttp.input_fflags:
            input_args.extend(["-fflags", tshttp.input_fflags])

        copytb = str(tshttp.copytb) if tshttp is not None else "1"
        video_args = self._video_ffmpeg_args()
        audio_args = self._audio_ffmpeg_args()

        return [
            str(FFMPEG_PATH),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            self._settings.ffmpeg_loglevel,
            "-stats_period",
            "5",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "10",
            "-rw_timeout",
            "15000000",
            *input_args,
            "-i",
            self.channel.source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-copytb",
            copytb,
            *video_args,
            *audio_args,
        ]

    def _video_ffmpeg_args(self) -> list[str]:
        if self.channel.transcoding.video == "transcode":
            return [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
            ]

        return [
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
        ]

    def _audio_ffmpeg_args(self) -> list[str]:
        if self.channel.transcoding.audio == "copy":
            return [
                "-c:a",
                "copy",
            ]

        return [
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
        ]

    @abstractmethod
    def _stdout_target(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def _build_ffmpeg_command(self) -> list[str]:
        raise NotImplementedError


class HlsChannelWorker(BaseChannelWorker):
    async def _before_start(self) -> None:
        await self._file_service.cleanup_hls_outputs(self.channel.name)

    def _stdout_target(self) -> int:
        return asyncio.subprocess.DEVNULL

    def _build_ffmpeg_command(self) -> list[str]:
        hls = self.channel.hls
        if hls is None:
            raise ValueError(f"channel '{self.channel.name}' missing hls settings")

        return [
            *self._common_ffmpeg_args(),
            "-f",
            "hls",
            "-hls_time",
            str(hls.segment_time),
            "-hls_list_size",
            str(hls.list_size),
            "-hls_flags",
            "delete_segments+omit_endlist+temp_file",
            "-hls_segment_filename",
            self._file_service.segment_path_pattern(self.channel.name),
            str(self._file_service.playlist_path(self.channel.name)),
        ]


class TshttpChannelWorker(BaseChannelWorker):
    _subscriber_queue_size = 32

    def __init__(
        self,
        channel: ChannelConfig,
        settings: Settings,
        file_service: FileService,
    ) -> None:
        super().__init__(channel=channel, settings=settings, file_service=file_service)
        self._active_consumer: asyncio.Queue[bytes | None] | None = None
        self._consumer_guard = asyncio.Lock()
        self._last_output_at: float | None = None

    def _stdout_target(self) -> int:
        return asyncio.subprocess.PIPE

    async def open_stream(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._consumer_guard:
            if self._active_consumer is not None:
                raise ActiveStreamConflict("channel already has an active consumer")
            self._active_consumer = queue

        return self._stream_generator(queue)

    async def _stream_generator(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            async with self._consumer_guard:
                if self._active_consumer is queue:
                    self._active_consumer = None

    async def _create_process_tasks(
        self,
        process: Process,
        loop: asyncio.AbstractEventLoop,
    ) -> list[asyncio.Task[None]]:
        self._last_output_at = None
        return [
            asyncio.create_task(self._consume_stdout(process, loop)),
            asyncio.create_task(self._watch_output_staleness(process, loop)),
        ]

    async def _after_process_stop(self) -> None:
        async with self._consumer_guard:
            queue = self._active_consumer
        if queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    async def _consume_stdout(self, process: Process, loop: asyncio.AbstractEventLoop) -> None:
        if process.stdout is None:
            return

        while True:
            chunk = await process.stdout.read(64 * 1024)
            if not chunk:
                break
            self._last_output_at = loop.time()
            await self._broadcast_chunk(chunk)

    async def _watch_output_staleness(
        self,
        process: Process,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        tshttp = self.channel.tshttp
        if tshttp is None:
            return

        started_at = loop.time()
        while process.returncode is None and not self._stop_event.is_set():
            await asyncio.sleep(1)
            reference_time = self._last_output_at if self._last_output_at is not None else started_at
            if loop.time() - reference_time <= tshttp.stale_output_seconds:
                continue

            self._last_stderr_lines = [
                f"ffmpeg produced no MPEG-TS output for {tshttp.stale_output_seconds}s; restarting"
            ]
            logger.warning("ffmpeg-%s: %s", self.channel.name, self._last_stderr_lines[0])
            process.terminate()
            return

    async def _broadcast_chunk(self, chunk: bytes) -> None:
        async with self._consumer_guard:
            queue = self._active_consumer

        if queue is None:
            return

        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(chunk)
        except asyncio.QueueEmpty:
            queue.put_nowait(chunk)

    def _build_ffmpeg_command(self) -> list[str]:
        tshttp = self.channel.tshttp
        if tshttp is None:
            raise ValueError(f"channel '{self.channel.name}' missing tshttp settings")

        command = [
            *self._common_ffmpeg_args(),
            "-f",
            "mpegts",
            "-mpegts_flags",
            "resend_headers",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        if tshttp.mpegts_copyts:
            command[command.index("-muxdelay"):command.index("-muxdelay")] = ["-mpegts_copyts", "1"]
        return command


class ChannelManager:
    def __init__(
        self,
        settings: Settings,
        file_service: FileService,
        channels: list[ChannelConfig],
    ) -> None:
        self._settings = settings
        self._file_service = file_service
        self._channels = {channel.name: channel for channel in channels}
        self._workers: dict[str, BaseChannelWorker] = {
            channel.name: self._build_worker(channel) for channel in channels
        }

    async def start(self) -> None:
        await self._file_service.prepare_runtime_root()
        for worker in self._workers.values():
            await worker.start()

    async def stop(self) -> None:
        for worker in self._workers.values():
            await worker.stop()

    def list_statuses(self) -> list[ChannelStatus]:
        return [self._workers[name].get_status() for name in sorted(self._workers)]

    def get_status(self, channel_name: str) -> ChannelStatus | None:
        worker = self._workers.get(channel_name)
        if worker is None:
            return None
        return worker.get_status()

    def get_channel(self, channel_name: str) -> ChannelConfig | None:
        return self._channels.get(channel_name)

    def get_tshttp_worker(self, channel_name: str) -> TshttpChannelWorker | None:
        worker = self._workers.get(channel_name)
        if isinstance(worker, TshttpChannelWorker):
            return worker
        return None

    def _build_worker(self, channel: ChannelConfig) -> BaseChannelWorker:
        if channel.output_format == "hls":
            return HlsChannelWorker(
                channel=channel,
                settings=self._settings,
                file_service=self._file_service,
            )
        return TshttpChannelWorker(
            channel=channel,
            settings=self._settings,
            file_service=self._file_service,
        )
