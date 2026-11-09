from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

SERVICE_NAME = "memorableday-downloader-api"
MAX_DOWNLOAD_BYTES = max(1, int(os.getenv("MAX_DOWNLOAD_MB", "300"))) * 1024 * 1024
MAX_DOWNLOAD_SECONDS = max(60, int(os.getenv("MAX_DOWNLOAD_SECONDS", "900")))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
SOCKET_TIMEOUT = max(5, int(os.getenv("YT_DLP_SOCKET_TIMEOUT", "20")))
PROBE_TIMEOUT = max(3.0, float(os.getenv("PROBE_TIMEOUT", "8")))
PAGE_TIMEOUT = max(3.0, float(os.getenv("PAGE_TIMEOUT", "12")))
PAGE_SIZE_LIMIT = 2 * 1024 * 1024
INSPECT_BUDGET_SECONDS = max(30, int(os.getenv("INSPECT_BUDGET_SECONDS", "70")))
REDIRECT_LIMIT = 4
FFMPEG_PATH = shutil.which("ffmpeg")
ALLOW_PRIVATE_TARGETS = os.getenv("ALLOW_PRIVATE_TARGETS", "").strip().lower() in {"1", "true", "yes"}

ALLOWED_ORIGINS = [
    item.strip()
    for item in os.getenv(
        "ALLOWED_ORIGINS",
        "https://memorableday.in,https://www.memorableday.in,https://memorableday-in.vercel.app,http://localhost:4321",
    ).split(",")
    if item.strip()
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".wma", ".aiff"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic", ".tiff"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
MANIFEST_EXTENSIONS = {".m3u8", ".mpd", ".m3u"}

RAW_MANIFEST_PATTERN = re.compile(
    r"""https?://[^\s"'<>{}\\]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>{}\\]*)?""",
    re.IGNORECASE,
)
RAW_MEDIA_FILE_PATTERN = re.compile(
    r"""https?://[^\s"'<>{}\\]+?\.(?:mp4|webm|mov|m4v|mkv|mp3|m4a|aac|ogg|oga|opus|flac|wav|jpg|jpeg|png|gif|webp|avif|bmp)(?:\?[^\s"'<>{}\\]*)?""",
    re.IGNORECASE,
)

app = FastAPI(
    title="MemorableDay Downloader API",
    version="3.0.0",
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
RATE_BUCKETS: dict[tuple[str, str], list[float]] = {}
RATE_LOCK = Lock()


class InspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


def enforce_rate_limit(request: Request, action: str) -> None:
    limits = {"inspect": (20, 60), "download": (6, 60)}
    limit, window_seconds = limits[action]
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    key = (client_ip, action)
    with RATE_LOCK:
        bucket = RATE_BUCKETS.setdefault(key, [])
        while bucket and now - bucket[0] >= window_seconds:
            bucket.pop(0)
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait a moment and try again.",
            )
        bucket.append(now)


def validate_public_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise HTTPException(status_code=400, detail="Enter a valid media link.")

    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid media link.")

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise HTTPException(status_code=400, detail="Only public HTTP or HTTPS links are supported.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Links containing embedded credentials are not supported.")
    if port and port not in {80, 443} and not ALLOW_PRIVATE_TARGETS:
        raise HTTPException(status_code=400, detail="Non-standard ports are not supported.")

    host = hostname.rstrip(".").lower()
    if (
        host in {"localhost", "metadata.google.internal"}
        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
    ):
        raise HTTPException(status_code=400, detail="Private and local network links are not allowed.")

    try:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            records = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = list({ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records})
    except (OSError, ValueError):
        raise HTTPException(status_code=422, detail="The website address could not be resolved.")

    if not ALLOW_PRIVATE_TARGETS and (not addresses or any(not address.is_global for address in addresses)):
        raise HTTPException(status_code=400, detail="Links to private or reserved network addresses are not allowed.")

    return value.strip()


# ---------------------------------------------------------------------------
# yt-dlp extraction (tier 2)
# ---------------------------------------------------------------------------


def _has_video_stream(item: dict) -> bool:
    vcodec = item.get("vcodec")
    if vcodec not in (None, "none"):
        return True
    if vcodec is None:
        return item.get("video_ext") not in (None, "", "none")
    return False


def _has_audio_stream(item: dict) -> bool:
    acodec = item.get("acodec")
    if acodec not in (None, "none"):
        return True
    if acodec is None:
        return item.get("audio_ext") not in (None, "", "none")
    return False


def _video_formats(info: dict) -> list[dict]:
    formats = info.get("formats") or []
    return [item for item in formats if _has_video_stream(item)]


def _audio_formats(info: dict) -> list[dict]:
    formats = info.get("formats") or []
    return [item for item in formats if _has_audio_stream(item)]


def build_format_options(info: dict) -> list[dict[str, str | bool]]:
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise HTTPException(
            status_code=422,
            detail="This link contains a playlist. Please paste a link to one individual video or audio item.",
        )

    video_formats = _video_formats(info)
    audio_formats = _audio_formats(info)
    if not video_formats and (_has_video_stream(info) if isinstance(info, dict) else False):
        video_formats = [info]
    if not audio_formats and (_has_audio_stream(info) if isinstance(info, dict) else False):
        audio_formats = [info]

    heights = sorted(
        {
            int(item["height"])
            for item in video_formats
            if isinstance(item.get("height"), (int, float)) and 0 < int(item["height"]) <= 4320
        },
        reverse=True,
    )

    options: list[dict[str, str | bool]] = []
    if video_formats:
        highest = heights[0] if heights else None
        best_label = f"Best available video ({highest}p)" if highest else "Best available video"
        options.append({"id": "best", "label": best_label, "available": True})
        for height in heights[1:6]:
            options.append({"id": f"p{height}", "label": f"{height}p video", "available": True})
    elif audio_formats:
        options.append({"id": "audio", "label": "Original audio", "available": True})

    if audio_formats:
        options.append({"id": "mp3", "label": "MP3 audio · 192 kbps", "available": True})

    if not options:
        raise HTTPException(
            status_code=422,
            detail="No downloadable audio or video formats were found for this link.",
        )
    return options


def _format_plan(format_id: str, info: dict) -> dict:
    available_ids = {str(item["id"]) for item in build_format_options(info)}
    if format_id not in available_ids:
        raise HTTPException(status_code=400, detail="Choose one of the formats shown for this link.")

    if format_id == "best":
        return {"format": "bestvideo+bestaudio/best"}
    if format_id == "audio":
        return {"format": "bestaudio/best"}
    if format_id == "mp3":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

    match = re.fullmatch(r"p([0-9]{2,4})", format_id)
    if match:
        height = int(match.group(1))
        if not 144 <= height <= 4320:
            raise HTTPException(status_code=400, detail="That video resolution is not available.")
        return {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        }

    raise HTTPException(status_code=400, detail="Choose a supported download format.")


class QuietLogger:
    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def ydl_base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
        "restrictfilenames": True,
        "logger": QuietLogger(),
        "geo_bypass": False,
        "nocheckcertificate": False,
    }


def extract_metadata(url: str) -> dict:
    options = ydl_base_options()
    options["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        raise HTTPException(status_code=422, detail=friendly_extractor_error(str(error)))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="The website could not provide media details. Try another link later.",
        )

    if not isinstance(info, dict):
        raise HTTPException(status_code=422, detail="No media information was returned for this link.")
    return info


def friendly_extractor_error(message: str) -> str:
    lowered = message.lower()
    if any(term in lowered for term in ("private video", "login", "sign in", "authentication", "members-only")):
        return "This media requires an account or permission that MemorableDay does not have."
    if any(term in lowered for term in ("unsupported url", "no suitable extractor", "not a valid url")):
        return "This website or link is not currently supported by the extraction engine."
    if any(term in lowered for term in ("drm", "widevine", "fairplay", "playready")):
        return "This media is DRM-protected and cannot be processed."
    if "http error 403" in lowered or "forbidden" in lowered:
        return "The website refused this request. Its content is not available to this downloader."
    if "video unavailable" in lowered or "not available" in lowered:
        return "This media is unavailable from the source website."
    return "The website could not provide this media. It may be unsupported, unavailable, or access-restricted."


TERMINAL_ERROR_PHRASES = (
    "requires an account or permission",
    "DRM-protected",
    "not available to this downloader",
    "This media is unavailable",
    "contains a playlist",
)


def is_terminal_error(detail: str) -> bool:
    return any(phrase in str(detail) for phrase in TERMINAL_ERROR_PHRASES)


def inspect_media(url: str) -> dict:
    info = extract_metadata(url)
    formats = build_format_options(info)
    raw_formats = info.get("formats") or []
    size_candidates = [
        item.get("filesize") or item.get("filesize_approx")
        for item in raw_formats
        if item.get("filesize") or item.get("filesize_approx")
    ]
    size_bytes = max((int(value) for value in size_candidates if value), default=0) or None

    has_video = bool(_video_formats(info)) or _has_video_stream(info)
    content_type = "video" if has_video else "audio"
    return {
        "ok": True,
        "title": str(info.get("title") or "Media ready")[:180],
        "contentType": content_type,
        "sizeBytes": size_bytes,
        "duration": info.get("duration"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Direct media probing (tier 1)
# ---------------------------------------------------------------------------


def extension_of(url: str) -> str:
    match = re.search(r"\.([a-z0-9]{2,5})$", urlsplit(url).path.lower())
    return f".{match.group(1)}" if match else ""


def kind_from_extension(extension: str) -> str | None:
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in MANIFEST_EXTENSIONS:
        return "manifest"
    return None


MANIFEST_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "application/x-dash+xml",
    "audio/mpegurl",
    "audio/x-mpegurl",
}


def classify_media_kind(content_type: str, extension: str) -> str | None:
    media_type = (content_type or "").split(";")[0].strip().lower()
    # Manifests must be detected before generic video/audio prefixes, because
    # servers commonly label playlists as audio/mpegurl or similar.
    if media_type in MANIFEST_CONTENT_TYPES:
        return "manifest"
    if extension in MANIFEST_EXTENSIONS:
        return "manifest"
    if media_type.startswith("video/"):
        return "video"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("image/"):
        return "image"
    if media_type in {"text/html", "application/xhtml+xml"}:
        return "html"
    if (media_type in {"application/octet-stream", "application/binary", "binary/octet-stream"} or not media_type) and extension:
        return kind_from_extension(extension)
    return None


def open_remote(url: str, method: str, headers: dict | None = None, timeout: float = 8.0, stream: bool = False):
    current = validate_public_url(url)
    for _ in range(REDIRECT_LIMIT + 1):
        try:
            response = requests.request(
                method,
                current,
                allow_redirects=False,
                timeout=(6.0, timeout),
                stream=stream,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})},
            )
        except requests.RequestException:
            raise HTTPException(status_code=502, detail="The media source could not be reached. Check the link and try again.")
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise HTTPException(status_code=502, detail="The media source returned an invalid redirect.")
            current = validate_public_url(urljoin(current, location))
            continue
        return response, current
    raise HTTPException(status_code=502, detail="The media source returned too many redirects.")


@dataclass
class ProbedMedia:
    url: str
    final_url: str
    kind: str
    content_type: str | None
    size_bytes: int | None
    filename: str

    @property
    def extension(self) -> str:
        for candidate in (self.filename, self.final_url):
            extension = extension_of(candidate)
            if extension:
                return extension
        if self.content_type:
            guessed = mimetypes.guess_extension(self.content_type.split(";")[0].strip())
            if guessed:
                return guessed
        return ".media"


def _filename_from_headers(response, final_url: str) -> str:
    disposition = response.headers.get("Content-Disposition") or ""
    candidate = ""
    extended = re.search(r"filename\*=utf-8''([^;]+)", disposition, re.IGNORECASE)
    ordinary = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    if extended:
        try:
            candidate = unquote(extended.group(1))
        except Exception:
            candidate = extended.group(1)
    elif ordinary:
        candidate = ordinary.group(1)
    else:
        try:
            candidate = unquote(urlsplit(final_url).path.rsplit("/", 1)[-1] or "media-file")
        except Exception:
            candidate = "media-file"
    candidate = unicodedata.normalize("NFKC", candidate)
    candidate = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")[:120]
    if not candidate or candidate in {".", ".."}:
        candidate = "media-file"
    return candidate


def probe_media_url(url: str) -> ProbedMedia | None:
    try:
        response, final_url = open_remote(url, "GET", {"Range": "bytes=0-0"}, timeout=PROBE_TIMEOUT, stream=True)
    except HTTPException as error:
        if error.status_code >= 500:
            return None
        raise
    try:
        if response.status_code in {401, 403}:
            return None
        if not (200 <= response.status_code < 300):
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower() or ""
        extension = extension_of(final_url) or extension_of(url)
        kind = classify_media_kind(content_type, extension)
        if kind is None or kind == "html":
            return None

        size_bytes = None
        if response.status_code == 206:
            range_total = re.search(r"/(\d+)$", response.headers.get("Content-Range") or "")
            if range_total:
                size_bytes = int(range_total.group(1))
        else:
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                size_bytes = int(length)

        filename = _filename_from_headers(response, final_url)
        return ProbedMedia(
            url=url,
            final_url=final_url,
            kind=kind,
            content_type=content_type or None,
            size_bytes=size_bytes,
            filename=filename,
        )
    finally:
        response.close()


def direct_inspect_result(media: ProbedMedia) -> dict:
    extension_label = media.extension.lstrip(".").upper() or "FILE"
    formats: list[dict[str, str | bool]] = [
        {"id": "original", "label": f"Original file ({extension_label})", "available": True}
    ]
    if media.kind in {"video", "audio"} and FFMPEG_PATH:
        formats.append({"id": "mp3", "label": "MP3 audio · 192 kbps", "available": True})
    title = Path(media.filename).stem or "Media file"
    return {
        "ok": True,
        "title": title[:180],
        "contentType": media.kind,
        "sizeBytes": media.size_bytes,
        "duration": None,
        "extractor": "direct-media",
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Generic HTML page scraping (tier 3)
# ---------------------------------------------------------------------------


def _first_srcset_url(value: str) -> str:
    if not value:
        return ""
    first_candidate = value.split(",")[0].strip()
    return first_candidate.split()[0] if first_candidate else ""


class _MediaCollector(HTMLParser):
    """Collects candidate media URLs from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[int, str]] = []
        self.seen: set[str] = set()
        self.iframes: list[str] = []
        self.title = ""
        self.og_title = ""
        self.base_href = ""
        self.jsonld_blocks: list[str] = []
        self._av_depth = 0
        self._in_jsonld = False
        self._jsonld_buffer: list[str] = []
        self._in_title = False
        self._title_buffer: list[str] = []

    def _add(self, priority: int, value: str | None) -> None:
        if not value:
            return
        value = value.strip()
        if not value or value.startswith(("data:", "javascript:", "blob:", "#", "mailto:", "tel:")):
            return
        if value in self.seen:
            return
        self.seen.add(value)
        self.entries.append((priority, value))

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "base":
            if attributes.get("href") and not self.base_href:
                self.base_href = attributes["href"].strip()
        elif tag in {"video", "audio"}:
            self._av_depth += 1
            self._add(1, attributes.get("src"))
        elif tag == "source":
            if self._av_depth > 0:
                self._add(1, attributes.get("src"))
            else:
                candidate = attributes.get("src") or _first_srcset_url(attributes.get("srcset") or attributes.get("data-srcset"))
                self._add(4, candidate)
        elif tag == "a":
            href = attributes.get("href")
            if href and extension_of(urljoin("https://example.invalid", href)) in MEDIA_EXTENSIONS:
                self._add(3, href)
        elif tag == "img":
            candidate = (
                attributes.get("src")
                or attributes.get("data-src")
                or _first_srcset_url(attributes.get("srcset") or attributes.get("data-srcset"))
            )
            self._add(4, candidate)
        elif tag == "meta":
            prop = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content") or ""
            if prop in {"og:video", "og:video:secure_url", "og:video:url", "twitter:player:stream"}:
                self._add(0, content)
            elif prop in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
                self._add(4, content)
            elif prop in {"og:title", "twitter:title"} and not self.og_title:
                self.og_title = content.strip()
        elif tag == "iframe":
            src = attributes.get("src") or ""
            if src.startswith(("http://", "https://", "//")):
                self.iframes.append(src.strip())
        elif tag == "script":
            if "ld+json" in (attributes.get("type") or "").lower():
                self._in_jsonld = True
                self._jsonld_buffer = []
        elif tag == "title":
            self._in_title = True
            self._title_buffer = []

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag in {"video", "audio"} and self._av_depth > 0:
            self._av_depth -= 1
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld_blocks.append("".join(self._jsonld_buffer))
            self._jsonld_buffer = []
        elif tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title_buffer).strip()

    def handle_data(self, data) -> None:
        if self._in_jsonld:
            self._jsonld_buffer.append(data)
        elif self._in_title:
            self._title_buffer.append(data)


def _jsonld_media_urls(blocks: list[str]) -> list[str]:
    urls: list[str] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in ("contentUrl", "embedUrl"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        urls.append(value.strip())
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
    return urls


def _clean_raw_url(value: str) -> str:
    return value.rstrip("),.;\"'!?]")


@dataclass
class PageMedia:
    entries: list[tuple[int, str]]  # (priority, absolute_url) in deterministic order
    title: str
    iframes: list[str]


def find_page_media(html: str, base_url: str) -> PageMedia:
    collector = _MediaCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        pass

    base = collector.base_href or base_url

    combined: list[tuple[int, int, str]] = []
    seen_absolute: set[str] = set()

    def _add_absolute(priority: int, raw_value: str) -> None:
        if not raw_value:
            return
        try:
            absolute = urljoin(base, raw_value)
        except Exception:
            return
        if not absolute.startswith(("http://", "https://")):
            return
        if absolute in seen_absolute:
            return
        seen_absolute.add(absolute)
        combined.append((priority, len(combined), absolute))

    for jsonld_url in _jsonld_media_urls(collector.jsonld_blocks):
        _add_absolute(0, jsonld_url)
    for priority, raw_value in collector.entries:
        _add_absolute(priority, raw_value)
    for match in RAW_MANIFEST_PATTERN.findall(html) or []:
        _add_absolute(2, _clean_raw_url(match))
    for match in RAW_MEDIA_FILE_PATTERN.findall(html) or []:
        _add_absolute(3, _clean_raw_url(match))

    entries = [(priority, absolute) for priority, _order, absolute in sorted(combined, key=lambda item: (item[0], item[1]))]
    title = (collector.og_title or collector.title or "").strip()
    return PageMedia(entries=entries, title=title, iframes=list(dict.fromkeys(collector.iframes)))


def fetch_page(url: str) -> tuple[str, str] | None:
    try:
        response, final_url = open_remote(
            url,
            "GET",
            {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=PAGE_TIMEOUT,
            stream=True,
        )
    except HTTPException:
        return None
    try:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if response.status_code >= 400 or "html" not in content_type and content_type:
            return None
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            chunks.append(chunk)
            if total >= PAGE_SIZE_LIMIT:
                break
        if not chunks:
            return None
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", response.headers.get("Content-Type") or "", re.IGNORECASE)
        if match:
            charset = match.group(1)
        html = b"".join(chunks).decode(charset, errors="replace")
        return html, final_url
    except Exception:
        return None
    finally:
        response.close()


def _safe_probe(url: str) -> ProbedMedia | None:
    try:
        return probe_media_url(url)
    except Exception:
        return None


def probe_candidates(entries: list[tuple[int, str]], deadline: float, limit: int = 8) -> list[ProbedMedia]:
    probed: list[ProbedMedia] = []
    targets = entries[: 10]
    if not targets:
        return probed
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = [pool.submit(_safe_probe, absolute) for _priority, absolute in targets]
        remaining = max(1.0, deadline - time.monotonic())
        futures_wait(futures, timeout=remaining)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for future, (_priority, absolute) in zip(futures, targets):
        if not future.done() or future.cancelled():
            continue
        try:
            media = future.result()
        except Exception:
            media = None
        if media is not None and media.kind in {"video", "audio", "image", "manifest"}:
            probed.append(media)
        if len(probed) >= limit:
            break
    return probed


def _human_size(size_bytes: int | None) -> str:
    if not size_bytes or size_bytes <= 0:
        return ""
    units = ["B", "KB", "MB", "GB"]
    amount = float(size_bytes)
    unit = 0
    while amount >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    return f"{amount:.0f} B" if unit == 0 else f"{amount:.1f} {units[unit]}"


def build_scraped_formats(probed: list[ProbedMedia]) -> list[dict[str, str | bool]]:
    options: list[dict[str, str | bool]] = []
    for index, media in enumerate(probed[:8], start=1):
        extension_label = media.extension.lstrip(".").upper() or "FILE"
        if media.kind == "manifest":
            options.append({"id": f"media-{index}", "label": f"Stream item {index} · best quality", "available": True})
            if FFMPEG_PATH:
                options.append({"id": f"media-{index}-mp3", "label": f"MP3 audio · 192 kbps · item {index}", "available": True})
            continue
        kind_name = {"video": "Video", "audio": "Audio", "image": "Image"}[media.kind]
        size_label = _human_size(media.size_bytes)
        label = f"{kind_name} {index} · {extension_label}"
        if size_label:
            label += f" · {size_label}"
        options.append({"id": f"media-{index}", "label": label, "available": True})
        if media.kind in {"video", "audio"} and FFMPEG_PATH:
            options.append({"id": f"media-{index}-mp3", "label": f"MP3 audio · 192 kbps · item {index}", "available": True})
    return options[:12]


def _iframe_fallback(iframes: list[str], base_url: str, title: str, deadline: float) -> dict | None:
    checked = 0
    for iframe in iframes:
        if checked >= 2 or time.monotonic() >= deadline:
            break
        absolute = urljoin(base_url, iframe)
        try:
            validate_public_url(absolute)
        except HTTPException:
            continue
        try:
            result = inspect_media(absolute)
        except HTTPException:
            checked += 1
            continue
        if title and not (result.get("title") or "").strip():
            result["title"] = title[:180]
        return result
    return None


def scrape_page_media(url: str, deadline: float) -> dict | None:
    page = fetch_page(url)
    if page is None:
        return None
    html, final_url = page
    page_media = find_page_media(html, final_url)
    title = page_media.title

    probed = probe_candidates(page_media.entries, deadline)
    if probed:
        formats = build_scraped_formats(probed)
        if formats:
            size_bytes = next((media.size_bytes for media in probed if media.size_bytes), None)
            return {
                "ok": True,
                "title": (title or "Media found on this page")[:180],
                "contentType": probed[0].kind,
                "sizeBytes": size_bytes,
                "duration": None,
                "extractor": "page-media",
                "formats": formats,
            }

    if page_media.iframes:
        fallback = _iframe_fallback(page_media.iframes, final_url, title, deadline)
        if fallback is not None:
            return fallback
    return None


# ---------------------------------------------------------------------------
# Universal orchestration
# ---------------------------------------------------------------------------


def inspect_universal(url: str) -> dict:
    deadline = time.monotonic() + INSPECT_BUDGET_SECONDS

    direct = probe_media_url(url)
    if direct is not None and direct.kind in {"video", "audio", "image"}:
        return direct_inspect_result(direct)

    tier2_error: HTTPException | None = None
    if direct is None or direct.kind in {"manifest", "html"}:
        try:
            return inspect_media(url)
        except HTTPException as error:
            if error.status_code not in {402, 403, 422, 429, 500, 502, 503, 504}:
                raise
            tier2_error = error
            if error.status_code == 429 or is_terminal_error(error.detail):
                raise

    if time.monotonic() >= deadline:
        if tier2_error is not None:
            raise tier2_error
        raise HTTPException(status_code=504, detail="Checking this link is taking too long. Please try again.")

    scraped = scrape_page_media(url, deadline)
    if scraped is not None:
        return scraped

    if tier2_error is not None:
        raise tier2_error
    raise HTTPException(status_code=422, detail="No downloadable media was found at this link.")


def safe_filename(value: str, extension: str) -> str:
    title = unicodedata.normalize("NFKC", value or "memorableday-download")
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" .")[:110]
    if not title:
        title = "memorableday-download"
    suffix = extension if extension.startswith(".") else f".{extension}" if extension else ".media"
    return f"{title}{suffix.lower()}"


def download_to_temp(url: str, format_id: str) -> tuple[str, str, str, str]:
    metadata = extract_metadata(url)
    plan = _format_plan(format_id, metadata)
    temp_dir = tempfile.mkdtemp(prefix="memorableday-")
    output_template = str(Path(temp_dir) / "download.%(ext)s")
    started = time.monotonic()

    def progress_hook(data: dict) -> None:
        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
            raise yt_dlp.utils.DownloadError("The download exceeded the configured time limit.")
        downloaded = int(data.get("downloaded_bytes") or 0)
        if downloaded > MAX_DOWNLOAD_BYTES:
            raise yt_dlp.utils.DownloadError("The download exceeded the configured file-size limit.")

    options = ydl_base_options()
    options.update(
        {
            "format": plan["format"],
            "outtmpl": output_template,
            "max_filesize": MAX_DOWNLOAD_BYTES,
            "nopart": True,
            "overwrites": True,
            "progress_hooks": [progress_hook],
        }
    )
    if plan.get("postprocessors"):
        options["postprocessors"] = plan["postprocessors"]

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(url, download=True)

        candidates = [
            item
            for item in Path(temp_dir).glob("download.*")
            if item.is_file() and item.suffix not in {".part", ".ytdl"}
        ]
        if not candidates:
            raise HTTPException(status_code=502, detail="The source did not produce a downloadable file.")
        output_path = max(candidates, key=lambda item: item.stat().st_size)
        size = output_path.stat().st_size
        if size <= 0:
            raise HTTPException(status_code=502, detail="The downloaded file is empty.")
        if size > MAX_DOWNLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The media exceeds the current download-size limit.")

        title = str(metadata.get("title") or "memorableday-download")
        filename = safe_filename(title, output_path.suffix)
        media_type = mimetypes.guess_type(output_path.name)[0] or "application/octet-stream"
        return temp_dir, str(output_path), filename, media_type
    except yt_dlp.utils.DownloadError as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=friendly_extractor_error(str(error)))
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail="The media could not be downloaded. Please try again.")


def direct_download_to_temp(media: ProbedMedia, want_mp3: bool) -> tuple[str, str, str, str]:
    temp_dir = tempfile.mkdtemp(prefix="memorableday-")
    started = time.monotonic()
    try:
        response, _final_url = open_remote(media.final_url, "GET", timeout=90.0, stream=True)
        try:
            length_header = response.headers.get("Content-Length")
            if length_header and length_header.isdigit() and int(length_header) > MAX_DOWNLOAD_BYTES:
                raise HTTPException(status_code=413, detail="The media exceeds the current download-size limit.")
            extension = media.extension
            input_path = Path(temp_dir) / f"download{extension}"
            total = 0
            with input_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="The media exceeds the current download-size limit.")
                    if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                        raise HTTPException(status_code=422, detail="The download exceeded the configured time limit.")
                    handle.write(chunk)
            if total <= 0:
                raise HTTPException(status_code=502, detail="The downloaded file is empty.")
        finally:
            response.close()

        if want_mp3:
            if not FFMPEG_PATH:
                raise HTTPException(status_code=503, detail="MP3 conversion is unavailable on this deployment.")
            output_path = Path(temp_dir) / "download.mp3"
            try:
                subprocess.run(
                    [
                        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(input_path),
                        "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
                        str(output_path),
                    ],
                    check=True,
                    timeout=min(MAX_DOWNLOAD_SECONDS, 600),
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                raise HTTPException(status_code=422, detail="This file does not contain audio that can be converted to MP3.")
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=504, detail="The MP3 conversion took too long and was stopped.")
            input_path.unlink(missing_ok=True)
            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise HTTPException(status_code=502, detail="The media could not be converted to MP3 audio.")
            base_title = Path(media.filename or "memorableday-download").stem or "memorableday-download"
            return temp_dir, str(output_path), safe_filename(base_title, "mp3"), "audio/mpeg"

        base_title = Path(media.filename or "media-file").stem or "media-file"
        media_type = media.content_type or mimetypes.guess_type(input_path.name)[0] or "application/octet-stream"
        return temp_dir, str(input_path), safe_filename(base_title, extension), media_type
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail="The media could not be downloaded. Please try again.")


def resolve_scraped_candidate(url: str, index: int) -> ProbedMedia:
    deadline = time.monotonic() + INSPECT_BUDGET_SECONDS
    page = fetch_page(url)
    if page is None:
        raise HTTPException(status_code=422, detail="The page could not be loaded again. Please re-check the link.")
    html, final_url = page
    page_media = find_page_media(html, final_url)
    probed = probe_candidates(page_media.entries, deadline)
    if index < 1 or index > len(probed):
        raise HTTPException(
            status_code=400,
            detail="Choose one of the formats shown for this link. The page may have changed — please check the link again.",
        )
    return probed[index - 1]


def download_universal(url: str, format_id: str) -> tuple[str, str, str, str]:
    if format_id in {"best", "audio"} or re.fullmatch(r"p[0-9]{2,4}", format_id):
        return download_to_temp(url, format_id)

    if format_id == "mp3":
        direct = _safe_probe(url)
        if direct is not None and direct.kind in {"video", "audio"}:
            return direct_download_to_temp(direct, want_mp3=True)
        return download_to_temp(url, "mp3")

    if format_id == "original":
        direct = _safe_probe(url)
        if direct is not None and direct.kind in {"video", "audio", "image"}:
            return direct_download_to_temp(direct, want_mp3=False)
        return download_to_temp(url, "best")

    scraped_match = re.fullmatch(r"media-([0-9]+)(-mp3)?", format_id)
    if scraped_match:
        index = int(scraped_match.group(1))
        want_mp3 = bool(scraped_match.group(2))
        candidate = resolve_scraped_candidate(url, index)
        if candidate.kind == "manifest":
            return download_to_temp(candidate.final_url, "mp3" if want_mp3 else "best")
        return direct_download_to_temp(candidate, want_mp3=want_mp3)

    raise HTTPException(status_code=400, detail="Choose a supported download format.")


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


async def parse_download_request(request: Request) -> tuple[str, str]:
    if request.headers.get("content-length") and int(request.headers["content-length"]) > 16_384:
        raise HTTPException(status_code=413, detail="The download request is too large.")
    body = await request.body()
    content_type = request.headers.get("content-type", "").lower()
    try:
        if "application/json" in content_type:
            payload = json.loads(body or b"{}")
        else:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            payload = {key: items[-1] for key, items in values.items()}
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="The download request is invalid.")

    url = payload.get("url") if isinstance(payload, dict) else None
    format_id = payload.get("format", "best") if isinstance(payload, dict) else "best"
    if not isinstance(url, str) or not isinstance(format_id, str):
        raise HTTPException(status_code=400, detail="Provide a media link and format.")
    return validate_public_url(url), format_id


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": str(error.detail)},
        headers=error.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, _error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "The download service encountered a problem. Please try again."},
    )


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "version": "3.0.0",
        "universal": True,
        "engine": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": bool(FFMPEG_PATH),
    }


@app.post("/v1/inspect")
async def inspect_endpoint(payload: InspectRequest, request: Request) -> dict:
    enforce_rate_limit(request, "inspect")
    url = validate_public_url(payload.url)
    return await asyncio.to_thread(inspect_universal, url)


@app.post("/v1/download")
async def download_endpoint(request: Request):
    enforce_rate_limit(request, "download")
    url, format_id = await parse_download_request(request)

    async with DOWNLOAD_SEMAPHORE:
        temp_dir, output_path, filename, media_type = await asyncio.to_thread(download_universal, url, format_id)

    return FileResponse(
        output_path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )
