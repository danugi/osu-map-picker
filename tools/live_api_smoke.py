"""Manually runnable live osu! search-contract smoke test.

This is intentionally outside the pytest suite: it performs real API requests.
It prints only request parameters, aggregate counts and public beatmapset IDs;
credentials, tokens and cursor payloads are never printed.

Usage:
    python tools/live_api_smoke.py
    python tools/live_api_smoke.py --pages 3 --query "Cut Ver" --query "Sped Up"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from models import SearchQuery  # noqa: E402
from osu_api import OsuApiClient  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sanitized live osu! /beatmapsets/search smoke test"
    )
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="Title/search query; may be repeated",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="Maximum cursor pages per query (default: 2)",
    )
    parser.add_argument(
        "--status",
        default="ranked",
        help="osu! search status (default: ranked)",
    )
    parser.add_argument(
        "--sort",
        default="ranked_desc",
        help="osu! search sort (default: ranked_desc)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    queries = args.queries or [
        "Cut Ver",
        "Nightcore & Cut Ver",
        "Sped Up",
    ]
    pages_limit = max(1, min(10, int(args.pages)))

    try:
        api_config = config.load_api_config()
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    if not api_config.configured:
        print(
            "NOT CONFIGURED: set OSU_CLIENT_ID and OSU_CLIENT_SECRET in the "
            "environment or .env. No request was made."
        )
        return 2

    client = OsuApiClient(api_config)
    print("LIVE API SMOKE (sanitized; credentials/tokens are not printed)")
    print(f"pages per query: {pages_limit}")
    try:
        for query_text in queries:
            print(f"\nQUERY: {query_text!r}")
            query = SearchQuery(
                mode="osu",
                status=args.status,
                sort=args.sort,
                query=query_text,
                nsfw=False,
            )
            all_ids: set[int] = set()
            seen_ids: set[int] = set()
            for page_number in range(1, pages_limit + 1):
                params = query.to_params()
                safe_params = {
                    key: (
                        f"<cursor len={len(str(value))}>"
                        if key.startswith("cursor")
                        else value
                    )
                    for key, value in params.items()
                }
                print(f"  page {page_number} params: {safe_params}")
                page = client.search_beatmapsets(query)
                ids = [item.id for item in page.beatmapsets]
                all_ids.update(ids)
                overlap = len(seen_ids & set(ids))
                seen_ids.update(ids)
                print(
                    "    raw="
                    f"{page.raw_count} parsed={len(ids)} total={page.total} "
                    f"cursor_string={bool(page.cursor_string)} "
                    f"cursor_object={bool(page.cursor)} "
                    f"overlap_previous={overlap} ids={ids[:5]}"
                )
                if page.warnings:
                    print(f"    warnings={len(page.warnings)}")
                if page.error:
                    print(f"    api_error={page.error!r}")
                if not page.has_more:
                    print("    exhausted: no next cursor")
                    break
                next_query = (
                    SearchQuery(
                        mode=query.mode,
                        status=query.status,
                        sort=query.sort,
                        query=query.query,
                        nsfw=query.nsfw,
                        cursor=None,
                        cursor_string=page.cursor_string,
                    )
                    if page.cursor_string
                    else SearchQuery(
                        mode=query.mode,
                        status=query.status,
                        sort=query.sort,
                        query=query.query,
                        nsfw=query.nsfw,
                        cursor=page.cursor,
                        cursor_string=None,
                    )
                )
                query = next_query
            print(f"  unique public IDs across pages: {len(all_ids)}")
        print(
            "\nTOTALS: "
            f"requests={client.requests_made} "
            f"http_attempts={client.http_attempts} "
            f"token_requests={client.token_requests}"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
