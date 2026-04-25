# restreamer

Minimal FastAPI service that pulls one or more DASH `.livx` streams with `ffmpeg` and exposes each channel as either HLS or MPEG-TS over HTTP.

## Requirements

- Docker for containerized runtime

## Docker

1. Copy `.env.example` to `.env`.
2. Edit `streams.toml` and define your channels.
3. Build and run with Docker Compose:

```bash
docker compose up --build -d
```

The container:

- installs `ffmpeg`
- installs Python dependencies with `uv`
- exposes the service on port `8092`
- starts one ffmpeg worker per configured channel
- stores generated HLS playlists and segments in a Docker named volume mounted at `/app/runtime`

## Configuration

`.env` contains only global process settings:

- `DEBUG`: when `false`, app logs stay at `info` and ffmpeg runs with `-loglevel quiet`; when `true`, app logs include `debug` and ffmpeg runs with minimal ffmpeg warnings via `-loglevel warning`
- `STREAMS_CONFIG`: startup path to `streams.toml`, default `streams.toml`
- `ACCESS_TOKEN`: bearer token required for protected operational endpoints
- `FFMPEG_THREADS`: limits libx264 video encoder and filter threads per transcoding worker; `0` lets ffmpeg choose automatically
- `WORKER_START_STAGGER_SECONDS`: minimum delay between ffmpeg start attempts across all workers, default `2`
- `MAX_CONCURRENT_WORKER_STARTS`: maximum workers allowed to resolve sources and start ffmpeg at the same time, default `2`

The same `.env` file is also used by Docker Compose for host/container limits:

- `RESTREAMER_BIND_HOST`: host interface for port publishing. Use `127.0.0.1` when Flussonic runs on the same server.
- `RESTREAMER_PORT`: host port mapped to container port `8092`
- `RESTREAMER_CPUS`: maximum CPU cores available to the container
- `RESTREAMER_MEMORY`: container memory limit
- `RESTREAMER_MEMORY_SWAP`: total memory plus swap limit. Set equal to `RESTREAMER_MEMORY` to avoid container swap growth.
- `RESTREAMER_PIDS_LIMIT`: maximum processes/threads in the container. Keep this high enough for all ffmpeg workers; CPU usage is controlled separately by `RESTREAMER_CPUS` and `FFMPEG_THREADS`.
- `RESTREAMER_STOP_GRACE_PERIOD`: time Compose gives the app to stop before SIGKILL
- `RESTREAMER_NOFILE_SOFT` / `RESTREAMER_NOFILE_HARD`: open-file limits

For a shared Flussonic host, start conservatively:

```env
RESTREAMER_BIND_HOST=127.0.0.1
RESTREAMER_CPUS=6.0
RESTREAMER_MEMORY=4g
RESTREAMER_MEMORY_SWAP=4g
RESTREAMER_PIDS_LIMIT=512
FFMPEG_THREADS=2
WORKER_START_STAGGER_SECONDS=2
MAX_CONCURRENT_WORKER_STARTS=2
```

Raise these only after watching Flussonic scheduler/load metrics and `docker stats`.

`streams.toml` contains the channel definitions. Example:

```toml
[channels.kan11]
source_url = "https://n-121-6.il.cdn-redge.media/livedash/oil/kancdn-live/live/kan11/live.livx"
output_format = "tshttp"

[channels.kan11.transcoding]
video = "transcode"
audio = "transcode"
video_width = 1280
video_height = 720
video_bitrate = "2800k"

[channels.kan11.tshttp]
stale_output_seconds = 15
queue_size = 32
chunk_size = 65536
consumer_write_timeout_seconds = 10

[channels.kan23]
source_url = "https://example.invalid/live/kan23/live.livx"
output_format = "hls"

[channels.kan23.transcoding]
video = "copy"
audio = "copy"

[channels.kan23.hls]
segment_time = 4
list_size = 6
delete_threshold = 30
probe_mode = "off"
probe_interval_segments = 30

[channels.keshet12]
source_type = "mako_keshet12"
output_format = "hls"

[channels.keshet12.input]
stream = "clean"
variant = "highest"

[channels.keshet12.transcoding]
video = "copy"
audio = "transcode"

[channels.keshet12.hls]
segment_time = 4
list_size = 30
delete_threshold = 30
probe_mode = "off"
probe_interval_segments = 30
```

`source_type` accepts:

- `static`: default; ffmpeg reads `source_url` directly
- `mako_keshet12`: resolves the official Keshet 12 live playlist and a short-lived Akamai ticket before starting ffmpeg

For long live HLS playlists, `input_live_start_index = -3` can be set on a channel to make ffmpeg
start near the live edge instead of reading from the beginning of the available window.

For `mako_keshet12`, `input.stream` accepts:

- `clean`: the main clean Keshet 12 feed
- `clean_port`: portrait-oriented clean feed
- `standard`: non-SSAI public player feed
- `dvr`: DVR/SSAI public player feed

`input.variant` accepts:

- `highest`: choose the highest resolution advertised by the resolved HLS master
- `720p`: choose a 720p variant if present, otherwise fall back to highest
- `first`: choose the first advertised variant

The official clean Keshet 12 master currently advertises up to `1280x720` at 25 fps. The resolver keeps the upstream Akamai ticket internal; downstream clients only see the local HLS output.

`transcoding.video` and `transcoding.audio` accept:

- `copy`
- `transcode`

Current ffmpeg behavior:

- `video = "copy"` uses `-c:v copy`
- `video = "transcode"` uses `libx264`
- `transcoding.video_width` and `transcoding.video_height` add a fixed `scale=W:H`
- `transcoding.video_bitrate` adds `-b:v`
- `transcoding.video_fps` adds a fixed frame rate and matching GOP for stable HLS segments
- `FFMPEG_THREADS > 0` adds `-threads:v <value>` to video transcode workers and caps ffmpeg filter thread pools
- `audio = "copy"` uses `-c:a copy`
- `audio = "transcode"` uses AAC with the existing resample settings

For Flussonic pull ingest, keep a larger HLS window than a browser player needs. `hls.list_size`
controls how many segments are advertised in the playlist, and `hls.delete_threshold` keeps old
segments on disk briefly after they leave the playlist so a downstream probe does not race segment
cleanup.

HLS playlist advancement checks are always enabled. Expensive media probing is controlled by
`hls.probe_mode`:

- `off`: default; no per-segment ffprobe work
- `periodic`: probe every `hls.probe_interval_segments` new segments
- `every_segment`: strict diagnostic mode matching the older per-segment probe behavior

Segment GET responses are streamed from disk and byte-range responses stream only the requested
range, while preserving the existing HLS headers and status codes.

## Output URLs

TSHTTP channel:

```text
http://<host>:8092/channels/kan11/stream.ts?access_token=<ACCESS_TOKEN>
```

HLS channel:

```text
http://<host>:8092/channels/kan23/hls/index.m3u8?access_token=<ACCESS_TOKEN>
```

## Endpoints

- `GET /health` protected
- `GET /stats` protected; returns `active_channels`, the count of channels currently in `running` state, and `consumed_channels`, the count of channels with a connected TSHTTP client
- `GET /channels` protected
- `GET /channels/{channel}` protected
- `POST /channels/{channel}/reload` protected
- `GET /channels/{channel}/hls` protected
- `GET /channels/{channel}/hls/index.m3u8` protected
- `GET /channels/{channel}/hls/{asset_name}` protected
- `GET /channels/{channel}/stream.ts` protected

The HLS endpoints also support `HEAD`; segment requests support single HTTP byte ranges.

Protected endpoints require:

```text
Authorization: Bearer <ACCESS_TOKEN>
```

They also accept the same token in the URL:

```text
?access_token=<ACCESS_TOKEN>
```

For HLS playback, use the query parameter on the playlist URL. The service adds it to segment URLs inside the playlist so clients can fetch each segment.

`POST /channels/{channel}/reload` reloads a single channel from `streams.toml` without restarting the app. This lets you edit one channel definition and apply it from Swagger UI immediately.

## Troubleshooting

- If startup fails immediately, verify that the container image built successfully and includes `ffmpeg`.
- If the app fails on startup, verify that `/app/streams.toml` exists and defines at least one channel.
- If a worker keeps restarting, verify that the channel `source_url` points to a reachable DASH MPD that `ffmpeg` can read.
- TSHTTP channels allow only one active client per channel at a time. A second client receives `409 Conflict`.
- TSHTTP output uses a bounded queue. If a connected client stops reading for longer than
  `tshttp.consumer_write_timeout_seconds`, the worker restarts instead of dropping MPEG-TS chunks.
