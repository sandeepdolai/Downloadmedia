import unittest

from fastapi import HTTPException

from app import (
    build_format_options,
    build_scraped_formats,
    classify_media_kind,
    find_page_media,
    _format_plan,
    _safe_probe,
    validate_public_url,
    validate_cookie_text,
    validate_user_agent,
    ydl_base_options,
    ProbedMedia,
)


class UrlValidationTests(unittest.TestCase):
    def test_rejects_file_scheme(self):
        with self.assertRaises(HTTPException):
            validate_public_url("file:///etc/passwd")

    def test_rejects_localhost(self):
        with self.assertRaises(HTTPException):
            validate_public_url("http://localhost/video.mp4")

    def test_rejects_private_ip(self):
        with self.assertRaises(HTTPException):
            validate_public_url("http://127.0.0.1/video.mp4")

    def test_rejects_private_range(self):
        with self.assertRaises(HTTPException):
            validate_public_url("http://192.168.1.5/video.mp4")


class FormatSelectionTests(unittest.TestCase):
    def setUp(self):
        self.info = {
            "title": "Sample video",
            "formats": [
                {"format_id": "v1080", "vcodec": "avc1", "acodec": "none", "height": 1080},
                {"format_id": "v720", "vcodec": "avc1", "acodec": "none", "height": 720},
                {"format_id": "a128", "vcodec": "none", "acodec": "mp4a", "abr": 128},
            ],
        }

    def test_returns_quality_and_mp3_options(self):
        options = build_format_options(self.info)
        ids = [item["id"] for item in options]
        self.assertEqual(ids, ["best", "p720", "mp3"])

    def test_best_selection_uses_separate_audio_and_video(self):
        plan = _format_plan("best", self.info)
        self.assertEqual(plan["format"], "bestvideo+bestaudio/best")

    def test_rejects_unlisted_format(self):
        with self.assertRaises(HTTPException):
            _format_plan("p4320", self.info)


class MediaKindClassificationTests(unittest.TestCase):
    def test_video_content_type(self):
        self.assertEqual(classify_media_kind("video/mp4", ".mp4"), "video")

    def test_audio_content_type(self):
        self.assertEqual(classify_media_kind("audio/mpeg", ".mp3"), "audio")

    def test_image_content_type(self):
        self.assertEqual(classify_media_kind("image/jpeg", ".jpg"), "image")

    def test_html_is_not_direct_media(self):
        self.assertEqual(classify_media_kind("text/html; charset=utf-8", ""), "html")
        # even when the URL looks like a media file, an HTML response means "webpage"
        self.assertEqual(classify_media_kind("text/html; charset=utf-8", ".mp4"), "html")

    def test_manifest_extension(self):
        self.assertEqual(classify_media_kind("application/vnd.apple.mpegurl", ".m3u8"), "manifest")

    def test_manifest_content_type(self):
        self.assertEqual(classify_media_kind("application/vnd.apple.mpegurl", ""), "manifest")
        # Some CDNs (e.g. mux.dev) label playlists audio/mpegurl — must still
        # be treated as a manifest, not as a direct audio file.
        self.assertEqual(classify_media_kind("audio/mpegurl", ".m3u8"), "manifest")
        self.assertEqual(classify_media_kind("audio/x-mpegurl", ""), "manifest")
        self.assertEqual(classify_media_kind("application/dash+xml", ""), "manifest")

    def test_octet_stream_with_media_extension(self):
        self.assertEqual(classify_media_kind("application/octet-stream", ".mp4"), "video")
        self.assertEqual(classify_media_kind("application/octet-stream", ".flac"), "audio")

    def test_unknown_type_returns_none(self):
        self.assertIsNone(classify_media_kind("text/plain", ".txt"))
        self.assertIsNone(classify_media_kind("", ""))


class PageScrapingTests(unittest.TestCase):
    SAMPLE_HTML = """
    <html>
      <head>
        <title>Sample Blog Post</title>
        <meta property="og:title" content="Sample Blog Video" />
        <meta property="og:video" content="https://cdn.example.com/embed/clip.mp4" />
        <meta property="og:image" content="https://cdn.example.com/cover.jpg" />
        <script type="application/ld+json">
          {"@type": "VideoObject", "contentUrl": "https://cdn.example.com/ld/lecture.mp4"}
        </script>
        <script>
          var player = new Player({source: "https://stream.example.com/live/master.m3u8?token=abc"});
        </script>
      </head>
      <body>
        <video controls poster="https://cdn.example.com/poster.png">
          <source src="/media/hero.webm" type="video/webm" />
        </video>
        <a href="https://files.example.com/downloads/interview.mp3">Download audio</a>
        <a href="/articles/next-page">Read more</a>
        <img src="https://cdn.example.com/photos/sunset.jpg" srcset="https://cdn.example.com/photos/sunset-2x.jpg 2x" />
        <iframe src="https://www.youtube.com/embed/aqz-KE-bpKQ"></iframe>
      </body>
    </html>
    """

    def test_collects_media_candidates_in_priority_order(self):
        page = find_page_media(self.SAMPLE_HTML, "https://www.example.com/post/1")
        urls = [url for _priority, url in page.entries]
        # og:video and JSON-LD contentUrl are priority 0
        self.assertIn("https://cdn.example.com/embed/clip.mp4", urls)
        self.assertIn("https://cdn.example.com/ld/lecture.mp4", urls)
        # inline <video>/<source> is priority 1 and resolves relative URLs
        self.assertIn("https://www.example.com/media/hero.webm", urls)
        # raw manifest regex is priority 2 (query string preserved)
        self.assertIn("https://stream.example.com/live/master.m3u8?token=abc", urls)
        # <a href> to a media file is priority 3, page links are skipped
        self.assertIn("https://files.example.com/downloads/interview.mp3", urls)
        self.assertNotIn("https://www.example.com/articles/next-page", urls)
        # images are priority 4
        self.assertIn("https://cdn.example.com/photos/sunset.jpg", urls)
        self.assertIn("https://cdn.example.com/cover.jpg", urls)

        priorities = {url: priority for priority, url in page.entries}
        self.assertEqual(priorities["https://cdn.example.com/embed/clip.mp4"], 0)
        self.assertEqual(priorities["https://www.example.com/media/hero.webm"], 1)
        self.assertEqual(priorities["https://stream.example.com/live/master.m3u8?token=abc"], 2)
        self.assertEqual(priorities["https://files.example.com/downloads/interview.mp3"], 3)
        self.assertEqual(priorities["https://cdn.example.com/photos/sunset.jpg"], 4)

    def test_priority_ordering_beats_dom_order(self):
        page = find_page_media(self.SAMPLE_HTML, "https://www.example.com/post/1")
        first_urls = [url for priority, url in page.entries if priority == 0]
        self.assertEqual(len(first_urls), 2)

    def test_title_prefers_open_graph(self):
        page = find_page_media(self.SAMPLE_HTML, "https://www.example.com/post/1")
        self.assertEqual(page.title, "Sample Blog Video")

    def test_iframes_collected_for_embed_fallback(self):
        page = find_page_media(self.SAMPLE_HTML, "https://www.example.com/post/1")
        self.assertEqual(page.iframes, ["https://www.youtube.com/embed/aqz-KE-bpKQ"])

    def test_relative_base_href_is_respected(self):
        html = '<html><head><base href="https://static.example.com/assets/"></head>' \
               '<body><video src="clip.mp4"></video></body></html>'
        page = find_page_media(html, "https://www.example.com/page")
        self.assertIn("https://static.example.com/assets/clip.mp4", [url for _p, url in page.entries])

    def test_data_and_javascript_uris_skipped(self):
        html = '<body><img src="data:image/png;base64,AAAA"><a href="javascript:void(0)">x</a>' \
               '<video src="blob:https://example.com/uuid"></video></body>'
        page = find_page_media(html, "https://www.example.com/page")
        self.assertEqual(page.entries, [])


class ScrapedFormatTests(unittest.TestCase):
    def _probed(self, kind, ext, size=None):
        filename = f"sample{ext}"
        return ProbedMedia(
            url="https://cdn.example.com/x" + ext,
            final_url="https://cdn.example.com/x" + ext,
            kind=kind,
            content_type=None,
            size_bytes=size,
            filename=filename,
        )

    def test_builds_media_ids_and_mp3_variants(self):
        probed = [self._probed("video", ".mp4", 12_000_000), self._probed("image", ".jpg", 300_000)]
        options = build_scraped_formats(probed)
        ids = [item["id"] for item in options]
        self.assertIn("media-1", ids)
        self.assertIn("media-1-mp3", ids)
        self.assertIn("media-2", ids)
        self.assertNotIn("media-2-mp3", ids)  # images have no mp3 variant

    def test_labels_include_kind_extension_and_size(self):
        probed = [self._probed("video", ".mp4", 12_000_000)]
        options = build_scraped_formats(probed)
        self.assertEqual(options[0]["label"], "Video 1 · MP4 · 11.4 MB")

    def test_manifest_label(self):
        probed = [self._probed("manifest", ".m3u8")]
        options = build_scraped_formats(probed)
        self.assertEqual(options[0]["label"], "Stream item 1 · best quality")
        self.assertEqual(options[1]["id"], "media-1-mp3")

    def test_caps_total_options(self):
        probed = [self._probed("video", f".mp{i}", None) for i in range(1, 10)]
        options = build_scraped_formats(probed)
        self.assertLessEqual(len(options), 12)


class EngineEnvironmentTests(unittest.TestCase):
    def test_safe_probe_swallows_errors(self):
        # Invalid host cannot resolve; _safe_probe must return None, not raise.
        self.assertIsNone(_safe_probe("https://this-domain-definitely-does-not-exist-zzz.invalid/x.mp4"))


class SessionCookieTests(unittest.TestCase):
    COOKIE_TEXT = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tsample-token\n"
    )

    def test_accepts_netscape_cookie_file_and_normalizes_newlines(self):
        value = validate_cookie_text(self.COOKIE_TEXT.replace("\n", "\r\n"))
        self.assertEqual(value, self.COOKIE_TEXT)

    def test_rejects_json_cookie_export(self):
        with self.assertRaises(HTTPException):
            validate_cookie_text('{"cookies": []}')

    def test_rejects_empty_netscape_file(self):
        with self.assertRaises(HTTPException):
            validate_cookie_text("# Netscape HTTP Cookie File\n# exported file\n")

    def test_rejects_cookie_file_over_size_limit(self):
        huge = "# Netscape HTTP Cookie File\n" + ("x" * (64 * 1024))
        with self.assertRaises(HTTPException):
            validate_cookie_text(huge)

    def test_accepts_browser_user_agent(self):
        value = "Mozilla/5.0 (Android 10; Mobile) Chrome/131.0.0.0"
        self.assertEqual(validate_user_agent(value), value)

    def test_rejects_control_characters_in_user_agent(self):
        with self.assertRaises(HTTPException):
            validate_user_agent("Mozilla/5.0\\nInjected: header")

    def test_yt_dlp_options_receive_cookie_file_and_user_agent(self):
        options = ydl_base_options("/tmp/session.txt", "Mozilla/5.0 test")
        self.assertEqual(options["cookiefile"], "/tmp/session.txt")
        self.assertEqual(options["http_headers"]["User-Agent"], "Mozilla/5.0 test")



if __name__ == "__main__":
    unittest.main()
