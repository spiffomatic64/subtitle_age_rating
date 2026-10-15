#!/usr/bin/env python3
"""Rate-limited episode-source and artwork caching for the subtitle application."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
from PIL import Image


WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKISIMPSONS_API = "https://simpsonswiki.com/w/api.php"
SOURCE_CONFIGS = (
    ("wikipedia", WIKIPEDIA_API),
    ("wikisimpsons", WIKISIMPSONS_API),
)
DEFAULT_ARTWORK_DIR = Path("static/episode_artwork")
USER_AGENT = "SubtitleMediaRatings/3.0 (local single-user research tool)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_curl_cffi() -> Any:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError(
            "curl_cffi is required for external-source and artwork caching; "
            "install dependencies with: pip install -r requirements.txt"
        ) from exc
    return requests


@dataclass
class PoliteClient:
    min_interval: float = 0.4
    retries: int = 4
    timeout: float = 30.0

    def __post_init__(self) -> None:
        requests = require_curl_cffi()
        self.session = requests.Session()
        self._last_request = 0.0

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    timeout=self.timeout,
                    impersonate="chrome",
                )
                self._last_request = time.monotonic()
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 30)
                    if attempt < self.retries:
                        time.sleep(delay)
                        continue
                response.raise_for_status()
                return response
            except Exception as exc:  # curl_cffi uses its own exception hierarchy
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(2**attempt, 30))
        assert last_error is not None
        raise last_error


def _clean_text(value: str) -> str:
    value = re.sub(r"\[[0-9]+]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _title_score(expected: str, candidate: str, snippet: str, context: str = "") -> float:
    expected_lower = expected.casefold()
    candidate_clean = re.sub(r"\s*\([^)]*\)\s*$", "", candidate).casefold()
    score = SequenceMatcher(None, expected_lower, candidate_clean).ratio()
    if expected_lower == candidate_clean:
        score += 1.0
    combined = f"{candidate} {snippet}".casefold()
    if context and context.casefold() in combined:
        score += 0.35
    if "episode" in combined:
        score += 0.15
    return score


def _extract_relevant_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style, table, .navbox, .metadata, .mw-editsection, sup.reference"):
        unwanted.decompose()

    headings = soup.find_all(["h2", "h3"])
    preferred = ("plot", "synopsis", "story", "premise", "summary")
    chunks: list[str] = []
    for heading in headings:
        heading_text = _clean_text(heading.get_text(" ", strip=True)).casefold()
        if not any(word in heading_text for word in preferred):
            continue
        level = int(heading.name[1])
        for sibling in heading.find_next_siblings():
            if sibling.name in {"h2", "h3"} and int(sibling.name[1]) <= level:
                break
            if sibling.name in {"p", "ul", "ol"}:
                text = _clean_text(sibling.get_text(" ", strip=True))
                if text:
                    chunks.append(text)
        if chunks:
            break

    if not chunks:
        chunks = [
            _clean_text(paragraph.get_text(" ", strip=True))
            for paragraph in soup.find_all("p", limit=10)
            if _clean_text(paragraph.get_text(" ", strip=True))
        ]
    return "\n".join(chunks)[:7000]


def fetch_mediawiki_source(client: PoliteClient, api_url: str, title: str,
                          context: str = "", allow_search: bool = True) -> dict[str, Any] | None:
    def parse_page(page_title: str) -> dict[str, Any]:
        return client.get(
            api_url,
            params={
                "action": "parse",
                "format": "json",
                "formatversion": 2,
                "page": page_title,
                "prop": "text|displaytitle",
                "redirects": 1,
            },
        ).json()

    # Episode titles are normally the canonical page names on both sources.
    # Trying the exact page first also avoids Wikisimpsons' stricter search syntax.
    direct_response = parse_page(title)
    direct_parsed = direct_response.get("parse", {})
    direct_html = direct_parsed.get("text") or ""
    direct_summary = _extract_relevant_page_text(direct_html) if direct_html else ""
    if direct_summary:
        page_title = str(direct_parsed.get("title") or title)
        base = f"{urlparse(api_url).scheme}://{urlparse(api_url).netloc}"
        return {
            "page_title": page_title,
            "source_url": f"{base}/wiki/{quote(page_title.replace(' ', '_'))}",
            "summary": direct_summary,
            "raw_json": json.dumps(
                {"match": "exact-title", "displaytitle": direct_parsed.get("displaytitle")},
                ensure_ascii=False,
                sort_keys=True,
            ),
        }

    if not allow_search:
        return None
    search_response = client.get(
        api_url,
        params={
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "list": "search",
            "srsearch": f'"{title}" {context}'.strip(),
            "srlimit": 8,
            "utf8": 1,
        },
    ).json()
    results = search_response.get("query", {}).get("search", [])
    if not results:
        return None
    best = max(
        results,
        key=lambda item: _title_score(title, str(item.get("title", "")), str(item.get("snippet", "")), context),
    )
    if _title_score(title, str(best.get("title", "")), str(best.get("snippet", "")), context) < 0.65:
        return None

    page_title = str(best["title"])
    parse_response = parse_page(page_title)
    parsed = parse_response.get("parse", {})
    html = parsed.get("text") or ""
    summary = _extract_relevant_page_text(html)
    if not summary:
        return None
    base = f"{urlparse(api_url).scheme}://{urlparse(api_url).netloc}"
    return {
        "page_title": page_title,
        "source_url": f"{base}/wiki/{quote(page_title.replace(' ', '_'))}",
        "summary": summary,
        "raw_json": json.dumps(
            {"search_result": best, "displaytitle": parsed.get("displaytitle")},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def cache_episode_sources(
    connection: sqlite3.Connection,
    *,
    force: bool = False,
    limit: int = 0,
    episode_codes: set[str] | None = None,
    client: PoliteClient | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    episodes = connection.execute(
        "SELECT * FROM episodes WHERE title IS NOT NULL ORDER BY series_title, season, episode, title"
    ).fetchall()
    if episode_codes is not None:
        episodes = [row for row in episodes if row["episode_code"] in episode_codes]
    if limit:
        episodes = episodes[:limit]
    counts = {"success": 0, "cached": 0, "missing": 0, "error": 0}
    def sources_for(episode):
        if episode["series_key"] == "tvmaze:83":
            return [(name, url, episode["title"], "Simpsons episode", True) for name, url in SOURCE_CONFIGS]
        page = json.loads(episode["provider_ids_json"] or "{}").get("wikipedia_title")
        # General media uses an explicitly selected page. Ambiguous title-only
        # searches must never silently attach an unrelated plot to a transcript.
        return [("wikipedia", WIKIPEDIA_API, page, "", False)] if page else []

    total = sum(len(sources_for(episode)) for episode in episodes)
    index = 0
    for episode in episodes:
        code = episode["episode_code"]
        for source_name, api_url, page_title, context, allow_search in sources_for(episode):
            index += 1
            existing = connection.execute(
                "SELECT status FROM episode_sources WHERE episode_code = ? AND source_name = ?",
                (code, source_name),
            ).fetchone()
            # Preserve successful and confidently missing lookups, but retry
            # transient errors automatically on the next cache run.
            if existing and existing["status"] in {"success", "missing"} and not force:
                counts["cached"] += 1
                continue
            try:
                client = client or PoliteClient()
                source = fetch_mediawiki_source(client, api_url, page_title, context, allow_search)
                if source is None:
                    values = (None, None, None, None, "missing", "No matching episode page")
                    counts["missing"] += 1
                else:
                    values = (
                        source["source_url"],
                        source["page_title"],
                        source["summary"],
                        source["raw_json"],
                        "success",
                        None,
                    )
                    counts["success"] += 1
            except Exception as exc:
                values = (None, None, None, None, "error", str(exc)[:2000])
                counts["error"] += 1
            connection.execute(
                """
                INSERT INTO episode_sources(
                    episode_code, source_name, source_url, page_title, summary,
                    raw_json, status, error, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_code, source_name) DO UPDATE SET
                    source_url = excluded.source_url,
                    page_title = excluded.page_title,
                    summary = excluded.summary,
                    raw_json = excluded.raw_json,
                    status = excluded.status,
                    error = excluded.error,
                    fetched_at = excluded.fetched_at
                """,
                (code, source_name, *values, utc_now()),
            )
            connection.commit()
            if index % 25 == 0 or index == total:
                progress(f"Sources {index}/{total}: {counts}")
    return counts


def _extension_for(content_type: str, source_url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, Path(urlparse(source_url).path).suffix.lower() or ".jpg")


def cache_one_artwork(
    connection: sqlite3.Connection,
    episode_code: str,
    *,
    artwork_dir: Path = DEFAULT_ARTWORK_DIR,
    force: bool = False,
    client: PoliteClient | None = None,
) -> Path | None:
    row = connection.execute(
        """
        SELECT ep.image_original, ep.image_medium, ac.local_path, ac.status
        FROM episodes AS ep
        LEFT JOIN artwork_cache AS ac ON ac.episode_code = ep.episode_code
        WHERE ep.episode_code = ?
        """,
        (episode_code,),
    ).fetchone()
    if row is None:
        return None
    if not force and row["status"] == "success" and row["local_path"]:
        existing = Path(row["local_path"])
        if existing.is_file():
            return existing
    source_url = row["image_original"] or row["image_medium"]
    if not source_url:
        connection.execute(
            """
            INSERT INTO artwork_cache(episode_code, status, error, updated_at)
            VALUES (?, 'missing', 'No artwork URL in episode metadata', ?)
            ON CONFLICT(episode_code) DO UPDATE SET
                status = excluded.status, error = excluded.error, updated_at = excluded.updated_at
            """,
            (episode_code, utc_now()),
        )
        connection.commit()
        return None

    client = client or PoliteClient(min_interval=0.25)
    previous = connection.execute(
        "SELECT attempts FROM artwork_cache WHERE episode_code = ?", (episode_code,)
    ).fetchone()
    attempts = int(previous["attempts"]) + 1 if previous else 1
    try:
        response = client.get(source_url)
        content = bytes(response.content)
        if len(content) > 15 * 1024 * 1024:
            raise RuntimeError("Artwork exceeds 15 MiB safety limit")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        extension = _extension_for(content_type, source_url)
        artwork_dir.mkdir(parents=True, exist_ok=True)
        target = artwork_dir / f"{episode_code}{extension}"
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(content)
        with Image.open(temporary) as image:
            image.verify()
        temporary.replace(target)
        values = (source_url, str(target), content_type, "success", attempts, None, utc_now())
    except Exception as exc:
        values = (source_url, None, None, "error", attempts, str(exc)[:2000], utc_now())
        target = None
    connection.execute(
        """
        INSERT INTO artwork_cache(
            episode_code, source_url, local_path, content_type, status, attempts, error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(episode_code) DO UPDATE SET
            source_url = excluded.source_url,
            local_path = excluded.local_path,
            content_type = excluded.content_type,
            status = excluded.status,
            attempts = excluded.attempts,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (episode_code, *values),
    )
    connection.commit()
    return target


def cache_all_artwork(
    connection: sqlite3.Connection,
    *,
    artwork_dir: Path = DEFAULT_ARTWORK_DIR,
    force: bool = False,
    limit: int = 0,
    progress: Callable[[str], None] = print,
) -> dict[str, int]:
    codes = [
        row["episode_code"]
        for row in connection.execute(
            "SELECT episode_code FROM episodes ORDER BY season, episode"
        )
    ]
    if limit:
        codes = codes[:limit]
    client = PoliteClient(min_interval=0.25)
    counts = {"success": 0, "missing_or_error": 0}
    for index, code in enumerate(codes, 1):
        path = cache_one_artwork(
            connection, code, artwork_dir=artwork_dir, force=force, client=client
        )
        counts["success" if path else "missing_or_error"] += 1
        if index % 25 == 0 or index == len(codes):
            progress(f"Artwork {index}/{len(codes)}: {counts}")
    return counts
