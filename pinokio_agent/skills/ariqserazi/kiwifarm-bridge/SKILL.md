---
name: ariqserazi-kiwifarm-bridge
description: Search and retrieve verified public Kiwi Farms threads through the Kiwifarms Bridge REST API.
---

# Kiwifarms Bridge

Use the caller-reachable deployment from `KIWIFARMS_BRIDGE_URL`. Keep its value in the repository's ignored `.env`; no deployment URL is committed. No authentication or remote file upload is required.

- Check `GET /health` for process health and `GET /site` for verified guest-search capability.
- Search with `GET /search?q=<query>&page=<1-based>&limit=<1-20>`.
- Use only the returned `url` or `thread_id`; never infer a thread URL or ID from a name.
- Retrieve one page with `GET /thread?url=<verified-url>&page=<1-based>&post_limit=<1-50>`.
- Search responses contain `thread_id`, `title`, `url`, optional forum/snippet/author/date fields, and no guessed results.
- Thread responses contain verified canonical metadata and structured posts with redacted text, permalinks, and `outbound_links`.
- Expected failures use `detail.error`, `detail.message`, `detail.upstream_status`, and `detail.retryable`. Hosting-level HTML errors are outages, not API data.

Drama Clip Scout implements this contract in `app/kiwifarms_client.py`, batches search at 20 results, and uses remaining request budget for thread/media enrichment. Regenerate a client only if `/search` or `/thread` returns a 404/405, a 400/422 schema mismatch, or authentication requirements change.
