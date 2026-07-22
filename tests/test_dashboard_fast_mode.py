import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.routers.ui import dashboard
from app.schemas import RedditCollectRequest


class DashboardFastModeTests(unittest.TestCase):
    def test_dashboard_defaults_to_fast_collection_mode(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        html = dashboard(db).body.decode()

        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', html)
        self.assertIn('id="research-limit" type="number" min="1" max="5000" value="10"', html)
        self.assertIn('id="deep-search" type="checkbox"', html)
        self.assertIn("Deep search / more results", html)
        self.assertIn("const redditComments = deepSearchEnabled() ? 5 : 0;", html)
        self.assertIn("top_comments_limit: redditComments", html)
        self.assertIn("function activeXPageLimit()", html)
        self.assertIn('Search mode: ${deepSearchEnabled() ? "deep" : "fast"}', html)
        self.assertIn('id="copy-urls"', html)
        self.assertIn('id="copy-download-command"', html)
        self.assertIn('id="download-all"', html)
        self.assertIn("function dockerDownloadCommand(urls)", html)
        self.assertIn("yt-dlp -P /data/downloads -a -", html)
        self.assertIn('data-download-id="${esc(item.id)}"', html)
        self.assertIn("async function downloadItem(itemId, button)", html)
        self.assertIn("async function downloadAllResults(button)", html)
        self.assertIn("Math.min(2, itemIds.length)", html)

    def test_clips_page_has_download_all_for_filtered_results(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        from app.routers.ui import clips_page

        html = clips_page(
            source="all",
            min_drama_score=0,
            time_window="week",
            has_video=None,
            keyword=None,
            account=None,
            limit=50,
            db=db,
        ).body.decode()

        self.assertIn("data-download-all", html)
        self.assertIn("async function downloadAllStaticItems(button)", html)
        self.assertIn("Math.min(2, uniqueButtons.length)", html)

    def test_reddit_collect_request_allows_fast_comment_limits(self):
        self.assertEqual(RedditCollectRequest(top_comments_limit=0).top_comments_limit, 0)
        self.assertEqual(RedditCollectRequest(top_comments_limit=1).top_comments_limit, 1)


if __name__ == "__main__":
    unittest.main()
