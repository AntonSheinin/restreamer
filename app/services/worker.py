import asyncio
import contextlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from asyncio.subprocess import Process
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.config import (
    ChannelConfig,
    FFMPEG_PATH,
    Settings,
    TshttpChannelConfig,
    load_channel_config,
)
from app.models import ChannelStatus
from app.services.files import FileService
from app.services.source_resolver import (
    ResolvedSource,
    SourceResolutionError,
    build_source_resolver,
)

logger = logging.getLogger("uvicorn.error")
SEGMENT_NUMBER_PATTERN = re.compile(r"segment_(\d+)\.ts$")
URL_TEXT_PATTERN = re.compile(r"https?://\S+")


class ActiveStreamConflict(Exception):
    pass


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    query = "redacted" if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _redact_command(command: list[str]) -> list[str]:
    return [_redact_url(part) for part in command]


def _redact_text_urls(value: str) -> str:
    return URL_TEXT_PATTERN.sub(lambda match: _redact_url(match.group(0)), value)


class WorkerStartGate:
    def __init__(self, max_concurrent_starts: int, stagger_seconds: float) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_starts)
        self._stagger_seconds = stagger_seconds
        self._stagger_lock = asyncio.Lock()
        self._last_start_at: float | None = None

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._semaphore.acquire()
        try:
            await self._wait_for_stagger()
            yield
        finally:
            self._semaphore.release()

    async def _wait_for_stagger(self) -> None:
        if self._stagger_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._stagger_lock:
            if self._last_start_at is not None:
                delay = self._stagger_seconds - (loop.time() - self._last_start_at)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_start_at = loop.time()


@dataclass
class HlsSegmentProbe:
    first_video_pts: float | None
    audio_stream_present: bool
    audio_packet_count: int | None
    audio_sample_rate: int | None
    audio_channels: int | None


class BaseChannelWorker(ABC):
    _backoff_seconds = (1, 2, 5, 10, 30)
    _stable_run_reset_seconds = 30

    def __init__(
        self,
        channel: ChannelConfig,
        settings: Settings,
        file_service: FileService,
        start_gate: WorkerStartGate,
    ) -> None:
        self.channel = channel
        self._settings = settings
        self._file_service = file_service
        self._start_gate = start_gate
        self._process: Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._status = ChannelStatus(channel=channel.name, output_format=channel.output_format)
        self._last_stderr_lines: list[str] = []
        self._source_resolver = build_source_resolver(channel)
        self._resolved_source: ResolvedSource | None = None

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

    def is_consumed(self) -> bool:
        return False

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
                async with self._start_gate.slot():
                    if self._stop_event.is_set():
                        break
                    await self._before_start()
                    self._resolved_source = await self._source_resolver.resolve()
                    command = self._build_ffmpeg_command()
                    logger.info("starting ffmpeg worker for %s", self.channel.name)
                    logger.debug(
                        "ffmpeg command for %s: %s",
                        self.channel.name,
                        " ".join(_redact_command(command)),
                    )
                    self._process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=self._stdout_target(),
                        stderr=asyncio.subprocess.PIPE,
                    )
            except FileNotFoundError:
                logger.exception("ffmpeg executable not found for %s: %s", self.channel.name, FFMPEG_PATH)
                self._status = self._status.model_copy(
                    update={
                        "state": "error",
                        "last_error": f"ffmpeg executable not found: {FFMPEG_PATH}",
                        "pid": None,
                    }
                )
                return
            except SourceResolutionError as exc:
                attempt += 1
                last_error = f"source resolution failed: {_redact_text_urls(str(exc))}"
                logger.warning(
                    "source resolver for %s failed: %s",
                    self.channel.name,
                    _redact_text_urls(str(exc)),
                )
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
                    break
                except asyncio.TimeoutError:
                    continue
            except Exception as exc:
                logger.exception("ffmpeg worker for %s failed before start", self.channel.name)
                self._status = self._status.model_copy(
                    update={"state": "error", "last_error": _redact_text_urls(str(exc)), "pid": None}
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
            text = _redact_text_urls(text)
            logger.debug("ffmpeg-%s: %s", self.channel.name, text)
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

    def _active_tshttp_settings(self) -> TshttpChannelConfig | None:
        if self.channel.output_format != "tshttp":
            return None
        return self.channel.tshttp

    def _common_ffmpeg_args(self) -> list[str]:
        input_args: list[str] = []
        if self.channel.input_live_start_index is not None:
            input_args.extend(["-live_start_index", str(self.channel.input_live_start_index)])

        tshttp = self._active_tshttp_settings()
        if self.channel.output_format == "hls":
            input_args.extend(
                [
                    "-fflags",
                    "+genpts+discardcorrupt",
                    "-analyzeduration",
                    "5000000",
                    "-probesize",
                    "5000000",
                ]
            )
        if tshttp is not None and tshttp.input_fflags:
            input_args.extend(["-fflags", tshttp.input_fflags])

        resolved_source = self._active_resolved_source()
        copytb = str(tshttp.copytb) if tshttp is not None else "1"
        video_args = self._video_ffmpeg_args()
        audio_args = self._audio_ffmpeg_args()

        command = [
            str(FFMPEG_PATH),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            self._ffmpeg_loglevel(),
            "-stats_period",
            "5",
        ]
        if self._settings.ffmpeg_threads:
            command.extend(
                [
                    "-filter_threads",
                    str(self._settings.ffmpeg_threads),
                    "-filter_complex_threads",
                    str(self._settings.ffmpeg_threads),
                ]
            )
        command.extend(
            [
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
                resolved_source.url,
                "-map",
                resolved_source.video_map,
                "-map",
                resolved_source.audio_map,
                "-copytb",
                copytb,
                *video_args,
                *audio_args,
            ]
        )
        return command

    def _active_resolved_source(self) -> ResolvedSource:
        if self._resolved_source is None:
            raise ValueError(f"channel '{self.channel.name}' source was not resolved")
        return self._resolved_source

    def _ffmpeg_loglevel(self) -> str:
        return "warning" if self._settings.debug else "quiet"

    def _video_ffmpeg_args(self) -> list[str]:
        if self.channel.transcoding.video == "transcode":
            args = [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
            ]
            if (
                self.channel.transcoding.video_width is not None
                and self.channel.transcoding.video_height is not None
            ):
                args.extend(
                    [
                        "-vf",
                        f"scale={self.channel.transcoding.video_width}:{self.channel.transcoding.video_height}",
                    ]
                )
            if self.channel.transcoding.video_bitrate is not None:
                args.extend(
                    [
                        "-b:v",
                        self.channel.transcoding.video_bitrate,
                    ]
                )
            if self.channel.transcoding.video_fps is not None:
                fps = self.channel.transcoding.video_fps
                hls = self.channel.hls
                segment_time = hls.segment_time if hls is not None else 4
                gop = fps * segment_time
                args.extend(
                    [
                        "-r",
                        str(fps),
                        "-g",
                        str(gop),
                        "-keyint_min",
                        str(gop),
                        "-sc_threshold",
                        "0",
                        "-force_key_frames",
                        f"expr:gte(t,n_forced*{segment_time})",
                    ]
                )
            if self._settings.ffmpeg_threads:
                args.extend(["-threads:v", str(self._settings.ffmpeg_threads)])
            return args

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
    _playlist_poll_seconds = 1
    _playlist_stale_floor_seconds = 30
    _segment_pts_jump_floor_seconds = 30
    _ffprobe_timeout_seconds = 5

    def __init__(
        self,
        channel: ChannelConfig,
        settings: Settings,
        file_service: FileService,
        start_gate: WorkerStartGate,
    ) -> None:
        super().__init__(
            channel=channel,
            settings=settings,
            file_service=file_service,
            start_gate=start_gate,
        )
        self._last_playlist_segment_number: int | None = None
        self._last_playlist_advanced_at: float | None = None
        self._last_checked_segment_number: int | None = None
        self._last_checked_segment_pts: float | None = None
        self._last_checked_segment_duration: float | None = None

    async def _before_start(self) -> None:
        await self._file_service.cleanup_hls_outputs(self.channel.name)
        self._last_playlist_segment_number = None
        self._last_playlist_advanced_at = None
        self._last_checked_segment_number = None
        self._last_checked_segment_pts = None
        self._last_checked_segment_duration = None

    async def _create_process_tasks(
        self,
        process: Process,
        loop: asyncio.AbstractEventLoop,
    ) -> list[asyncio.Task[None]]:
        self._last_playlist_advanced_at = loop.time()
        return [
            asyncio.create_task(self._watch_hls_health(process, loop)),
        ]

    def _stdout_target(self) -> int:
        return asyncio.subprocess.DEVNULL

    def _build_ffmpeg_command(self) -> list[str]:
        hls = self.channel.hls
        if hls is None:
            raise ValueError(f"channel '{self.channel.name}' missing hls settings")

        hls_flags = [
            "delete_segments",
            "omit_endlist",
            "temp_file",
        ]
        if self.channel.transcoding.video == "transcode":
            hls_flags.append("independent_segments")

        return [
            *self._common_ffmpeg_args(),
            "-mpegts_flags",
            "resend_headers+pat_pmt_at_frames",
            "-f",
            "hls",
            "-hls_time",
            str(hls.segment_time),
            "-hls_list_size",
            str(hls.list_size),
            "-hls_delete_threshold",
            str(hls.delete_threshold),
            "-hls_flags",
            "+".join(hls_flags),
            "-hls_segment_filename",
            self._file_service.segment_path_pattern(self.channel.name),
            str(self._file_service.playlist_path(self.channel.name)),
        ]

    async def _watch_hls_health(
        self,
        process: Process,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        hls = self.channel.hls
        if hls is None:
            return

        stale_seconds = max(self._playlist_stale_floor_seconds, hls.segment_time * 3)
        max_pts_jump_seconds = max(self._segment_pts_jump_floor_seconds, hls.segment_time * 3)
        playlist_path = self._file_service.playlist_path(self.channel.name)

        while process.returncode is None and not self._stop_event.is_set():
            await asyncio.sleep(self._playlist_poll_seconds)
            try:
                playlist = await asyncio.to_thread(playlist_path.read_text, encoding="utf-8")
            except FileNotFoundError:
                if self._last_playlist_advanced_at is None:
                    self._last_playlist_advanced_at = loop.time()
                if loop.time() - self._last_playlist_advanced_at > stale_seconds:
                    await self._terminate_for_health_failure(
                        process,
                        f"HLS playlist not created for {stale_seconds}s; restarting",
                    )
                    return
                continue

            playlist_state = self._parse_playlist(playlist)
            if playlist_state is None:
                continue

            _, segments = playlist_state
            latest_segment_name, latest_segment_duration = segments[-1]
            latest_segment_number = self._segment_number(latest_segment_name)
            if latest_segment_number is None:
                continue

            if self._last_playlist_segment_number != latest_segment_number:
                self._last_playlist_segment_number = latest_segment_number
                self._last_playlist_advanced_at = loop.time()
            elif self._last_playlist_advanced_at is not None and (
                loop.time() - self._last_playlist_advanced_at > stale_seconds
            ):
                await self._terminate_for_health_failure(
                    process,
                    f"HLS playlist did not advance for {stale_seconds}s; restarting",
                )
                return

            if latest_segment_number == self._last_checked_segment_number:
                continue

            segment_path = self._file_service.resolve_hls_asset_path(
                self.channel.name,
                latest_segment_name,
            )
            if segment_path is None:
                continue

            if not self._should_probe_hls_segment(latest_segment_number):
                continue

            try:
                probe = await self._probe_hls_segment(segment_path)
            except Exception as exc:
                logger.debug(
                    "ffmpeg-%s: failed to probe segment %s: %s",
                    self.channel.name,
                    latest_segment_name,
                    exc,
                )
                continue
            if probe.audio_stream_present and probe.audio_packet_count == 0:
                await self._terminate_for_health_failure(
                    process,
                    f"HLS segment {latest_segment_name} has an audio stream but no audio packets; restarting",
                )
                return
            if probe.audio_stream_present and (
                probe.audio_packet_count is None
                or probe.audio_sample_rate in (None, 0)
                or probe.audio_channels in (None, 0)
            ):
                await self._terminate_for_health_failure(
                    process,
                    f"HLS segment {latest_segment_name} has an unreadable audio stream; restarting",
                )
                return

            if (
                probe.first_video_pts is not None
                and self._last_checked_segment_number is not None
                and self._last_checked_segment_pts is not None
                and latest_segment_number == self._last_checked_segment_number + 1
            ):
                pts_delta = probe.first_video_pts - self._last_checked_segment_pts
                expected_delta = self._last_checked_segment_duration or hls.segment_time
                if pts_delta < 0 or pts_delta > max(max_pts_jump_seconds, expected_delta * 3):
                    await self._terminate_for_health_failure(
                        process,
                        (
                            f"HLS segment {latest_segment_name} PTS jumped by {pts_delta:.3f}s "
                            f"(expected about {expected_delta:.3f}s); restarting"
                        ),
                    )
                    return

            self._last_checked_segment_number = latest_segment_number
            self._last_checked_segment_pts = probe.first_video_pts
            self._last_checked_segment_duration = latest_segment_duration

    def _should_probe_hls_segment(self, latest_segment_number: int) -> bool:
        hls = self.channel.hls
        if hls is None or hls.probe_mode == "off":
            return False
        if hls.probe_mode == "every_segment":
            return latest_segment_number != self._last_checked_segment_number
        if self._last_checked_segment_number is None:
            return True
        return latest_segment_number - self._last_checked_segment_number >= hls.probe_interval_segments

    def _parse_playlist(self, playlist: str) -> tuple[int, list[tuple[str, float]]] | None:
        media_sequence: int | None = None
        segments: list[tuple[str, float]] = []
        current_duration: float | None = None

        for raw_line in playlist.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                media_sequence = int(line.split(":", 1)[1])
                continue
            if line.startswith("#EXTINF:"):
                current_duration = float(line.split(":", 1)[1].split(",", 1)[0])
                continue
            if line.startswith("#"):
                continue
            if current_duration is None:
                continue
            segments.append((line, current_duration))
            current_duration = None

        if media_sequence is None or not segments:
            return None
        return media_sequence, segments

    async def _probe_hls_segment(self, path: Path) -> HlsSegmentProbe:
        audio_probe = await self._run_ffprobe_json(
            [
                "-count_packets",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,channels,nb_read_packets",
                str(path),
            ]
        )
        video_probe = await self._run_ffprobe_json(
            [
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pts_time",
                "-read_intervals",
                "%+0.05",
                str(path),
            ]
        )

        audio_streams = audio_probe.get("streams", [])
        audio_stream_present = bool(audio_streams)
        audio_packet_count: int | None = None
        audio_sample_rate: int | None = None
        audio_channels: int | None = None
        if audio_stream_present:
            audio_stream = audio_streams[0]
            packet_value = audio_stream.get("nb_read_packets")
            if packet_value is not None:
                audio_packet_count = int(packet_value)
            sample_rate_value = audio_stream.get("sample_rate")
            if sample_rate_value is not None:
                audio_sample_rate = int(sample_rate_value)
            channels_value = audio_stream.get("channels")
            if channels_value is not None:
                audio_channels = int(channels_value)

        packets = video_probe.get("packets", [])
        first_video_pts: float | None = None
        if packets:
            first_packet_pts = packets[0].get("pts_time")
            if first_packet_pts is not None:
                first_video_pts = float(first_packet_pts)

        return HlsSegmentProbe(
            first_video_pts=first_video_pts,
            audio_stream_present=audio_stream_present,
            audio_packet_count=audio_packet_count,
            audio_sample_rate=audio_sample_rate,
            audio_channels=audio_channels,
        )

    async def _run_ffprobe_json(self, args: list[str]) -> dict[str, object]:
        process = await asyncio.create_subprocess_exec(
            str(FFMPEG_PATH.with_name("ffprobe")),
            "-hide_banner",
            "-v",
            "error",
            "-of",
            "json",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._ffprobe_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"ffprobe timed out after {self._ffprobe_timeout_seconds}s for {self.channel.name}"
            ) from exc
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip() or "unknown ffprobe error"
            raise RuntimeError(f"ffprobe failed for {self.channel.name}: {error_text}")
        return json.loads(stdout.decode("utf-8"))

    async def _terminate_for_health_failure(self, process: Process, reason: str) -> None:
        self._last_stderr_lines = [reason]
        logger.warning("ffmpeg-%s: %s", self.channel.name, reason)
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning(
                "ffmpeg-%s: process did not exit after health failure; killing",
                self.channel.name,
            )
            process.kill()

    def _segment_number(self, segment_name: str) -> int | None:
        match = SEGMENT_NUMBER_PATTERN.fullmatch(segment_name)
        if match is None:
            return None
        return int(match.group(1))


class TshttpChannelWorker(BaseChannelWorker):
    def __init__(
        self,
        channel: ChannelConfig,
        settings: Settings,
        file_service: FileService,
        start_gate: WorkerStartGate,
    ) -> None:
        super().__init__(
            channel=channel,
            settings=settings,
            file_service=file_service,
            start_gate=start_gate,
        )
        self._active_consumer: asyncio.Queue[bytes | None] | None = None
        self._consumer_guard = asyncio.Lock()
        self._last_output_at: float | None = None

    def _stdout_target(self) -> int:
        return asyncio.subprocess.PIPE

    def is_consumed(self) -> bool:
        return self._active_consumer is not None

    async def open_stream(self) -> AsyncIterator[bytes]:
        queue_size = self.channel.tshttp.queue_size if self.channel.tshttp is not None else 32
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_size)
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
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(None)

    async def _consume_stdout(self, process: Process, loop: asyncio.AbstractEventLoop) -> None:
        if process.stdout is None:
            return

        while True:
            tshttp = self.channel.tshttp
            chunk_size = tshttp.chunk_size if tshttp is not None else 64 * 1024
            chunk = await process.stdout.read(chunk_size)
            if not chunk:
                break
            self._last_output_at = loop.time()
            try:
                await self._broadcast_chunk(chunk)
            except asyncio.TimeoutError:
                timeout_seconds = (
                    self.channel.tshttp.consumer_write_timeout_seconds
                    if self.channel.tshttp is not None
                    else 10
                )
                self._last_stderr_lines = [
                    f"MPEG-TS consumer blocked for {timeout_seconds}s; restarting"
                ]
                logger.warning("ffmpeg-%s: %s", self.channel.name, self._last_stderr_lines[0])
                process.terminate()
                return

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

        timeout_seconds = (
            self.channel.tshttp.consumer_write_timeout_seconds
            if self.channel.tshttp is not None
            else 10
        )
        await asyncio.wait_for(queue.put(chunk), timeout=timeout_seconds)

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
        self._start_gate = WorkerStartGate(
            max_concurrent_starts=settings.max_concurrent_worker_starts,
            stagger_seconds=settings.worker_start_stagger_seconds,
        )
        self._workers: dict[str, BaseChannelWorker] = {
            channel.name: self._build_worker(channel) for channel in channels
        }
        self._reload_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._file_service.prepare_runtime_root()
        for worker in self._workers.values():
            await worker.start()

    async def stop(self) -> None:
        for worker in self._workers.values():
            await worker.stop()

    async def reload_channel(self, channel_name: str, config_path: Path) -> ChannelStatus | None:
        channel = load_channel_config(config_path, channel_name)
        if channel is None:
            return None

        async with self._reload_lock:
            existing_worker = self._workers.get(channel_name)
            if existing_worker is not None:
                await existing_worker.stop()

            worker = self._build_worker(channel)
            self._channels[channel_name] = channel
            self._workers[channel_name] = worker
            try:
                await worker.start()
            except Exception:
                logger.exception("failed to start reloaded worker for %s", channel_name)
                raise
            return worker.get_status()

    def list_statuses(self) -> list[ChannelStatus]:
        return [self._workers[name].get_status() for name in sorted(self._workers)]

    def count_active_channels(self) -> int:
        return sum(
            1 for worker in self._workers.values() if worker.get_status().state == "running"
        )

    def count_consumed_channels(self) -> int:
        return sum(1 for worker in self._workers.values() if worker.is_consumed())

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
                start_gate=self._start_gate,
            )
        return TshttpChannelWorker(
            channel=channel,
            settings=self._settings,
            file_service=self._file_service,
            start_gate=self._start_gate,
        )
