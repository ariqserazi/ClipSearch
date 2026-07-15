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

    def test_download_item_media_raises_for_missing_item(self):
        db, _item_id = self._db_with_item()

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(downloads.download_item_media(db, 999))

        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
