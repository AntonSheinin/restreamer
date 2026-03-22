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

## Configuration

`.env` contains only global process settings:

- `FFMPEG_LOGLEVEL`: ffmpeg stderr verbosity, default `info`
- `STREAMS_CONFIG`: startup path to `streams.toml`, default `streams.toml`

`streams.toml` contains the channel definitions. Example:

```toml
[channels.kan11]
source_url = "https://n-121-6.il.cdn-redge.media/livedash/oil/kancdn-live/live/kan11/live.livx"
output_format = "tshttp"

[channels.kan11.transcoding]
video = "copy"
audio = "transcode"

[channels.kan11.tshttp]
stale_output_seconds = 15

[channels.kan23]
source_url = "https://example.invalid/live/kan23/live.livx"
output_format = "hls"

[channels.kan23.transcoding]
video = "copy"
audio = "copy"

[channels.kan23.hls]
segment_time = 4
list_size = 6
```

`transcoding.video` and `transcoding.audio` accept:

- `copy`
- `transcode`

Current ffmpeg behavior:

- `video = "copy"` uses `-c:v copy`
- `video = "transcode"` uses `libx264`
- `audio = "copy"` uses `-c:a copy`
- `audio = "transcode"` uses AAC with the existing resample settings

## Output URLs

TSHTTP channel:

```text
tshttp://<host>:8092/channels/kan11/stream.ts
```

HLS channel:

```text
http://<host>:8092/channels/kan23/hls/index.m3u8
```

## Endpoints

- `GET /health`
- `GET /channels`
- `GET /channels/{channel}`
- `GET /channels/{channel}/hls/index.m3u8`
- `GET /channels/{channel}/hls/{asset_name}`
- `GET /channels/{channel}/stream.ts`

## Troubleshooting

- If startup fails immediately, verify that the container image built successfully and includes `ffmpeg`.
- If the app fails on startup, verify that `/app/streams.toml` exists and defines at least one channel.
- If a worker keeps restarting, verify that the channel `source_url` points to a reachable DASH MPD that `ffmpeg` can read.
- TSHTTP channels allow only one active client per channel at a time. A second client receives `409 Conflict`.
