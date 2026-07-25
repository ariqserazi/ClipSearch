import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base
from app.routers import downloads


class DownloadEndpointTests(unittest.TestCase):
    def _db_with_item(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = models.Source(name="reddit", type="reddit", base_url="https://www.reddit.com")
        db.add(source)
        db.flush()
        item = models.Item(
            source_id=source.id,
            external_id="clip-1",
            title_or_text="Tiny test clip",
            url="https://example.com/tiny.mp4",
            permalink="https://example.com/tiny.mp4",
            domain="example.com",
            is_video=True,
            metrics_json="{}",
            raw_json="{}",
        )
        db.add(item)
        db.commit()
        return db, item.id

    def _db_with_x_item(self, *, image_url=None):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = models.Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()
        item = models.Item(
            source_id=source.id,
            external_id="123456",
            title_or_text="A normal post that should become a screenshot.",
            author_name="example",
            url="https://x.com/example/status/123456",
            permalink="https://x.com/example/status/123456",
            is_video=False,
            metrics_json='{"like_count": 25, "retweet_count": 4}',
            raw_json='{"author": {"name": "Example Person", "username": "example"}}',
        )
        db.add(item)
        db.flush()
        if image_url:
            db.add(
                models.Media(
                    item_id=item.id,
                    media_key="photo-1",
                    media_type="photo",
                    url=image_url,
                    preview_image_url=image_url,
                )
            )
        db.commit()
        return db, item.id

    def test_download_item_media_writes_under_source_and_item_directory(self):
        db, item_id = self._db_with_item()

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            self.assertEqual(url, "https://example.com/tiny.mp4")
            self.assertTrue(str(target_dir).endswith(f"reddit/{item_id}-tiny-test-clip"))
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "tiny.mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(downloads.download_item_media(db, item_id, Path(temp_dir)))
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["item_id"], item_id)
        self.assertEqual(result["source"], "reddit")
        self.assertEqual(result["host_dir"], f"data/downloads/reddit/{item_id}-tiny-test-clip")
        self.assertEqual(result["files"][0]["host_path"], f"data/downloads/reddit/{item_id}-tiny-test-clip/tiny.mp4")
        self.assertEqual(result["files"][0]["size_bytes"], 5)

    def test_download_directory_uses_readable_slug_after_item_id(self):
        db, item_id = self._db_with_item()
        item = db.get(models.Item, item_id)
        item.title_or_text = 'Hasan says "zero chance" & more!!!'
        db.commit()

        path = downloads._download_dir_for_item(item, Path("/tmp/downloads"))

        self.assertEqual(path, Path(f"/tmp/downloads/reddit/{item_id}-hasan-says-zero-chance-more"))

    def test_downloaded_file_info_has_safe_browser_download_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "link-downloader" / "Tweet image [123].png"
            path.parent.mkdir()
            path.write_bytes(b"png")

            info = downloads._file_info(path, root)

            self.assertEqual(info["name"], "Tweet image [123].png")
            self.assertEqual(
                info["download_url"],
                "/downloads/files/link-downloader/Tweet%20image%20%5B123%5D.png",
            )
            self.assertEqual(
                downloads._resolve_download_file("link-downloader/Tweet image [123].png", root),
                path.resolve(),
            )
            with self.assertRaises(HTTPException) as caught:
                downloads._resolve_download_file("../outside.png", root)
            self.assertEqual(caught.exception.status_code, 404)

    def test_nested_html_entities_are_removed_from_tweet_text(self):
        self.assertEqual(
            downloads._decode_html_entities("fleet of 500k &amp;amp; make more"),
            "fleet of 500k & make more",
        )

    def test_retry_replaces_existing_download_instead_of_reporting_stale_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_dir = root / ".link-downloader-work" / "x-example"
            target_dir = root / "link-downloader"
            work_dir.mkdir(parents=True)
            target_dir.mkdir()
            (work_dir / "x-example-tweet.png").write_bytes(b"new screenshot")
            destination = target_dir / "x-example-tweet.png"
            destination.write_bytes(b"old screenshot")

            files = downloads._move_completed_files(work_dir, target_dir, root)

            self.assertEqual(destination.read_bytes(), b"new screenshot")
            self.assertFalse(work_dir.exists())
            self.assertEqual(files[0]["name"], "x-example-tweet.png")

    def test_download_item_attempts_every_attached_media_url(self):
        db, item_id = self._db_with_item()
        item = db.get(models.Item, item_id)
        item.source.name = "kiwifarms"
        db.add_all(
            [
                models.Media(item_id=item.id, media_key="primary", media_type="direct_video", url=item.url),
                models.Media(
                    item_id=item.id,
                    media_key="youtube",
                    media_type="youtube",
                    url="https://youtube.com/watch?v=second",
                ),
            ]
        )
        db.commit()
        attempted = []

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            attempted.append(url)
            if "youtube.com" in url:
                return downloads.DownloadProcessResult(returncode=1, stdout="", stderr="blocked")
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "tiny.mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(downloads.download_item_media(db, item_id, Path(temp_dir)))
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(
            attempted,
            ["https://example.com/tiny.mp4", "https://youtube.com/watch?v=second"],
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["media_count"], 2)
        self.assertEqual(result["media_succeeded"], 1)
        self.assertEqual(result["media_failed"], 1)
        self.assertEqual(len(result["downloads"]), 2)

    def test_download_item_media_raises_for_missing_item(self):
        db, _item_id = self._db_with_item()

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(downloads.download_item_media(db, 999))

        self.assertEqual(caught.exception.status_code, 404)

    def test_text_only_x_item_download_creates_tweet_screenshot(self):
        db, item_id = self._db_with_x_item()

        def fake_render(snapshot, destination):
            self.assertEqual(snapshot.handle, "example")
            self.assertIn("normal post", snapshot.text)
            destination.write_bytes(b"png")
            return destination

        original_render = downloads._render_tweet_screenshot
        downloads._render_tweet_screenshot = fake_render
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(downloads.download_item_media(db, item_id, Path(temp_dir)))
        finally:
            downloads._render_tweet_screenshot = original_render

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["media_succeeded"], 1)
        self.assertEqual(result["downloads"][0]["kind"], "screenshot")
        self.assertTrue(result["files"][0]["host_path"].endswith("/tweet.png"))

    def test_x_photo_item_download_saves_image_and_tweet_screenshot(self):
        image_url = "https://pbs.twimg.com/media/example.jpg"
        db, item_id = self._db_with_x_item(image_url=image_url)

        async def fake_download_image(url, destination_stem):
            self.assertEqual(url, image_url)
            destination = destination_stem.with_suffix(".jpg")
            destination.write_bytes(b"image")
            return destination

        def fake_render(_snapshot, destination):
            destination.write_bytes(b"png")
            return destination

        original_download_image = downloads._download_image_url
        original_render = downloads._render_tweet_screenshot
        downloads._download_image_url = fake_download_image
        downloads._render_tweet_screenshot = fake_render
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(downloads.download_item_media(db, item_id, Path(temp_dir)))
        finally:
            downloads._download_image_url = original_download_image
            downloads._render_tweet_screenshot = original_render

        self.assertEqual(result["status"], "success")
        self.assertEqual([entry["kind"] for entry in result["downloads"]], ["image", "screenshot"])
        self.assertEqual(
            {Path(entry["host_path"]).suffix for entry in result["files"]},
            {".jpg", ".png"},
        )

    def test_x_status_url_normalization_accepts_x_and_twitter_links(self):
        self.assertEqual(
            downloads._normalize_x_status_url(
                "https://mobile.twitter.com/Awk20000/statuses/123456/video/1?ref=test"
            ),
            ("https://x.com/awk20000/status/123456", "awk20000", "123456"),
        )
        self.assertEqual(
            downloads._normalize_x_status_url("x.com/Example/status/987654"),
            ("https://x.com/example/status/987654", "example", "987654"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_x_status_url("https://x.com/example")
        with self.assertRaises(ValueError):
            downloads._normalize_x_status_url("https://youtube.com/watch?v=test")

    def test_youtube_url_normalization_accepts_videos_shorts_and_short_links(self):
        expected = ("https://www.youtube.com/watch?v=AbCdEf12345", "AbCdEf12345")
        self.assertEqual(
            downloads._normalize_youtube_url("https://youtu.be/AbCdEf12345?t=20"),
            expected,
        )
        self.assertEqual(
            downloads._normalize_youtube_url(
                "https://www.youtube.com/shorts/AbCdEf12345?feature=share"
            ),
            expected,
        )

        with self.assertRaises(ValueError):
            downloads._normalize_youtube_url("https://www.youtube.com/@example")
        with self.assertRaises(ValueError):
            downloads._normalize_youtube_url("https://www.youtube.com/watch?list=playlist")

    def test_reddit_url_normalization_accepts_posts_and_hosted_videos(self):
        self.assertEqual(
            downloads._normalize_reddit_url(
                "https://old.reddit.com/r/videos/comments/AbC123/example_title/?utm_source=test"
            ),
            ("https://www.reddit.com/r/videos/comments/AbC123/example_title", "abc123"),
        )
        self.assertEqual(
            downloads._normalize_reddit_url("https://v.redd.it/Video987/DASH_720.mp4"),
            ("https://v.redd.it/Video987", "video987"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_reddit_url("https://www.reddit.com/r/videos/")

    def test_mixed_link_download_deduplicates_and_uses_one_title_named_folder(self):
        attempted = []

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            attempted.append((url, target_dir))
            if "reddit.com" in url:
                return downloads.DownloadProcessResult(returncode=1, stdout="", stderr="blocked")
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"Video topic title [{len(attempted)}].mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        [
                            "https://x.com/Awk20000/status/111?ref=test",
                            "twitter.com/awk20000/statuses/111/video/1",
                            "https://youtu.be/AbCdEf12345?t=20",
                            "https://youtube.com/shorts/AbCdEf12345",
                            "https://www.reddit.com/r/videos/comments/AbC123/example_title/",
                            "https://redd.it/abc123",
                            "https://v.redd.it/Video987/DASH_720.mp4",
                            "https://example.com/not-supported",
                        ],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(
            [url for url, _target_dir in attempted],
            [
                "https://x.com/awk20000/status/111",
                "https://www.youtube.com/watch?v=AbCdEf12345",
                "https://www.reddit.com/r/videos/comments/AbC123/example_title",
                "https://v.redd.it/Video987",
            ],
        )
        self.assertTrue(
            all(target_dir.parent.name == ".link-downloader-work" for _url, target_dir in attempted)
        )
        self.assertEqual(len({target_dir for _url, target_dir in attempted}), 4)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["requested_count"], 8)
        self.assertEqual(result["unique_count"], 4)
        self.assertEqual(result["duplicates_skipped"], 3)
        self.assertEqual(result["invalid_count"], 1)
        self.assertEqual(result["succeeded"], 3)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(
            result["source_counts"],
            {"x": 1, "youtube": 1, "reddit": 2},
        )
        self.assertEqual(result["host_dir"], "data/downloads/link-downloader")
        self.assertTrue(
            all(
                entry["host_dir"] == "data/downloads/link-downloader"
                for entry in result["downloads"]
                if entry["status"] != "invalid"
            )
        )
        self.assertTrue(
            all(
                entry["host_path"].startswith("data/downloads/link-downloader/Video topic title")
                for entry in result["files"]
            )
        )

    def test_multi_link_downloader_falls_back_to_x_screenshot(self):
        async def fake_run_ytdlp(_url, _target_dir, _timeout_seconds):
            return downloads.DownloadProcessResult(returncode=1, stdout="", stderr="no video")

        async def fake_fetch_snapshot(url):
            return downloads.XSnapshot(
                text="This is an ordinary text post.",
                handle="example",
                display_name="Example Person",
                url=url,
                status_id="123456",
            )

        def fake_render(_snapshot, destination):
            destination.write_bytes(b"png")
            return destination

        original_run_ytdlp = downloads._run_ytdlp
        original_fetch_snapshot = downloads._fetch_x_snapshot
        original_render = downloads._render_tweet_screenshot
        downloads._run_ytdlp = fake_run_ytdlp
        downloads._fetch_x_snapshot = fake_fetch_snapshot
        downloads._render_tweet_screenshot = fake_render
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        ["https://x.com/example/status/123456"],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp
            downloads._fetch_x_snapshot = original_fetch_snapshot
            downloads._render_tweet_screenshot = original_render

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["downloads"][0]["fallback"], "x_screenshot")
        self.assertTrue(result["files"][0]["host_path"].endswith("-tweet.png"))

    def test_tweet_screenshot_renderer_outputs_png(self):
        from PIL import Image

        snapshot = downloads.XSnapshot(
            text="A screenshot test with enough text to verify wrapping across multiple lines.",
            handle="example",
            display_name="Example Person",
            url="https://x.com/example/status/123456",
            status_id="123456",
            created_at="2026-07-24T12:00:00Z",
            metrics={"like_count": 12, "retweet_count": 3},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "tweet.png"
            downloads._render_tweet_screenshot(snapshot, destination)
            with Image.open(destination) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 1200)
                self.assertGreater(image.height, 400)


if __name__ == "__main__":
    unittest.main()
