"""Media identification and additive, backwards-compatible library migration.

The historical ``episodes.episode_code`` column remains the public media ID.
Keeping that key preserves all existing foreign keys and bookmarked URLs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

EPISODE_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:s(?P<s>\d{1,3})e(?P<e>\d{1,3})|(?P<xs>\d{1,3})x(?P<xe>\d{1,3}))(?:[^a-z0-9]|$)"
)


def clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[._]+", " ", value)).strip(" -_()[]")


def stable_id(prefix: str, value: str, *, case_sensitive: bool = False) -> str:
    normalized = value if case_sensitive else value.casefold()
    return f"{prefix}-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]}"


@dataclass
class ImportOptions:
    media_type: str = "auto"
    show_title: str | None = None
    title: str | None = None
    year: int | None = None
    tvmaze_show_id: int | None = None
    language: str | None = None
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    existing: dict[str, dict[str, Any]] = field(default_factory=dict)
    legacy_root: Path | None = None


def load_manifest(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValueError('Media manifest must contain a "files" object keyed by relative subtitle path')
    result = {}
    allowed = {"type", "title", "year", "show", "season", "episode", "tvmaze_show_id",
               "language", "summary", "artwork_url", "source_url", "wikipedia_title", "edition"}
    for name, metadata in value["files"].items():
        if not isinstance(metadata, dict) or set(metadata) - allowed:
            raise ValueError(f"Invalid metadata fields for {name!r}")
        if metadata.get("type", "transcript") not in {"episode", "movie", "transcript"}:
            raise ValueError(f"Invalid media type for {name!r}")
        for key in ("year", "season", "episode", "tvmaze_show_id"):
            if key in metadata and (type(metadata[key]) is not int or metadata[key] < 0):
                raise ValueError(f"{key} must be a nonnegative integer for {name!r}")
        for key, item in metadata.items():
            if key not in {"year", "season", "episode", "tvmaze_show_id"} and not isinstance(item, str):
                raise ValueError(f"{key} must be text for {name!r}")
        if "language" in metadata and not re.fullmatch(r"[a-z]{2,3}|auto", metadata["language"]):
            raise ValueError(f"Invalid language code for {name!r}")
        normalized = Path(name).as_posix()
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Manifest paths must be relative to the subtitle directory")
        result[normalized] = metadata
    return result


def existing_bindings(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(subtitle_files)")}
    language = "sf.language" if "language" in columns else "'en'"
    return {
        row["path"]: dict(row)
        for row in connection.execute(
            f"SELECT sf.path, {language} AS subtitle_language, ep.* FROM subtitle_files sf JOIN episodes ep USING(episode_code)"
        )
    }


def identify(path: Path, root: Path, options: ImportOptions | None = None) -> dict[str, Any]:
    options = options or ImportOptions()
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    override = options.manifest.get(relative, {})
    identity_fields = {"type", "show", "season", "episode", "tvmaze_show_id", "year", "edition"}
    explicit = bool(identity_fields.intersection(override) or options.show_title or options.tvmaze_show_id
                    or options.media_type != "auto" or options.year)
    previous = options.existing.get(path.resolve().as_posix())
    if options.legacy_root and root.resolve() == options.legacy_root.resolve():
        previous = previous or options.existing.get(relative)
    if previous and not explicit:
        info = {"media_type": "episode", "series_key": "tvmaze:83", "series_title": "The Simpsons",
                "provider_ids_json": '{"tvmaze_show_id":83}', **previous,
                "storage_path": previous["path"],
                "language": override.get("language", options.language or previous.get("subtitle_language", "en"))}
        for source, target in (("title", "title"), ("summary", "summary"),
                               ("artwork_url", "image_original"), ("source_url", "tvmaze_url")):
            if source in override:
                info[target] = override[source]
        if options.title:
            info["title"] = options.title
        info["explicit_title"] = bool(options.title or override.get("title"))
        if override.get("wikipedia_title"):
            providers = json.loads(info["provider_ids_json"])
            providers["wikipedia_title"] = override["wikipedia_title"]
            info["provider_ids_json"] = json.dumps(providers)
        return info

    match = EPISODE_PATTERN.search(path.stem)
    season = override.get("season", int(match.group("s") or match.group("xs")) if match else 0)
    episode = override.get("episode", int(match.group("e") or match.group("xe")) if match else 0)
    kind = override.get("type", options.media_type)
    if kind == "auto":
        kind = "episode" if match or options.show_title or options.tvmaze_show_id else "transcript"
    show = override.get("show", options.show_title)
    year = override.get("year", options.year)
    provider_id = override.get("tvmaze_show_id", options.tvmaze_show_id)
    base_stem = re.sub(
        r"(?i)(?:[._-](?:en|eng|english|es|spa|fr|de|it|pt|ja|zh|ko|ru|ar|hi|nl|sv|pl|tr|sdh|cc|forced))+$",
        "", path.stem)
    stem = clean_title(base_stem)
    if kind == "episode":
        if not match and not ("season" in override and "episode" in override):
            raise ValueError(f"Set season and episode in a manifest for {path.name}")
        if not show and match:
            show = clean_title(path.stem[:match.start()]) or None
        if not show:
            folder = path.parent
            if re.fullmatch(r"(?i)(?:season|s)[ ._-]*\d+", folder.name):
                folder = folder.parent
            if folder.name.casefold() not in {"subtitles", "subtitle_cache", "subs"}:
                show = clean_title(folder.name)
        is_simpsons = provider_id == 83 or (show or "").casefold() in {"simpsons", "the simpsons"}
        if is_simpsons:
            show, provider_id = "The Simpsons", 83
        series_key = f"tvmaze:{provider_id}" if provider_id else stable_id("series", f"{show or path.parent.resolve()}|{year or ''}")
        code = f"s{season:02d}e{episode:02d}"
        media_id = code if is_simpsons else f"{stable_id('tv', series_key)}-{code}"
        inferred_title = clean_title(base_stem[match.end():]) if match else ""
        title = override.get("title", options.title) or inferred_title or f"Episode {code.upper()}"
    else:
        series_key, show = None, None
        title = override.get("title", options.title) or stem
        if kind == "movie":
            year_match = re.search(r"\b((?:19|20)\d{2})\b", title)
            if year_match and year is None:
                year = int(year_match.group(1))
                title = title[:year_match.start()].strip(" ()[]-") or title
            media_id = stable_id("movie", f"{title}|{year or ''}|{override.get('edition', '')}")
        else:
            media_id = stable_id("subtitle", os.path.normcase(path.resolve().as_posix()), case_sensitive=True)
        season = episode = 0
        provider_id = None
    providers = {"tvmaze_show_id": provider_id} if provider_id else {}
    if override.get("wikipedia_title"):
        providers["wikipedia_title"] = override["wikipedia_title"]
    return {
        "episode_code": media_id, "season": season, "episode": episode,
        "media_type": kind, "series_key": series_key, "series_title": show,
        "title": title, "release_year": year, "provider_ids_json": json.dumps(providers),
        "summary": override.get("summary"), "image_original": override.get("artwork_url"),
        "tvmaze_url": override.get("source_url"), "language": override.get("language", options.language or "en"),
        "explicit_title": bool(override.get("title") or options.title),
        "storage_path": previous["path"] if previous and previous["episode_code"] == media_id else path.resolve().as_posix(),
    }


def register_record(connection: sqlite3.Connection, info: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO episodes(episode_code, season, episode) VALUES (?, ?, ?) "
        "ON CONFLICT(episode_code) DO NOTHING",
        (info["episode_code"], info["season"], info["episode"]),
    )
    connection.execute(
        """UPDATE episodes SET media_type=?, series_key=?, series_title=COALESCE(?, series_title),
           release_year=COALESCE(?, release_year),
           title=CASE WHEN ? THEN ? ELSE COALESCE(title, ?) END, summary=COALESCE(?, summary),
           image_original=COALESCE(?, image_original), tvmaze_url=COALESCE(?, tvmaze_url),
           provider_ids_json=? WHERE episode_code=?""",
        (info["media_type"], info.get("series_key"), info.get("series_title"),
         info.get("release_year"), info.get("explicit_title", False), info.get("title"),
         info.get("title"), info.get("summary"), info.get("image_original"), info.get("tvmaze_url"),
         info.get("provider_ids_json", "{}"), info["episode_code"]),
    )


def migrate(connection: sqlite3.Connection) -> None:
    """Add columns only; existing IDs, rows, paths, and analyses stay intact."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(episodes)")}
    additions = {
        "media_type": "TEXT NOT NULL DEFAULT 'episode'",
        "series_key": "TEXT", "series_title": "TEXT", "release_year": "INTEGER",
        "provider_ids_json": "TEXT NOT NULL DEFAULT '{}'", "preferred_subtitle_id": "INTEGER",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE episodes ADD COLUMN {name} {declaration}")
    if "media_type" not in columns:
        connection.execute(
            "UPDATE episodes SET series_key='tvmaze:83', series_title='The Simpsons', "
            "provider_ids_json='{\"tvmaze_show_id\":83}'"
        )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_media_series ON episodes(series_key, season, episode)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_media_type ON episodes(media_type)")
    subtitle_columns = {row[1] for row in connection.execute("PRAGMA table_info(subtitle_files)")}
    if "language" not in subtitle_columns:
        connection.execute("ALTER TABLE subtitle_files ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
    # Keep per-file latest_analyses for resumable analysis. Browser views select
    # one complete analysis per media item, never a mixture of subtitle tracks.
    connection.execute("DROP VIEW IF EXISTS current_media_analyses")
    connection.execute("""CREATE VIEW current_media_analyses AS
        SELECT a.* FROM latest_analyses a WHERE a.id IN (
            SELECT id FROM (
                SELECT la.id, ROW_NUMBER() OVER (
                    PARTITION BY sf.episode_code ORDER BY
                    CASE WHEN sf.id=ep.preferred_subtitle_id THEN 1 ELSE 0 END DESC,
                    la.completed_at DESC, la.id DESC
                ) AS priority
                FROM latest_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
                JOIN episodes ep ON ep.episode_code=sf.episode_code
            ) WHERE priority=1
        )""")
    definitions = {
        "latest_episode_ratings": """SELECT sf.episode_code, sf.episode_code AS media_id,
            ep.season, ep.episode, ep.title, ep.airdate, ep.media_type, ep.series_key,
            ep.series_title, ep.release_year, sf.path AS subtitle_path,
            sf.id AS subtitle_file_id, la.id AS analysis_id,
            la.recommended_age, la.confidence, la.summary, la.parent_guidance,
            la.model, la.prompt_version, la.fuzzy_json_repaired, la.json_parse_method, la.completed_at
            FROM current_media_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
            JOIN episodes ep ON ep.episode_code=sf.episode_code""",
        "latest_category_ratings": """SELECT sf.episode_code, cr.category, cr.label,
            cr.polarity, cr.score, cr.explanation, la.id AS analysis_id
            FROM current_media_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
            JOIN category_ratings cr ON cr.analysis_id=la.id""",
        "latest_verified_evidence": """SELECT sf.episode_code, e.*, la.id AS analysis_id
            FROM current_media_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
            JOIN evidence e ON e.analysis_id=la.id WHERE e.transcript_verified=1""",
        "latest_content_advisories": """SELECT sf.episode_code, ca.id AS advisory_id, ca.*
            FROM current_media_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
            JOIN content_advisories ca ON ca.analysis_id=la.id""",
        "latest_age_band_guidance": """SELECT sf.episode_code, ab.*
            FROM current_media_analyses la JOIN subtitle_files sf ON sf.id=la.subtitle_file_id
            JOIN age_band_guidance ab ON ab.analysis_id=la.id""",
    }
    for name, definition in definitions.items():
        connection.execute(f"DROP VIEW IF EXISTS {name}")
        connection.execute(f"CREATE VIEW {name} AS {definition}")


def display_label(media: dict[str, Any]) -> str:
    if media.get("media_type") == "episode":
        return f"{media.get('series_title') or 'Unidentified series'} · S{media['season']:02d}E{media['episode']:02d}"
    kind = "Movie" if media.get("media_type") == "movie" else "Transcript"
    return f"{kind} · {media['release_year']}" if media.get("release_year") else kind
