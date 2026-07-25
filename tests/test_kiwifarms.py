import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.kiwifarms_client import (
    PublicSearchUnavailable,
    _RequestState,
    _bridge_post_media,
    _detect_access_issue,
    _is_no_results,
    _normalized_media_url,
    _search_base,
    _search_bridge,
    _source,
    _upsert_result,
    parse_search_results,
    redact_personal_information,
)
from app.main import app
from app.models import Item, Ranking, Source, utcnow
from app.routers.ui import clips_page
from app.schemas import KiwiFarmsCollectRequest

FIXTURE_DIR = Path(__file__).with_name("fixtures")


def fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class KiwiFarmsParserTests(unittest.TestCase):
    def setUp(self):
        self.results = parse_search_results(
            fixture("kiwifarms_search_results.html"),
            "https://kiwifarms.example",
        )

    def test_search_result_parsing_and_duplicate_handling(self):
        self.assertEqual(len(self.results), 2)
        self.assertEqual(self.results[0]["title"], "Hasan clip discussion")
        self.assertEqual(self.results[1]["title"], "Text-only lead")

    def test_direct_post_permalink_and_ids_are_extracted(self):
        result = self.results[0]
        self.assertEqual(result["thread_id"], "123")
        self.assertEqual(result["post_id"], "456")
        self.assertEqual(result["external_id"], "thread:123:post:456")
        self.assertEqual(result["permalink"], "https://kiwifarms.example/threads/sample-thread.123/post-456")
        self.assertEqual(result["thread_url"], "https://kiwifarms.example/threads/sample-thread.123/")

    def test_date_and_author_are_parsed(self):
        result = self.results[0]
        self.assertEqual(result["created_time"].isoformat(), "2026-07-20T12:30:00+00:00")
        self.assertEqual(result["author"], "forum_user")

    def test_forum_reply_view_and_relevance_metadata_are_parsed(self):
        result = self.results[0]
        self.assertEqual(result["forum"], "Culture")
        self.assertEqual(result["replies"], 1200)
        self.assertEqual(result["views"], 45678)
        self.assertEqual(result["relevance"], "0.91")

    def test_embedded_x_youtube_and_direct_video_links_are_extracted(self):
        media = {entry["type"]: entry["url"] for entry in self.results[0]["media"]}
        self.assertEqual(media["x_status"], "https://x.com/example/status/1001")
        self.assertEqual(media["youtube"], "https://www.youtube.com/shorts/abc123")
        self.assertEqual(media["direct_video"], "https://cdn.example.test/video.mp4")

    def test_archive_wrapped_media_urls_are_unwrapped_for_downloads(self):
        youtube = _normalized_media_url(
            "https://archive.ph/o/example/https://www.youtube.com/watch?v=video123&t=10s"
        )
        x_status = _normalized_media_url(
            "https://archive.ph/o/example/https%3A%2F%2Fx.com%2Fexample%2Fstatus%2F12345"
        )

        self.assertEqual(youtube, ("https://www.youtube.com/watch?v=video123&t=10s", "youtube"))
        self.assertEqual(x_status, ("https://x.com/example/status/12345", "x_status"))

    def test_bridge_post_media_keeps_its_forum_source(self):
        media = _bridge_post_media(
            {
                "post_id": "456",
                "permalink": "https://kiwifarms.st/threads/example.123/post-456",
                "outbound_links": ["https://youtube.com/watch?v=test"],
            }
        )

        self.assertEqual(media[0]["url"], "https://youtube.com/watch?v=test")
        self.assertEqual(media[0]["source_post_id"], "456")
        self.assertTrue(media[0]["source_post_permalink"].endswith("/post-456"))

    def test_personal_information_is_redacted_from_excerpt(self):
        excerpt = self.results[0]["excerpt"]
        self.assertNotIn("bad@example.com", excerpt)
        self.assertNotIn("212-555-0199", excerpt)
        self.assertNotIn("123 Main Street", excerpt)
        self.assertIn("[REDACTED EMAIL]", excerpt)
        self.assertIn("[REDACTED PHONE]", excerpt)
        self.assertIn("[REDACTED ADDRESS]", excerpt)

    def test_family_information_and_ip_redaction(self):
        value = redact_personal_information("His mother is Jane Doe and the server was 192.168.1.20.")
        self.assertNotIn("Jane Doe", value)
        self.assertNotIn("192.168.1.20", value)

    def test_empty_results_fixture_is_detected(self):
        html = fixture("kiwifarms_no_results.html")
        self.assertEqual(parse_search_results(html, "https://kiwifarms.example"), [])
        self.assertTrue(_is_no_results(html))

    def test_security_challenge_is_detected(self):
        request = httpx.Request("GET", "https://kiwifarms.example/search/")
        response = httpx.Response(203, request=request, text=fixture("kiwifarms_challenge.html"))
        self.assertIn("security challenge", _detect_access_issue(response))


class KiwiFarmsHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_search_maps_threads_and_enriches_media(self):
        seen_requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append((request.url.path, str(request.url.query)))
            if request.url.path == "/search":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "query": "Hasan",
                        "page": 1,
                        "limit": 2,
                        "results": [
                            {
                                "thread_id": "123",
                                "title": "Hasan discussion",
                                "url": "https://kiwifarms.st/threads/hasan.123/",
                                "forum_name": "Culture",
                                "snippet": "Search result excerpt",
                                "author_display_name": "public_user",
                                "created_at": "2026-07-20T12:30:00Z",
                            },
                            {
                                "thread_id": "789",
                                "title": "Second result",
                                "url": "https://kiwifarms.st/threads/second.789/",
                            },
                        ],
                    },
                )
            if request.url.path == "/threads/123":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "thread_id": "123",
                        "title": "Hasan discussion",
                        "forum_name": "Culture",
                        "canonical_url": "https://kiwifarms.net/threads/hasan.123/",
                        "requested_page": 1,
                        "current_page": 1,
                        "total_pages": 4,
                        "posts": [
                            {
                                "post_id": "456",
                                "position": 1,
                                "author_display_name": "clip_poster",
                                "created_at": "2026-07-21T01:02:03Z",
                                "permalink": "https://kiwifarms.st/threads/hasan.123/post-456",
                                "text": "Hasan clip with context",
                                "outbound_links": [
                                    "https://www.youtube.com/watch?v=test",
                                    "https://example.test/not-media",
                                ],
                            }
                        ],
                    },
                )
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results, requests_used, note = await _search_bridge(
                client,
                _RequestState(0),
                "https://bridge.example",
                KiwiFarmsCollectRequest(query="Hasan", limit=2),
                2,
            )

        self.assertEqual([result["external_id"] for result in results], ["thread:123", "thread:789"])
        self.assertEqual(results[0]["post_id"], "456")
        self.assertEqual(results[0]["author"], "clip_poster")
        self.assertEqual(results[0]["media"][0]["url"], "https://www.youtube.com/watch?v=test")
        self.assertEqual(results[0]["media"][0]["type"], "youtube")
        self.assertEqual(results[0]["media"][0]["source_post_id"], "456")
        self.assertEqual(results[0]["permalink"], "https://kiwifarms.st/threads/hasan.123/post-456")
        self.assertEqual(results[0]["canonical_url"], "https://kiwifarms.net/threads/hasan.123/")
        self.assertEqual(requests_used, 2)
        self.assertIn("Media scan enriched 1 of 2", note)
        self.assertEqual([path for path, _query in seen_requests], ["/search", "/threads/123"])

    async def test_bridge_media_scan_enriches_all_threads_before_recent_pages(self):
        seen_requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append((request.url.path, str(request.url.query)))
            if request.url.path == "/search":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "results": [
                            {"thread_id": "123", "title": "First", "url": "https://kiwifarms.st/threads/first.123/"},
                            {"thread_id": "789", "title": "Second", "url": "https://kiwifarms.st/threads/second.789/"},
                        ]
                    },
                )
            if request.url.path == "/threads/123":
                page = int(request.url.params.get("page", "1"))
                links = [] if page == 1 else ["https://youtube.com/watch?v=recent"]
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "thread_id": "123",
                        "title": "First",
                        "canonical_url": "https://kiwifarms.st/threads/first.123/",
                        "total_pages": 2,
                        "posts": [
                            {
                                "post_id": str(100 + page),
                                "position": page,
                                "permalink": f"https://kiwifarms.st/threads/first.123/post-{100 + page}",
                                "text": "First page",
                                "outbound_links": links,
                            }
                        ],
                    },
                )
            if request.url.path == "/threads/789":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "thread_id": "789",
                        "title": "Second",
                        "canonical_url": "https://kiwifarms.st/threads/second.789/",
                        "total_pages": 1,
                        "posts": [
                            {
                                "post_id": "790",
                                "position": 1,
                                "permalink": "https://kiwifarms.st/threads/second.789/post-790",
                                "text": "Second page",
                                "outbound_links": ["https://x.com/example/status/790"],
                            }
                        ],
                    },
                )
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results, requests_used, note = await _search_bridge(
                client,
                _RequestState(0),
                "https://bridge.example",
                KiwiFarmsCollectRequest(query="Hasan", limit=2),
                4,
            )

        self.assertEqual(requests_used, 4)
        self.assertIsNone(note)
        self.assertEqual(results[0]["media"][0]["url"], "https://youtube.com/watch?v=recent")
        self.assertEqual(results[1]["media"][0]["url"], "https://x.com/example/status/790")
        self.assertEqual(
            [path for path, _query in seen_requests],
            ["/search", "/threads/123", "/threads/789", "/threads/123"],
        )

    async def test_bridge_render_502_is_reported_without_storing_html(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, request=request, text="<html>Render Bad Gateway</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicSearchUnavailable) as caught:
                await _search_bridge(
                    client,
                    _RequestState(0),
                    "https://bridge.example",
                    KiwiFarmsCollectRequest(query="Hasan", limit=2),
                    2,
                )

        self.assertIn("HTTP 502", str(caught.exception))
        self.assertNotIn("Render Bad Gateway", str(caught.exception))

    async def test_public_search_form_is_submitted_with_exact_query(self):
        seen_body = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_body
            if request.method == "GET" and request.url.path == "/search/":
                return httpx.Response(200, request=request, text=fixture("kiwifarms_search_form.html"))
            if request.method == "POST" and request.url.path == "/search/search":
                seen_body = request.content.decode()
                return httpx.Response(200, request=request, text=fixture("kiwifarms_search_results.html"))
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            results, pages_seen, note = await _search_base(
                client,
                _RequestState(0),
                "https://kiwifarms.example",
                KiwiFarmsCollectRequest(query="Hasan Piker", limit=25),
                1,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(pages_seen, 1)
        self.assertIsNone(note)
        self.assertIn("keywords=Hasan+Piker", seen_body)
        self.assertIn("_xfToken=public-csrf-token", seen_body)

    async def test_http_403_is_a_non_bypassable_access_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, request=request, text="Forbidden")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(PublicSearchUnavailable) as caught:
                await _search_base(
                    client,
                    _RequestState(0),
                    "https://kiwifarms.example",
                    KiwiFarmsCollectRequest(query="Hasan", limit=5),
                    1,
                )

        self.assertTrue(caught.exception.stop_fallbacks)
        self.assertIn("403", str(caught.exception))


class KiwiFarmsStorageTests(unittest.TestCase):
    def test_results_use_generic_items_media_deduplication_and_ranking(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        db = sessionmaker(bind=engine)()
        source = _source(db, "https://kiwifarms.example")
        parsed = parse_search_results(fixture("kiwifarms_search_results.html"), "https://kiwifarms.example")[0]

        item = _upsert_result(db, source, parsed, "Hasan")
        _upsert_result(db, source, parsed, "Hasan")
        db.commit()

        self.assertEqual(db.query(Item).count(), 1)
        self.assertEqual(item.source.name, "kiwifarms")
        self.assertTrue(item.is_video)
        self.assertEqual(item.url, "https://cdn.example.test/video.mp4")
        self.assertEqual(item.permalink, "https://kiwifarms.example/threads/sample-thread.123/post-456")
        self.assertEqual(len(item.media), 3)
        self.assertGreaterEqual(len(item.rankings), 1)
        self.assertNotIn("bad@example.com", item.self_text)
        self.assertNotIn("<!doctype", item.raw_json.lower())
        self.assertNotIn("bad@example.com", item.raw_json)

    def test_nonvideo_forum_result_has_post_link_without_download_control(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        db = sessionmaker(bind=engine)()
        source = _source(db, "https://kiwifarms.example")
        item = Item(
            source_id=source.id,
            external_id="thread:789",
            title_or_text="Text-only lead",
            url="https://kiwifarms.example/threads/text-only.789/",
            permalink="https://kiwifarms.example/threads/text-only.789/",
            is_video=False,
        )
        db.add(item)
        db.flush()
        db.add(Ranking(item_id=item.id, drama_score=10, potential_label="low potential", reasoning="Text lead."))
        db.commit()

        page = clips_page(
            source="kiwifarms",
            min_drama_score=0,
            time_window="all",
            has_video=None,
            keyword=None,
            account=None,
            limit=20,
            db=db,
        ).body.decode()

        self.assertIn("Open forum post", page)
        self.assertNotIn(f'data-download-id="{item.id}"', page)

class KiwiFarmsAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_collect_kiwifarms_route(self):
        expected = {
            "run_id": 123,
            "status": "success",
            "source_mode": "bridge_api",
            "items_collected": 2,
            "pages_seen": 1,
            "note": None,
        }
        with patch("app.routers.collect.collect_kiwifarms", new=AsyncMock(return_value=expected)) as mocked:
            response = self.client.post("/collect/kiwifarms", json={"query": "Hasan Piker", "limit": 25})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        request = mocked.await_args.args[1]
        self.assertEqual(request.query, "Hasan Piker")

    def test_collect_all_keeps_reddit_and_x_when_kiwifarms_is_unavailable(self):
        reddit_result = {"status": "success", "items_collected": 3}
        x_result = {"status": "success", "items_collected": 2}
        kiwi_result = {
            "status": "failed",
            "source_mode": "bridge_api",
            "items_collected": 0,
            "pages_seen": 0,
            "note": "Kiwi Farms returned a security challenge.",
        }
        with (
            patch("app.routers.collect.collect_reddit", new=AsyncMock(return_value=reddit_result)),
            patch("app.routers.collect.collect_x", new=AsyncMock(return_value=x_result)),
            patch("app.routers.collect.collect_kiwifarms", new=AsyncMock(return_value=kiwi_result)),
        ):
            response = self.client.post(
                "/collect/all",
                json={"kiwifarms": {"query": "Hasan Piker", "limit": 25}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reddit"], reddit_result)
        self.assertEqual(response.json()["x"], x_result)
        self.assertEqual(response.json()["kiwifarms"], kiwi_result)

    def _seed_ranked_items(self):
        db = self.Session()
        kiwi = Source(name="kiwifarms", type="forum", base_url="https://kiwifarms.example")
        reddit = Source(name="reddit", type="reddit", base_url="https://www.reddit.com")
        x_source = Source(name="x", type="x", base_url="https://x.com")
        db.add_all([kiwi, reddit, x_source])
        db.flush()
        items = [
            Item(
                source_id=kiwi.id,
                external_id="thread:1:post:2",
                title_or_text="Hasan forum clip",
                created_time=utcnow(),
                url="https://youtube.com/watch?v=test",
                permalink="https://kiwifarms.example/threads/test.1/post-2",
                is_video=True,
                metrics_json=json.dumps({"forum": "Culture"}),
            ),
            Item(
                source_id=reddit.id,
                external_id="reddit-1",
                title_or_text="Hasan reddit clip",
                created_time=utcnow(),
                url="https://v.redd.it/test",
                permalink="https://reddit.com/r/test/comments/reddit-1",
                is_video=True,
            ),
            Item(
                source_id=x_source.id,
                external_id="x-1",
                title_or_text="Unrelated X post",
                created_time=utcnow(),
                url="https://x.com/test/status/1",
                permalink="https://x.com/test/status/1",
                is_video=True,
            ),
        ]
        db.add_all(items)
        db.flush()
        for item in items:
            db.add(Ranking(item_id=item.id, drama_score=25, potential_label="low potential", reasoning="Test lead."))
        db.commit()
        db.close()

    def test_agent_search_clips_filters_kiwifarms_only(self):
        self._seed_ranked_items()
        response = self.client.post(
            "/agent/search-clips",
            json={"source": "kiwifarms", "keywords": ["Hasan"], "time_window": "month", "min_drama_score": 0, "limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["source"] for item in response.json()["results"]], ["kiwifarms"])
        self.assertTrue(response.json()["results"][0]["is_video"])
        self.assertIn("/threads/test.1/post-2", response.json()["results"][0]["permalink"])

    def test_agent_search_clips_all_includes_all_three_sources(self):
        self._seed_ranked_items()
        response = self.client.post(
            "/agent/search-clips",
            json={"source": "all", "keywords": [], "time_window": "month", "min_drama_score": 0, "limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["source"] for item in response.json()["results"]}, {"reddit", "x", "kiwifarms"})

    def test_agent_search_clips_reddit_x_excludes_kiwifarms(self):
        self._seed_ranked_items()
        response = self.client.post(
            "/agent/search-clips",
            json={"source": "reddit_x", "keywords": [], "time_window": "month", "min_drama_score": 0, "limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["source"] for item in response.json()["results"]}, {"reddit", "x"})

    def test_markdown_report_can_filter_kiwifarms(self):
        self._seed_ranked_items()
        response = self.client.get("/reports/latest.md?source=kiwifarms")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Source filter: kiwifarms", response.text)
        self.assertIn("Hasan forum clip", response.text)
        self.assertNotIn("Hasan reddit clip", response.text)
        self.assertIn("Forum post:", response.text)


if __name__ == "__main__":
    unittest.main()
