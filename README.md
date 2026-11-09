# MemorableDay.in

Astro website for MemorableDay.in, preserving the existing landing-page design and assets.

## Website

- Framework: Astro
- Hosting: Vercel
- Build: npm install && npm run build
- Local development: npm run dev

The downloader form keeps the existing UI and calls `/api/v1/inspect` and `/api/v1/download` on its own origin.

## Universal downloader architecture (v3)

The goal: download media from **any source link** — direct media files, extractor sites, and generic webpages — through one frontend that never changes.

Two cooperating backends provide this:

1. **Vercel gateway functions** (`api/v1/*.js`, Node.js) — the frontend's first hop.
   - Direct HTTPS media files (video/audio/image) are inspected and streamed **locally** on Vercel with FFmpeg MP3 conversion. This path works even while the engine is waking up.
   - Everything else — webpages, extractor sites, HTTP links, HLS/DASH manifests — is **forwarded** to the universal engine and the resulting file is streamed back through the same origin.
   - `DOWNLOADER_API_URL` (default `https://memorableday-downloader-api.onrender.com`) selects the engine.

2. **Universal engine** (`server/`, Python/FastAPI on Render) — a three-tier cascade for every URL:
   - **Tier 1 — Direct media probe.** A redirect-validated, SSRF-checked `GET Range` request classifies the URL. Direct video/audio/image files are inspected instantly (filename, size, type) and streamed with byte/time caps. Formats: `original`, plus `mp3` (FFmpeg 192 kbps) for audio/video.
   - **Tier 2 — yt-dlp extraction.** 1800+ extractors (YouTube, Instagram, TikTok, Facebook, Pinterest, Reddit, Vimeo, …) plus yt-dlp's generic extractor, which also handles HLS (`.m3u8`) and DASH (`.mpd`) manifests and many embedded players. Formats: `best`, `p1080`/`p720`/…, `audio`, `mp3`.
   - **Tier 3 — Generic page scraping.** For any other webpage: the HTML is fetched and parsed for `og:video`, `twitter:player:stream`, JSON-LD `contentUrl`, `<video>`/`<audio>`/`<source>`, `<a>` links to media files, raw `.m3u8`/`.mpd` URLs, and images (`og:image`, `<img>`, `srcset`). Candidates are probed in parallel and offered as `media-1`, `media-2`, … (+ `media-N-mp3` variants). If a page only embeds an iframe player, the engine retries extraction on the embed URL.

All tiers share the same API contract, so the frontend never changes:

- `GET /health` — service health, engine version.
- `POST /v1/inspect` — JSON `{"url":"https://..."}` → `{ok, title, contentType, sizeBytes, formats:[{id,label,available}]}`.
- `POST /v1/download` — form fields `url` + `format` → the media file streamed to the browser.

### Safety limits

The engine only accepts public HTTP/HTTPS URLs, rejects local/private IP destinations on every request and redirect, disables playlist downloads, does not load browser cookies, and does not attempt DRM or authentication bypasses. It applies per-IP rate limits, a download-size limit (`MAX_DOWNLOAD_MB`, default 300 MB), a processing-time limit (`MAX_DOWNLOAD_SECONDS`, default 900 s) and an inspect deadline (`INSPECT_BUDGET_SECONDS`, default 70 s). `ALLOW_PRIVATE_TARGETS=true` is a **dev-only** escape hatch for local testing and must never be enabled in production.

Deploy the engine in an isolated environment with restricted network egress before accepting untrusted public traffic; an initial URL check alone cannot fully control every secondary URL requested by third-party extractors.

### Deploy the engine on Render

1. Open the Render Dashboard and choose New → Blueprint.
2. Connect the sandeepdolai/DownloadMediaMemorable repository and select the main branch.
3. Render reads `render.yaml` and builds the Docker service (includes FFmpeg and Deno).
4. Wait for `GET /health` to return `{"ok":true,...,"universal":true}`.
5. If Render assigns a different hostname than `memorableday-downloader-api.onrender.com`, set Vercel's `DOWNLOADER_API_URL` environment variable to that exact URL and redeploy the website. Also update the rewrite destination in `vercel.json`.

The Blueprint uses Render's Free plan for initial testing. Free services spin down after inactivity (the first request then waits ~30-60 s for a cold start; the frontend shows a friendly "engine is waking up" message). Use a paid instance for reliable downloads.

### Build and test the engine locally

~~~sh
cd server
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v test_app
ALLOW_PRIVATE_TARGETS=true uvicorn app:app --host 0.0.0.0 --port 10000
~~~

FFmpeg must be installed for the full conversion feature set. The Dockerfile installs it automatically.

## Compatibility and operating limits

No downloader is literally universal in practice:

- yt-dlp covers a large, changing list of websites; an extractor can stop working when a source website changes.
- Large platforms (YouTube, TikTok, X, Instagram, Facebook) apply bot checks to datacenter IPs, which can block extraction regardless of the code. Success varies by hosting IP reputation, and links requiring login, private access, paid access or DRM are not supported.
- JS-rendered single-page apps expose little media in their initial HTML, so the scraping tier may only find preview images.
- Downloads must be authorized by the content owner and permitted by the source platform's terms.

This version still runs extraction synchronously with bounded in-process concurrency. A Redis-backed job queue, multiple worker instances, object storage and progress polling are future scaling work.
