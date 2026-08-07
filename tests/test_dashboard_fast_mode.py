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
        self.assertIn('id="research-limit" type="number" min="25" max="5000" value="25"', html)
        self.assertIn("return Math.max(25, Number", html)
        self.assertIn("return Math.min(25, limit);", html)
        self.assertIn('<option value="reddit_x">Reddit + X</option>', html)
        self.assertIn('source === "reddit_x"', html)
        self.assertIn('id="deep-search" type="checkbox"', html)
        self.assertIn("Deep search / more results", html)
        self.assertIn("const redditComments = deepSearchEnabled() ? 5 : 0;", html)
        self.assertIn("top_comments_limit: redditComments", html)
        self.assertIn("function activeXPageLimit()", html)
        self.assertIn('document.getElementById("deep-search").addEventListener("change"', html)
        self.assertIn('timeWindow.value === "day"', html)
        self.assertIn('timeWindow.value = "month"', html)
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
        self.assertIn('<option value="kiwifarms">Kiwi Farms</option>', html)
        self.assertIn('<option value="all">All Sources</option>', html)
        self.assertIn('id="person-topic"', html)
        self.assertIn('["/collect/kiwifarms"', html)
        self.assertIn("Kiwi Farms searched:", html)
        self.assertIn("function renderCollectionStatus(items)", html)
        self.assertIn("Pinterest Image Research", html)
        self.assertIn('id="pinterest-query"', html)
        self.assertIn('id="pinterest-limit" type="number" min="1" max="50" value="8"', html)
        self.assertIn('id="search-pinterest"', html)
        self.assertIn('id="download-pinterest"', html)
        self.assertIn('postJson("/research/pinterest-search", { query, limit })', html)
        self.assertIn('postJson("/research/pinterest-download"', html)
        self.assertIn("function renderPinterestSearchResult(data)", html)
        self.assertIn("Multi-link Downloader", html)
        self.assertIn("X posts &amp; photos • YouTube • Reddit • Instagram • Twitch • Kick • Rumble", html)
        self.assertIn("Twitch and Kick clips, VODs, and live channels are supported", html)
        self.assertIn("Rumble video and livestream links work too", html)
        self.assertIn("text-only X posts save as PNG screenshots", html)
        self.assertIn('id="video-download-urls"', html)
        self.assertIn('id="download-video-links"', html)
        self.assertIn('postJson("/downloads/links", { urls })', html)
        self.assertIn("function renderLinkDownloadResult(data)", html)
        self.assertIn("data/downloads/link-downloader", html)
        self.assertIn('item.is_video || item.source === "x"', html)
        self.assertIn("Use <strong>Save file</strong>", html)
        self.assertIn('href="${esc(file.download_url)}" download', html)

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
