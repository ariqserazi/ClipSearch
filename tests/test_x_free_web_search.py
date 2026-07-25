import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import x_client
from app.database import Base
from app.models import Item, Ranking, Run, Source, utcnow
from app.routers.clips import query_ranked_items
from app.schemas import XArchiveSearchRequest, XCollectRequest
from app.x_client import (
    _discover_x_status_urls_from_free_web_search,
    _extract_search_hrefs,
    _free_web_search_queries,
    _free_web_search_result_urls,
    _status_urls_from_decoded_text,
    _tweet_from_public_status_html,
    _upsert_search_video_lead,
    collect_x_from_archive_search,
)


class XFreeWebSearchDiscoveryTests(unittest.TestCase):
    def test_duckduckgo_redirect_hrefs_are_decoded(self):
        html = """
        <a href="/l/?uddg=https%3A%2F%2Fx.com%2Fhasanthehun%2Fstatus%2F12345">x</a>
        <a href="https://twitter.com/Other/status/67890">twitter</a>
        """

        hrefs = _extract_search_hrefs(html)

        self.assertIn("https://x.com/hasanthehun/status/12345", hrefs)
        self.assertIn("https://twitter.com/Other/status/67890", hrefs)

    def test_status_urls_can_be_extracted_without_account_filter(self):
        text = """
        https://twitter.com/hasanthehun/status/12345
        https://mobile.twitter.com/Other/statuses/67890
        """

        urls = _status_urls_from_decoded_text(text, None)

        self.assertEqual(
            urls,
            [
                "https://x.com/hasanthehun/status/12345",
                "https://x.com/Other/status/67890",
            ],
        )

    def test_status_urls_still_filter_when_account_is_provided(self):
        text = """
        https://x.com/hasanthehun/status/12345
        https://x.com/Other/status/67890
        """

        urls = _status_urls_from_decoded_text(text, "@hasanthehun")

        self.assertEqual(urls, ["https://x.com/hasanthehun/status/12345"])

    def test_free_web_search_queries_target_x_statuses(self):
        queries = _free_web_search_queries(None, "Hasan debate")
        joined = "\n".join(queries)

        self.assertIn("site:x.com", joined)
        self.assertIn("inurl:/status", joined)
        self.assertIn("Hasan debate", joined)

        account_queries = "\n".join(_free_web_search_queries("@hasanthehun", "Hasan"))
        self.assertIn("site:x.com/hasanthehun/status", account_queries)

    def test_free_web_search_result_urls_include_duckduckgo_first(self):
        urls = _free_web_search_result_urls("site:x.com Hasan")

        self.assertIn("duckduckgo.com/html", urls[0])
        self.assertTrue(any("bing.com/search" in url for url in urls))

    def test_free_web_discovery_decodes_status_urls(self):
        class FakeResponse:
            text = '<a href="/l/?uddg=https%3A%2F%2Fx.com%2Fhasanthehun%2Fstatus%2F12345">x</a>'

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self):
                self.requested_urls = []

            async def get(self, url):
                self.requested_urls.append(url)
                return FakeResponse()

        client = FakeClient()
        urls, pages_seen = asyncio.run(
            _discover_x_status_urls_from_free_web_search(client, None, "Hasan", 1, [], set())
        )

        self.assertEqual(urls, ["https://x.com/hasanthehun/status/12345"])
        self.assertEqual(pages_seen, 1)
        self.assertIn("duckduckgo.com/html", client.requested_urls[0])

    def test_public_status_html_extracts_tweet_metadata(self):
        html = """
        <title>hasanabi on X: "TRUMP IS THE PRESIDENT" / X</title>
        <meta property="og:title" content="hasanabi (@hasanthehun) on X">
        <meta property="og:description" content="TRUMP IS THE PRESIDENT">
        <meta property="article:published_time" content="2026-07-07T00:36:12.000Z">
        <link rel="preload" as="image" href="https://pbs.twimg.com/amplify_video_thumb/1/img/thumb.jpg">
        <meta property="og:image" content="https://pbs.twimg.com/profile_images/profile.jpg">
        """

        parsed = _tweet_from_public_status_html(html, "https://x.com/hasanthehun/status/12345")

        self.assertIsNotNone(parsed)
        tweet, users, media = parsed
        self.assertEqual(tweet["id"], "12345")
        self.assertEqual(tweet["text"], "TRUMP IS THE PRESIDENT")
        self.assertEqual(tweet["created_at"], "2026-07-07T00:36:12.000Z")
        self.assertEqual(users["hasanthehun"]["name"], "hasanabi")
        self.assertEqual(media["12345-public-preview"]["preview_image_url"], "https://pbs.twimg.com/amplify_video_thumb/1/img/thumb.jpg")
        self.assertEqual(media["12345-public-preview"]["type"], "video")

    def test_public_status_html_does_not_classify_a_photo_as_video(self):
        html = """
        <title>hasanabi on X: "A photo post" / X</title>
        <meta property="og:title" content="hasanabi (@hasanthehun) on X">
        <meta property="og:description" content="A photo post">
        <meta property="og:image" content="https://pbs.twimg.com/media/example.jpg">
        """

        parsed = _tweet_from_public_status_html(html, "https://x.com/hasanthehun/status/12345")

        self.assertIsNotNone(parsed)
        _tweet, _users, media = parsed
        self.assertEqual(media["12345-public-preview"]["type"], "photo")

    def test_archive_search_request_defaults_to_web_provider(self):
        request = XArchiveSearchRequest(topic="Hasan debate")

        self.assertEqual(request.search_provider, "web")
        self.assertIsNone(request.account)

    def test_search_status_lead_is_not_stored_as_video_when_metadata_is_unavailable(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()

        item, created = _upsert_search_video_lead(
            db,
            source,
            "https://x.com/hasanthehun/status/12345",
            "Hasan debate",
        )
        db.commit()

        stored = db.query(Item).filter(Item.id == item.id).one()
        self.assertTrue(created)
        self.assertFalse(stored.is_video)
        self.assertEqual(stored.url, "https://x.com/hasanthehun/status/12345")
        self.assertEqual(stored.author_name, "hasanthehun")
        self.assertIn("Hasan debate", stored.title_or_text)

        _item, created_again = _upsert_search_video_lead(
            db,
            source,
            "https://x.com/hasanthehun/status/12345",
            "Hasan debate",
        )
        self.assertFalse(created_again)

    def test_search_discovery_topic_does_not_pollute_existing_tweet_search_text(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()
        existing = Item(
            source_id=source.id,
            external_id="12345",
            title_or_text="TRUMP IS THE PRESIDENT",
            author_name="hasanthehun",
            url="https://x.com/hasanthehun/status/12345",
            permalink="https://x.com/hasanthehun/status/12345",
            is_video=True,
        )
        db.add(existing)
        db.flush()

        item, created = _upsert_search_video_lead(
            db,
            source,
            "https://x.com/hasanthehun/status/12345",
            "Hasan",
        )

        self.assertFalse(created)
        self.assertEqual(item.title_or_text, "TRUMP IS THE PRESIDENT")
        self.assertNotIn("Search topic: Hasan", item.self_text or "")
        self.assertIn("Hasan", item.raw_json)

    def test_web_collection_keeps_unverified_status_links_out_of_video_only_results(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        original_collect_x = x_client.collect_x

        async def fake_collect_x(db, request: XCollectRequest):
            run = Run(
                source="x",
                mode="web",
                status="success",
                items_collected=0,
                finished_at=utcnow(),
            )
            db.add(run)
            db.commit()
            return {"run_id": run.id, "status": "success", "source_mode": "web", "items_collected": 0}

        try:
            x_client.collect_x = fake_collect_x
            result = asyncio.run(
                collect_x_from_archive_search(
                    db,
                    XArchiveSearchRequest(
                        topic="Hasan debate",
                        archive_html="https://x.com/hasanthehun/status/12345",
                        limit=1,
                    ),
                )
            )
        finally:
            x_client.collect_x = original_collect_x

        stored = db.query(Item).one()
        self.assertEqual(result["source_mode"], "web_search_discovery")
        self.assertEqual(result["items_collected"], 1)
        self.assertEqual(result["web_search_status_leads_created"], 1)
        self.assertFalse(stored.is_video)
        rows = query_ranked_items(
            db,
            source="x",
            min_drama_score=0,
            time_window="all",
            has_video=True,
            keyword="Hasan",
            limit=10,
        )
        self.assertEqual(rows, [])
        self.assertEqual(stored.url, "https://x.com/hasanthehun/status/12345")

    def test_web_collection_skips_a_dead_status_url_and_keeps_processing(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        db = sessionmaker(bind=engine)()

        with patch(
            "app.x_client._collect_x_web_page",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("dead status URL"),
                    (1, [], []),
                ]
            ),
        ):
            result = asyncio.run(
                x_client.collect_x(
                    db,
                    XCollectRequest(
                        urls=[
                            "https://x.com/example/status/111",
                            "https://x.com/example/status/222",
                        ],
                        source_mode="web",
                        limit=10,
                    ),
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["items_collected"], 1)
        self.assertEqual(result["pages_seen"], 2)
        self.assertEqual(result["pages_failed"], 1)
        self.assertIn("dead status URL", result["note"])

    def test_ranked_query_can_filter_x_results_by_account(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        source = Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()
        _upsert_search_video_lead(db, source, "https://x.com/Awk20000/status/111", "Hasan Piker")
        _upsert_search_video_lead(db, source, "https://x.com/Other/status/222", "Hasan Piker")
        for item in db.query(Item).all():
            item.is_video = True
            item.raw_json = "{}"
        db.commit()

        rows = query_ranked_items(
            db,
            source="x",
            min_drama_score=0,
            time_window="year",
            has_video=True,
            keyword="Hasan",
            account="Awk20000",
            limit=10,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].author_name, "Awk20000")

    def test_ranked_query_does_not_match_x_discovery_notes_as_tweet_content(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        db = sessionmaker(bind=engine)()
        source = Source(name="x", type="x", base_url="https://x.com")
        db.add(source)
        db.flush()
        item = Item(
            source_id=source.id,
            external_id="unrelated",
            title_or_text="An unrelated sports post",
            url="https://x.com/example/status/111",
            self_text="Search topic: Streamer University. Search-discovered X/Twitter video lead.",
            is_video=True,
            raw_json="{}",
        )
        db.add(item)
        db.flush()
        db.add(Ranking(item_id=item.id, drama_score=10, potential_label="low potential", reasoning="Test."))
        db.commit()

        rows = query_ranked_items(
            db,
            source="x",
            min_drama_score=0,
            time_window="all",
            has_video=True,
            keyword="university",
            limit=10,
        )

        self.assertEqual(rows, [])

    def test_ranked_query_keeps_reddit_when_all_sources_have_x_account_filter(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        x_source = Source(name="x", type="x", base_url="https://x.com")
        reddit_source = Source(name="reddit", type="reddit", base_url="https://www.reddit.com")
        db.add_all([x_source, reddit_source])
        db.flush()
        _upsert_search_video_lead(db, x_source, "https://x.com/Awk20000/status/111", "Hasan Piker")
        _upsert_search_video_lead(db, x_source, "https://x.com/Other/status/222", "Hasan Piker")
        for item in db.query(Item).filter(Item.source_id == x_source.id).all():
            item.is_video = True
            item.raw_json = "{}"
        reddit_item = Item(
            source_id=reddit_source.id,
            external_id="reddit-1",
            title_or_text="Hasan Piker reddit clip",
            author_name="reddit_user",
            created_time=utcnow(),
            url="https://www.reddit.com/r/LivestreamFail/comments/reddit-1/hasan_piker/",
            permalink="https://www.reddit.com/r/LivestreamFail/comments/reddit-1/hasan_piker/",
            domain="v.redd.it",
            subreddit="LivestreamFail",
            self_text="Hasan Piker video discussion",
            is_video=True,
            metrics_json="{}",
            raw_json="{}",
        )
        db.add(reddit_item)
        db.flush()
        db.add(
            Ranking(
                item_id=reddit_item.id,
                drama_score=30,
                potential_label="low potential",
                reasoning="has video or media metadata.",
            )
        )
        db.commit()

        rows = query_ranked_items(
            db,
            source="all",
            min_drama_score=0,
            time_window="year",
            has_video=True,
            keyword="Hasan",
            account="Awk20000",
            limit=10,
        )

        urls = [item.url for item, _ranking in rows]
        self.assertIn("https://x.com/Awk20000/status/111", urls)
        self.assertIn("https://www.reddit.com/r/LivestreamFail/comments/reddit-1/hasan_piker/", urls)
        self.assertNotIn("https://x.com/Other/status/222", urls)


if __name__ == "__main__":
    unittest.main()
