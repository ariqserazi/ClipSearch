import asyncio
import tempfile
import unittest
from pathlib import Path

from app.pinterest_client import PinterestPin, download_pinterest_pin, parse_pinterest_search_response
from app.routers import downloads


class PinterestResearchTests(unittest.TestCase):
    def test_search_parser_keeps_public_pins_and_prefers_original_images(self):
        payload = {
            "resource_response": {
                "http_status": 200,
                "status": "success",
                "data": {
                    "results": [
                        {"type": "story", "id": "not-a-pin"},
                        {
                            "type": "pin",
                            "id": "12345",
                            "description": "A moody newsroom",
                            "images": {
                                "736x": {
                                    "url": "https://i.pinimg.com/736x/ab/cd/example.jpg",
                                    "width": 736,
                                    "height": 920,
                                },
                                "orig": {
                                    "url": "https://i.pinimg.com/originals/ab/cd/example.jpg",
                                    "width": 1200,
                                    "height": 1500,
                                },
                            },
                            "pinner": {"username": "example_creator"},
                        },
                        {
                            "type": "pin",
                            "id": "67890",
                            "description": "External image should be rejected",
                            "images": {"orig": {"url": "https://example.com/not-pinterest.jpg"}},
                        },
                    ]
                },
            }
        }

        pins = parse_pinterest_search_response(payload, 10)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].pin_id, "12345")
        self.assertEqual(pins[0].pin_url, "https://www.pinterest.com/pin/12345/")
        self.assertEqual(pins[0].image_url, "https://i.pinimg.com/originals/ab/cd/example.jpg")
        self.assertEqual((pins[0].width, pins[0].height), (1200, 1500))
        self.assertEqual(pins[0].pinner, "example_creator")

    def test_image_downloader_rejects_non_pinterest_hosts_before_requesting(self):
        pin = PinterestPin(
            pin_id="12345",
            title="Example",
            description="",
            pin_url="https://www.pinterest.com/pin/12345/",
            image_url="https://example.com/image.jpg",
            width=100,
            height=100,
            pinner=None,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "outside Pinterest"):
                asyncio.run(download_pinterest_pin(pin, Path(temp_dir) / "image"))

    def test_research_download_returns_provenance_and_per_image_failures(self):
        pins = [
            PinterestPin(
                pin_id="111",
                title="First image",
                description="",
                pin_url="https://www.pinterest.com/pin/111/",
                image_url="https://i.pinimg.com/originals/first.jpg",
                width=1200,
                height=800,
                pinner="creator_one",
            ),
            PinterestPin(
                pin_id="222",
                title="Second image",
                description="",
                pin_url="https://www.pinterest.com/pin/222/",
                image_url="https://i.pinimg.com/originals/second.jpg",
                width=900,
                height=1200,
                pinner="creator_two",
            ),
        ]

        async def fake_search(query, limit):
            self.assertEqual(query, "late night studio")
            self.assertEqual(limit, 2)
            return "https://www.pinterest.com/search/pins/?q=late%20night%20studio", pins

        async def fake_download(pin, destination_stem):
            if pin.pin_id == "222":
                raise ValueError("image unavailable")
            path = destination_stem.with_suffix(".jpg")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            return path

        original_search = downloads.search_public_pinterest
        original_download = downloads.download_pinterest_pin
        downloads.search_public_pinterest = fake_search
        downloads.download_pinterest_pin = fake_download
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = asyncio.run(
                    downloads.research_pinterest_images(
                        "late night studio",
                        2,
                        Path(temp_dir),
                    )
                )
        finally:
            downloads.search_public_pinterest = original_search
            downloads.download_pinterest_pin = original_download

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pins_found"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["host_dir"], "data/downloads/pinterest/late-night-studio")
        self.assertEqual(result["downloads"][0]["pin_url"], "https://www.pinterest.com/pin/111/")
        self.assertEqual(result["downloads"][1]["error"], "image unavailable")
        self.assertIn("Pinterest-111", result["files"][0]["name"])
        self.assertIn("copyrighted", result["rights_note"])


if __name__ == "__main__":
    unittest.main()
