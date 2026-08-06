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

    def _db_with_reddit_text_item(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = models.Source(name="reddit", type="reddit", base_url="https://www.reddit.com")
        db.add(source)
        db.flush()
        item = models.Item(
            source_id=source.id,
            external_id="abc123",
            title_or_text="A Reddit text post",
            self_text="This is the body of the post and it should be visible.",
            author_name="example_user",
            subreddit="examples",
            url="https://www.reddit.com/r/examples/comments/abc123/a_reddit_text_post/",
            permalink="https://www.reddit.com/r/examples/comments/abc123/a_reddit_text_post/",
            is_video=False,
            metrics_json='{"score": 42, "num_comments": 7}',
            raw_json="{}",
        )
        db.add(item)
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
        video_file = next(entry for entry in result["files"] if entry["name"] == "tiny.mp4")
        self.assertEqual(video_file["host_path"], f"data/downloads/reddit/{item_id}-tiny-test-clip/tiny.mp4")
        self.assertEqual(video_file["size_bytes"], 5)

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

    def test_text_reddit_item_download_creates_post_screenshot_without_ytdlp(self):
        db, item_id = self._db_with_reddit_text_item()

        def fake_render(snapshot, destination):
            self.assertEqual(snapshot.subreddit, "examples")
            self.assertIn("body of the post", snapshot.body)
            destination.write_bytes(b"png")
            return destination

        async def fail_if_ytdlp_runs(*_args):
            raise AssertionError("yt-dlp should not run for a stored text-only Reddit post")

        original_render = downloads._render_reddit_screenshot
        original_run_ytdlp = downloads._run_ytdlp
        downloads._render_reddit_screenshot = fake_render
        downloads._run_ytdlp = fail_if_ytdlp_runs
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(downloads.download_item_media(db, item_id, Path(temp_dir)))
        finally:
            downloads._render_reddit_screenshot = original_render
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["downloads"][0]["kind"], "screenshot")
        self.assertTrue(result["files"][0]["host_path"].endswith("/reddit-post.png"))

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

    def test_instagram_url_normalization_accepts_reels_posts_and_igtv(self):
        expected_reel = (
            "https://www.instagram.com/reel/DbRJmS-pUBT/",
            "DbRJmS-pUBT",
        )
        self.assertEqual(
            downloads._normalize_instagram_url(
                "https://www.instagram.com/reels/DbRJmS-pUBT/?igsh=tracking"
            ),
            expected_reel,
        )
        self.assertEqual(
            downloads._normalize_instagram_url("instagram.com/reel/DbRJmS-pUBT/"),
            expected_reel,
        )
        self.assertEqual(
            downloads._normalize_instagram_url("https://m.instagram.com/p/AbCdEf12345/"),
            ("https://www.instagram.com/p/AbCdEf12345/", "AbCdEf12345"),
        )
        self.assertEqual(
            downloads._normalize_instagram_url("https://www.instagram.com/tv/AbCdEf12345/"),
            ("https://www.instagram.com/tv/AbCdEf12345/", "AbCdEf12345"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_instagram_url("https://www.instagram.com/example/")
        with self.assertRaises(ValueError):
            downloads._normalize_instagram_url("https://notinstagram.com/reel/DbRJmS-pUBT/")

    def test_instagram_link_download_uses_canonical_url_and_reports_source(self):
        attempted = []

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            attempted.append(url)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "Video by creator [Instagram-DbRJmS-pUBT].mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        ["https://www.instagram.com/reels/DbRJmS-pUBT/?igsh=tracking"],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(attempted, ["https://www.instagram.com/reel/DbRJmS-pUBT/"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source_counts"], {"instagram": 1})
        self.assertEqual(result["downloads"][0]["source"], "instagram")
        self.assertTrue(result["files"][0]["name"].endswith(".mp4"))

    def test_instagram_photo_post_download_writes_image_when_there_is_no_video(self):
        command = downloads._ytdlp_download_command(
            "https://www.instagram.com/p/DbdsCbOhEw4/",
            Path("/tmp/downloads"),
        )

        self.assertIn("--ignore-no-formats-error", command)
        self.assertIn("--write-thumbnail", command)

        reel_command = downloads._ytdlp_download_command(
            "https://www.instagram.com/reel/DbRJmS-pUBT/",
            Path("/tmp/downloads"),
        )
        self.assertNotIn("--ignore-no-formats-error", reel_command)
        self.assertNotIn("--write-thumbnail", reel_command)

        async def fake_run_ytdlp(_url, target_dir, _timeout_seconds):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "Photo by creator [Instagram-DbdsCbOhEw4].jpg").write_bytes(b"image")
            return downloads.DownloadProcessResult(
                returncode=1,
                stdout="wrote thumbnail",
                stderr="There is no video in this post",
            )

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        ["https://www.instagram.com/p/DbdsCbOhEw4/"],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["downloads"][0]["fallback"], "instagram_image")
        self.assertTrue(result["downloads"][0]["files"][0]["name"].endswith(".jpg"))

    def test_twitch_url_normalization_accepts_clips_vods_and_live_channels(self):
        expected_clip = (
            "https://clips.twitch.tv/AwkwardHelplessSalamanderSwiftRage",
            "clip-awkwardhelplesssalamanderswiftrage",
        )
        self.assertEqual(
            downloads._normalize_twitch_url(
                "https://clips.twitch.tv/AwkwardHelplessSalamanderSwiftRage?tt_content=url"
            ),
            expected_clip,
        )
        self.assertEqual(
            downloads._normalize_twitch_url(
                "twitch.tv/ExampleStreamer/clip/AwkwardHelplessSalamanderSwiftRage"
            ),
            expected_clip,
        )
        self.assertEqual(
            downloads._normalize_twitch_url("https://m.twitch.tv/videos/1234567890?t=1h2m"),
            ("https://www.twitch.tv/videos/1234567890", "video-1234567890"),
        )
        self.assertEqual(
            downloads._normalize_twitch_url("https://www.twitch.tv/ExampleStreamer"),
            ("https://www.twitch.tv/examplestreamer", "channel-examplestreamer"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_twitch_url("https://www.twitch.tv/directory/category/games")
        with self.assertRaises(ValueError):
            downloads._normalize_twitch_url("https://www.twitch.tv/videos")
        with self.assertRaises(ValueError):
            downloads._normalize_twitch_url("https://not-twitch.tv/example")

    def test_twitch_clip_download_canonicalizes_deduplicates_and_reports_source(self):
        attempted = []

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            attempted.append(url)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "A Twitch clip [TwitchClips-ExampleClipSlug].mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        [
                            "https://clips.twitch.tv/ExampleClipSlug?tt_medium=clips_api",
                            "https://www.twitch.tv/example/clip/ExampleClipSlug",
                        ],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(attempted, ["https://clips.twitch.tv/ExampleClipSlug"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(result["source_counts"], {"twitch": 1})
        self.assertEqual(result["downloads"][0]["source"], "twitch")
        self.assertTrue(result["files"][0]["name"].endswith(".mp4"))

    def test_kick_url_normalization_accepts_clips_vods_and_live_channels(self):
        clip_id = "clip_01J8RGZRKHXHXXKJEHGRM932A5"
        expected_clip = (
            f"https://kick.com/example/clips/{clip_id}",
            f"clip-{clip_id.lower()}",
        )
        self.assertEqual(
            downloads._normalize_kick_url(
                f"https://kick.com/Example?clip={clip_id}&ref=share"
            ),
            expected_clip,
        )
        self.assertEqual(
            downloads._normalize_kick_url(
                f"kick.com/example/clips/{clip_id}?source=share"
            ),
            expected_clip,
        )

        vod_id = "5C697A87-AFCE-4256-B01F-3C8FE71EF5CB"
        self.assertEqual(
            downloads._normalize_kick_url(
                f"https://www.kick.com/Example/videos/{vod_id}?ref=share"
            ),
            (
                f"https://kick.com/example/videos/{vod_id.lower()}",
                f"video-{vod_id.lower()}",
            ),
        )
        self.assertEqual(
            downloads._normalize_kick_url("https://kick.com/Example?ref=share"),
            ("https://kick.com/example", "channel-example"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_kick_url("https://kick.com/categories/just-chatting")
        with self.assertRaises(ValueError):
            downloads._normalize_kick_url("https://kick.com/example/videos/not-a-uuid")
        with self.assertRaises(ValueError):
            downloads._normalize_kick_url("https://not-kick.com/example")

    def test_rumble_url_normalization_accepts_video_embed_and_livestream_pages(self):
        self.assertEqual(
            downloads._normalize_rumble_url(
                "https://www.rumble.com/v6abcde-example-video.html?e9s=src_v1_ucp"
            ),
            (
                "https://rumble.com/v6abcde-example-video.html",
                "video-v6abcde",
            ),
        )
        self.assertEqual(
            downloads._normalize_rumble_url("rumble.com/embed/ufe9n.v5pv5f/?pub=test"),
            ("https://rumble.com/embed/v5pv5f", "embed-v5pv5f"),
        )
        self.assertEqual(
            downloads._normalize_rumble_url("https://rumble.com/v2e7fju-live-event.html"),
            ("https://rumble.com/v2e7fju-live-event.html", "video-v2e7fju"),
        )

        with self.assertRaises(ValueError):
            downloads._normalize_rumble_url("https://rumble.com/c/ExampleChannel")
        with self.assertRaises(ValueError):
            downloads._normalize_rumble_url("https://rumble.com/videos")
        with self.assertRaises(ValueError):
            downloads._normalize_rumble_url("https://not-rumble.com/v6abcde-video.html")

    def test_kick_and_rumble_links_download_deduplicate_and_report_sources(self):
        attempted = []

        async def fake_run_ytdlp(url, target_dir, timeout_seconds):
            attempted.append(url)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"Downloaded media [{len(attempted)}].mp4").write_bytes(b"video")
            return downloads.DownloadProcessResult(returncode=0, stdout="downloaded", stderr="")

        clip_id = "clip_01J8RGZRKHXHXXKJEHGRM932A5"
        original_run_ytdlp = downloads._run_ytdlp
        downloads._run_ytdlp = fake_run_ytdlp
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        [
                            f"https://kick.com/example?clip={clip_id}",
                            f"https://kick.com/example/clips/{clip_id}",
                            "https://rumble.com/v6abcde-first-title.html?ref=share",
                            "https://www.rumble.com/v6abcde-another-title.html",
                        ],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp

        self.assertEqual(
            attempted,
            [
                f"https://kick.com/example/clips/{clip_id}",
                "https://rumble.com/v6abcde-first-title.html",
            ],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["requested_count"], 4)
        self.assertEqual(result["unique_count"], 2)
        self.assertEqual(result["duplicates_skipped"], 2)
        self.assertEqual(result["source_counts"], {"kick": 1, "rumble": 1})
        self.assertEqual(
            [entry["source"] for entry in result["downloads"]],
            ["kick", "rumble"],
        )

    def test_reddit_archived_post_data_preserves_selftext_and_metrics(self):
        snapshot = downloads._reddit_snapshot_from_post_data(
            {
                "id": "1c2b1wl",
                "title": "YouTube Thumbnail Playbook",
                "selftext": "YouTubers need thumbnails.\n\nThis body must appear.",
                "author": "deadcoder0904",
                "subreddit": "sidehustle",
                "permalink": "/r/sidehustle/comments/1c2b1wl/example/",
                "created_utc": 1712933335,
                "score": 74,
                "num_comments": 50,
            },
            "https://www.reddit.com/r/sidehustle/comments/1c2b1wl/example",
            "1c2b1wl",
        )

        self.assertIn("This body must appear", snapshot.body)
        self.assertEqual(snapshot.score, 74)
        self.assertEqual(snapshot.comment_count, 50)
        self.assertEqual(snapshot.author, "deadcoder0904")

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
        original_fetch_reddit_snapshot = downloads._fetch_reddit_snapshot
        downloads._run_ytdlp = fake_run_ytdlp

        async def fake_fetch_reddit_snapshot(_url):
            raise ValueError("no public post data")

        downloads._fetch_reddit_snapshot = fake_fetch_reddit_snapshot
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
            downloads._fetch_reddit_snapshot = original_fetch_reddit_snapshot

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

    def test_multi_link_downloader_falls_back_to_reddit_screenshot(self):
        async def fake_run_ytdlp(_url, _target_dir, _timeout_seconds):
            return downloads.DownloadProcessResult(returncode=1, stdout="", stderr="no media")

        async def fake_fetch_snapshot(url):
            return downloads.RedditSnapshot(
                title="An ordinary Reddit text post",
                body="The post has a text body.",
                author="example_user",
                subreddit="examples",
                url=url,
                post_id="abc123",
                score=20,
                comment_count=3,
            )

        def fake_render(_snapshot, destination):
            destination.write_bytes(b"png")
            return destination

        original_run_ytdlp = downloads._run_ytdlp
        original_fetch_snapshot = downloads._fetch_reddit_snapshot
        original_render = downloads._render_reddit_screenshot
        downloads._run_ytdlp = fake_run_ytdlp
        downloads._fetch_reddit_snapshot = fake_fetch_snapshot
        downloads._render_reddit_screenshot = fake_render
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.download_links(
                        ["https://www.reddit.com/r/examples/comments/abc123/an_ordinary_post/"],
                        Path(temp_dir),
                    )
                )
        finally:
            downloads._run_ytdlp = original_run_ytdlp
            downloads._fetch_reddit_snapshot = original_fetch_snapshot
            downloads._render_reddit_screenshot = original_render

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["downloads"][0]["fallback"], "reddit_screenshot")
        self.assertTrue(result["files"][0]["host_path"].endswith("-reddit-post.png"))

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

    def test_reddit_screenshot_renderer_outputs_png(self):
        from PIL import Image

        snapshot = downloads.RedditSnapshot(
            title="A Reddit screenshot title that wraps cleanly",
            body="A text body with enough detail to verify the Reddit post card renderer.",
            author="example_user",
            subreddit="examples",
            url="https://www.reddit.com/r/examples/comments/abc123/example/",
            post_id="abc123",
            created_at="2026-07-24T12:00:00Z",
            score=12,
            comment_count=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "reddit-post.png"
            downloads._render_reddit_screenshot(snapshot, destination)
            with Image.open(destination) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 1200)
                self.assertGreater(image.height, 400)


if __name__ == "__main__":
    unittest.main()
