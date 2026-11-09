# MemorableDay.in

Astro website for MemorableDay.in. The existing landing-page design and assets are preserved.

## Architecture

- Frontend: Astro on Vercel.
- Downloader API: FastAPI + yt-dlp on Render.
- Public API path: `https://memorableday.in/api`.
- Vercel forwards `/api/*` to the active Render service using the external rewrite in `vercel.json`.
- The browser calls the same-origin `/api` path. Do not set a separate browser-facing Render URL.

## Downloader API

The Python service lives in `server/` and uses:

- FastAPI for the HTTP API.
- yt-dlp for websites supported by its current extractors.
- FFmpeg for merging separate audio/video streams and converting audio to MP3.
- Deno plus yt-dlp-ejs for YouTube JavaScript challenge support.
- Temporary local files with automatic cleanup after the browser receives the download.

### Endpoints

- `GET /health` — service health and extraction-engine version.
- `POST /v1/inspect` — accepts JSON `{"url":"https://..."}` and returns media details plus available formats.
- `POST /v1/download` — accepts form fields `url` and `format`, then returns the generated file.

The public production paths are:

- `https://memorableday.in/api/health`
- `https://memorableday.in/api/v1/inspect`
- `https://memorableday.in/api/v1/download`

### Format IDs

- `best` — best available video and audio combination.
- `p1080`, `p720`, etc. — video at or below the selected resolution when available.
- `audio` — original audio.
- `mp3` — audio converted to MP3 at 192 kbps.

## Tests

The GitHub Actions workflow validates the Vercel-to-Render route configuration, builds the downloader Docker image (including its Python unit tests), performs an API health smoke test, and builds the Astro frontend.

Run backend unit tests locally:

```sh
cd server
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v test_app
```

## Deployment

The API is deployed as the Render web service `memorableday-downloader-api-id9q`. Its service URL is `https://memorableday-downloader-api-id9q.onrender.com`.

The Vercel project uses the external rewrite in `vercel.json` to forward public `/api/*` requests to this Render service. Keep the rewrite destination synchronized if Render ever changes the service hostname.

## Compatibility and operational limits

yt-dlp supports many websites, not every website. Extractors can stop working when source websites change. Links that require private access, paid access, login cookies, or DRM bypass are not supported. Users should only download content they own or are authorized to save, in compliance with the source platform's terms.

The current backend processes downloads synchronously with bounded in-process concurrency. It has a 300 MB file limit and a 15-minute processing limit. It does not yet use a durable job queue or external object storage.

**Render Free is suitable for testing, not production availability.** Free web services spin down after 15 minutes without inbound traffic and typically take about one minute to start again. Their local filesystem is ephemeral and the service may restart. This can interrupt downloads. Keep the current free setup for testing; production-grade, always-on operation requires infrastructure that does not sleep and a tested download-delivery design.
