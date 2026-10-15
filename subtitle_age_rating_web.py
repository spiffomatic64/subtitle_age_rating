#!/usr/bin/env python3
"""Local Flask browser for subtitle-based episode age ratings."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import subtitle_age_rating as analyzer
import subtitle_age_rating_media as media
from subtitle_age_rating_enrichment import (
    DEFAULT_ARTWORK_DIR,
    cache_all_artwork,
    cache_episode_sources,
    cache_one_artwork,
)


DEFAULT_DATABASE = Path("subtitle_age_ratings.sqlite3")
DEFAULT_SUBTITLE_DIR = Path("subtitle_cache")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5005

SENSITIVITY_LEVELS = (
    (0, "Avoid entirely", "Hard limit for any detected content"),
    (1, "Very uncomfortable", "Hard limit above negligible or incidental content"),
    (2, "Uncomfortable", "Strongly lower the title's suitability"),
    (3, "Somewhat comfortable", "Allow moderate content with a smaller penalty"),
    (4, "Comfortable", "Apply little or no penalty"),
)

PROFILE_GROUPS: dict[str, dict[str, Any]] = {
    "violence": {
        "label": "Violence & scariness",
        "category": "violence_scariness",
        "default": 2,
        "advisories": [
            "cartoon_fantasy_violence", "realistic_violence", "gore_injury",
            "peril_frightening_scenes", "cruelty_bullying_humiliation",
        ],
    },
    "sexual": {
        "label": "Sexual & romantic content",
        "category": "sex_romance_nudity",
        "default": 1,
        "advisories": [
            "romance_flirting", "sexual_innuendo", "sexual_behavior", "nudity_references",
            "consent_coercion_unsafe_behavior", "infidelity_mature_relationships",
        ],
    },
    "language": {
        "label": "Language",
        "category": "language",
        "default": 3,
        "advisories": ["mild_profanity", "strong_profanity", "insults_name_calling", "hate_speech_slurs"],
    },
    "substances": {
        "label": "Drinking, drugs & smoking",
        "category": "drinking_drugs_smoking",
        "default": 2,
        "advisories": ["alcohol", "tobacco", "drugs", "underage_substance_use", "substance_glamorization"],
    },
    "consumerism": {
        "label": "Products, purchases & advertising",
        "category": "products_purchases",
        "default": 3,
        "advisories": ["consumerism_advertising"],
    },
    "prejudice": {
        "label": "Prejudice & demeaning stereotypes",
        "category": None,
        "default": 1,
        "advisories": [
            "sexism_misogyny", "racism_xenophobia", "lgbtq_prejudice", "religious_prejudice",
            "disability_mental_health_stereotypes", "body_shaming", "age_class_stereotypes",
        ],
    },
    "mature_themes": {
        "label": "Mature themes & risky behavior",
        "category": None,
        "default": 2,
        "advisories": [
            "crime_scams_dishonesty", "dangerous_imitable_behavior", "infidelity_mature_relationships",
        ],
    },
}

ALLOWED_BY_LEVEL = {0: 0, 1: 1, 2: 2, 3: 3, 4: 5}
PENALTY_WEIGHT = {0: 5.0, 1: 4.0, 2: 2.5, 3: 1.25, 4: 0.25}
LABEL_ORDER = {"Good match": 0, "Worth it": 1, "Review first": 2, "Poor match": 3}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_array(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def default_sensitivities() -> dict[str, int]:
    return {key: int(info["default"]) for key, info in PROFILE_GROUPS.items()}


def normalize_sensitivities(value: Mapping[str, Any]) -> dict[str, int]:
    defaults = default_sensitivities()
    for key in defaults:
        try:
            level = int(value.get(key, defaults[key]))
        except (TypeError, ValueError):
            level = defaults[key]
        defaults[key] = min(4, max(0, level))
    return defaults


def initialize_database(path: Path) -> None:
    with analyzer.connect_database(path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO user_profile(
                id, display_name, age, sensitivities_json, advanced_json, updated_at
            ) VALUES (1, 'My child', 8, ?, '{}', ?)
            """,
            (json.dumps(default_sensitivities(), sort_keys=True), utc_now()),
        )
        connection.commit()


def scan_subtitle_quality(
    connection: sqlite3.Connection, subtitle_dir: Path = DEFAULT_SUBTITLE_DIR,
    options: media.ImportOptions | None = None,
) -> dict[str, int]:
    """Refresh parser/language quality records without contacting llama.cpp."""
    counts = {"good": 0, "warning": 0, "corrupt": 0, "error": 0}
    if options is None:
        database_file = connection.execute("PRAGMA database_list").fetchone()[2]
        options = media.ImportOptions(existing=media.existing_bindings(connection),
                                      legacy_root=Path(database_file).parent / "subtitle_cache")
    paths = analyzer.discover_subtitles(subtitle_dir, set(), options)
    for path in paths:
        try:
            document = analyzer.load_subtitle(path, subtitle_dir, options)
            subtitle_file_id = analyzer.upsert_subtitle(connection, document)
            status, issues, metrics = analyzer.assess_subtitle_quality(document)
            analyzer.save_subtitle_quality(connection, subtitle_file_id, status, issues, metrics)
            counts[status] += 1
        except (OSError, analyzer.UserFacingError):
            counts["error"] += 1
    connection.commit()
    return counts


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _dict_rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    for row in result:
        if "media_type" in row:
            row["media_label"] = media.display_label(row)
    return result


def get_profile(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        return {"display_name": "My child", "age": 8, "sensitivities": default_sensitivities(), "advanced": {}}
    profile = dict(row)
    profile["sensitivities"] = normalize_sensitivities(json_object(profile.pop("sensitivities_json")))
    profile["advanced"] = json_object(profile.pop("advanced_json"))
    return profile


def load_episode_facets(
    connection: sqlite3.Connection, episode_codes: Iterable[str]
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    codes = list(dict.fromkeys(episode_codes))
    categories: dict[str, dict[str, dict[str, Any]]] = {code: {} for code in codes}
    advisories: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    if not codes:
        return categories, advisories
    placeholders = ",".join("?" for _ in codes)
    for row in connection.execute(
        f"SELECT * FROM latest_category_ratings WHERE episode_code IN ({placeholders})", codes
    ):
        categories[row["episode_code"]][row["category"]] = dict(row)
    for row in connection.execute(
        f"SELECT * FROM latest_content_advisories WHERE episode_code IN ({placeholders})", codes
    ):
        advisories[row["episode_code"]].append(dict(row))
    return categories, advisories


def score_episode(
    episode: Mapping[str, Any],
    categories: Mapping[str, Mapping[str, Any]],
    advisories: list[Mapping[str, Any]],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    sensitivities = normalize_sensitivities(profile.get("sensitivities", {}))
    advanced = profile.get("advanced", {}) if isinstance(profile.get("advanced"), dict) else {}
    advisory_by_type = {str(item["advisory_type"]): item for item in advisories}
    reasons: list[dict[str, Any]] = []
    hard_violations: list[str] = []
    penalty = 0.0
    penalized_advisories: set[str] = set()

    for group_key, group in PROFILE_GROUPS.items():
        level = sensitivities[group_key]
        severity = 0
        category_key = group.get("category")
        if category_key and category_key in categories:
            severity = int(categories[category_key]["score"])
        present_advisories = []
        for advisory_type in group["advisories"]:
            advisory = advisory_by_type.get(advisory_type)
            if advisory is None:
                continue
            override = advanced.get(advisory_type)
            advisory_level = min(4, max(0, int(override))) if override is not None else level
            advisory_severity = int(advisory["severity"])
            severity = max(severity, advisory_severity)
            present_advisories.append(advisory)
            if advisory_level == 0 and advisory_severity > 0:
                hard_violations.append(str(advisory["label"]))
            elif advisory_level == 1 and advisory_severity > 1:
                hard_violations.append(str(advisory["label"]))
            if advisory_type not in penalized_advisories:
                penalty += (
                    max(0, advisory_severity - ALLOWED_BY_LEVEL[advisory_level])
                    * PENALTY_WEIGHT[advisory_level]
                    * 0.45
                )
                penalized_advisories.add(advisory_type)

        allowed = ALLOWED_BY_LEVEL[level]
        group_penalty = max(0, severity - allowed) * PENALTY_WEIGHT[level]
        penalty += group_penalty
        if level == 0 and severity > 0:
            hard_violations.append(str(group["label"]))
        elif level == 1 and severity > 1:
            hard_violations.append(str(group["label"]))
        if severity > allowed or present_advisories:
            reasons.append(
                {
                    "group": group_key,
                    "label": group["label"],
                    "severity": severity,
                    "comfort": level,
                    "penalty": round(group_penalty, 2),
                    "advisories": present_advisories,
                }
            )

    child_age = int(profile.get("age", 8))
    recommended_age = int(episode.get("recommended_age") or 18)
    age_gap = max(0, recommended_age - child_age)
    penalty += age_gap * 2.5
    positive_scores = [
        int(categories[key]["score"])
        for key in ("positive_messages", "positive_role_models", "educational_value", "diverse_representations")
        if key in categories
    ]
    positive_average = sum(positive_scores) / len(positive_scores) if positive_scores else 0.0

    if hard_violations or age_gap >= 4 or penalty >= 12:
        label = "Poor match"
    elif age_gap > 0 or penalty >= 2.0:
        label = "Review first"
    else:
        label = "Good match"
    if label == "Review first" and not hard_violations and age_gap <= 1 and penalty < 6 and positive_average >= 4:
        label = "Worth it"

    return {
        "label": label,
        "penalty": round(penalty, 2),
        "age_gap": age_gap,
        "positive_average": round(positive_average, 2),
        "hard_violations": sorted(set(hard_violations)),
        "reasons": sorted(reasons, key=lambda item: (-item["severity"], item["label"])),
    }


def exclude_corrupt_subtitle(score: dict[str, Any], quality_status: str | None) -> dict[str, Any]:
    """Prevent a stale analysis from recommending an episode with a corrupt transcript."""
    if quality_status != "corrupt":
        return score
    score = dict(score)
    score["label"] = "Poor match"
    score["hard_violations"] = sorted(
        set([*score.get("hard_violations", []), "Corrupt subtitle — analysis excluded"])
    )
    return score


def create_app(
    database_path: str | Path = DEFAULT_DATABASE,
    *,
    artwork_dir: str | Path = DEFAULT_ARTWORK_DIR,
    testing: bool = False,
) -> Flask:
    database = Path(database_path).resolve()
    artwork_root = Path(artwork_dir).resolve()
    initialize_database(database)
    app = Flask(__name__)
    app.config.update(
        DATABASE=str(database),
        ARTWORK_DIR=str(artwork_root),
        SECRET_KEY=os.environ.get("SUBTITLE_AGE_RATING_SECRET_KEY") or secrets.token_hex(32),
        TESTING=testing,
    )

    def db() -> sqlite3.Connection:
        if "database" not in g:
            g.database = _connect(Path(app.config["DATABASE"]))
        return g.database

    @app.teardown_appcontext
    def close_database(_error: BaseException | None) -> None:
        connection = g.pop("database", None)
        if connection is not None:
            connection.close()

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        return {
            "profile": get_profile(db()),
            "profile_groups": PROFILE_GROUPS,
            "sensitivity_levels": SENSITIVITY_LEVELS,
            "category_info": {key: {"label": label, "polarity": polarity} for key, label, polarity in analyzer.CATEGORIES},
            "advisory_info": analyzer.ADVISORY_INFO,
        }

    @app.get("/")
    def index() -> Any:
        connection = db()
        profile = get_profile(connection)
        query = request.args.get("q", "").strip()
        season = request.args.get("season", type=int)
        series = request.args.get("series", "")
        media_type = request.args.get("type", "")
        max_age = request.args.get("max_age", type=int)
        match_filter = request.args.get("match", "")
        favorite = request.args.get("favorite") == "1"
        watched = request.args.get("watched")
        sort = request.args.get("sort", "episode")
        page = max(1, request.args.get("page", 1, type=int))
        per_page = min(100, max(12, request.args.get("per_page", 36, type=int)))

        clauses = ["1 = 1"]
        parameters: list[Any] = []
        if series:
            clauses.append("ler.series_key = ?")
            parameters.append(series)
        if media_type in {"movie", "episode", "transcript"}:
            clauses.append("ler.media_type = ?")
            parameters.append(media_type)
        if season:
            clauses.append("ler.season = ?")
            parameters.append(season)
        if max_age:
            clauses.append("ler.recommended_age <= ?")
            parameters.append(max_age)
        if favorite:
            clauses.append("COALESCE(eus.favorite, 0) = 1")
        if watched in {"0", "1"}:
            clauses.append("COALESCE(eus.watched, 0) = ?")
            parameters.append(int(watched))
        if query:
            like = f"%{query}%"
            clauses.append(
                """(
                    ler.episode_code LIKE ? OR ler.title LIKE ? OR ler.summary LIKE ? OR
                    ler.parent_guidance LIKE ? OR ler.series_title LIKE ? OR EXISTS (
                        SELECT 1 FROM latest_category_ratings lcr
                        WHERE lcr.episode_code = ler.episode_code
                          AND (lcr.explanation LIKE ? OR lcr.label LIKE ?)
                    ) OR EXISTS (
                        SELECT 1 FROM latest_verified_evidence lve
                        WHERE lve.episode_code = ler.episode_code AND lve.excerpt LIKE ?
                    ) OR EXISTS (
                        SELECT 1 FROM latest_content_advisories lca
                        WHERE lca.episode_code = ler.episode_code
                          AND (lca.label LIKE ? OR lca.explanation LIKE ?)
                    )
                )"""
            )
            parameters.extend([like] * 10)

        rows = _dict_rows(
            connection.execute(
                f"""
                SELECT ler.*, ep.image_medium, ep.tvmaze_url,
                       COALESCE(eus.favorite, 0) AS favorite,
                       COALESCE(eus.watched, 0) AS watched,
                       COALESCE(eus.notes, '') AS notes,
                       ac.status AS artwork_status,
                       sq.status AS subtitle_quality_status,
                       sq.issues_json
                FROM latest_episode_ratings ler
                JOIN episodes ep ON ep.episode_code = ler.episode_code
                LEFT JOIN episode_user_state eus ON eus.episode_code = ler.episode_code
                LEFT JOIN artwork_cache ac ON ac.episode_code = ler.episode_code
                LEFT JOIN subtitle_files sf ON sf.path = ler.subtitle_path
                LEFT JOIN subtitle_quality sq ON sq.subtitle_file_id = sf.id
                WHERE {' AND '.join(clauses)}
                """,
                parameters,
            )
        )
        categories, advisories = load_episode_facets(connection, (row["episode_code"] for row in rows))
        enriched = []
        for row in rows:
            code = row["episode_code"]
            row["categories"] = categories.get(code, {})
            row["advisories"] = advisories.get(code, [])
            row["match"] = exclude_corrupt_subtitle(
                score_episode(row, row["categories"], row["advisories"], profile),
                row.get("subtitle_quality_status"),
            )
            row["quality_issues"] = json_array(row.pop("issues_json", None))
            if not match_filter or row["match"]["label"] == match_filter:
                enriched.append(row)

        if sort == "age":
            enriched.sort(key=lambda row: (row["recommended_age"], row["season"], row["episode"]))
        elif sort == "release_date":
            enriched.sort(
                key=lambda row: (
                    not bool(row.get("airdate")), row.get("airdate") or "",
                    row["season"], row["episode"],
                )
            )
        elif sort == "release_date_desc":
            enriched.sort(
                key=lambda row: (
                    bool(row.get("airdate")), row.get("airdate") or "",
                    row["season"], row["episode"],
                ),
                reverse=True,
            )
        elif sort == "episode":
            enriched.sort(key=lambda row: ((row.get("series_title") or row.get("title") or "").casefold(),
                                            row["season"], row["episode"]))
        elif sort == "confidence":
            enriched.sort(key=lambda row: (-row["confidence"], row["recommended_age"]))
        elif sort == "title":
            enriched.sort(
                key=lambda row: (
                    (row.get("title") or row["episode_code"]).casefold(),
                    row["season"], row["episode"],
                )
            )
        elif sort == "suitability":
            enriched.sort(
                key=lambda row: (
                    LABEL_ORDER[row["match"]["label"]], row["match"]["penalty"],
                    row["recommended_age"], -row["match"]["positive_average"],
                    row["season"], row["episode"],
                )
            )
        else:
            sort = "episode"
            enriched.sort(key=lambda row: (row["season"], row["episode"]))
        total = len(enriched)
        start = (page - 1) * per_page
        episodes = enriched[start : start + per_page]
        pages = max(1, (total + per_page - 1) // per_page)
        seasons = [row[0] for row in connection.execute(
            "SELECT DISTINCT season FROM episodes WHERE media_type='episode' AND (?='' OR series_key=?) ORDER BY season",
            (series, series))]
        series_choices = _dict_rows(connection.execute(
            "SELECT DISTINCT series_key, series_title FROM episodes WHERE series_key IS NOT NULL ORDER BY series_title"))
        base_args = request.args.to_dict()
        base_args.pop("fragment", None)
        previous_args = dict(base_args)
        next_args = dict(base_args)
        previous_args["page"] = max(1, page - 1)
        next_args["page"] = page + 1
        next_url = url_for("index", **next_args) if page < pages else None
        next_fragment_args = dict(next_args)
        next_fragment_args["fragment"] = 1
        next_fragment_url = url_for("index", **next_fragment_args) if page < pages else None
        if request.args.get("fragment") == "1":
            return jsonify(
                {
                    "html": render_template("_episode_cards.html", episodes=episodes),
                    "next_url": next_url,
                    "next_fragment_url": next_fragment_url,
                    "loaded": len(episodes),
                }
            )
        filter_args = dict(base_args)
        filter_args.pop("page", None)

        def without_filter(name: str) -> str:
            remaining = dict(filter_args)
            remaining.pop(name, None)
            return url_for("index", **remaining)

        active_filters: list[dict[str, str]] = []
        if series:
            series_name = next((row["series_title"] for row in series_choices if row["series_key"] == series), series)
            active_filters.append({"label": series_name or "Unidentified series", "url": without_filter("series")})
        if media_type:
            active_filters.append({"label": media_type.title(), "url": without_filter("type")})
        if query:
            active_filters.append({"label": f'Search: “{query}”', "url": without_filter("q")})
        if season:
            active_filters.append({"label": f"Season {season}", "url": without_filter("season")})
        if max_age:
            active_filters.append({"label": f"Base age ≤ {max_age}", "url": without_filter("max_age")})
        if match_filter:
            active_filters.append({"label": match_filter, "url": without_filter("match")})
        if favorite:
            active_filters.append({"label": "Favorites only", "url": without_filter("favorite")})
        if watched in {"0", "1"}:
            active_filters.append(
                {"label": "Watched" if watched == "1" else "Unwatched", "url": without_filter("watched")}
            )
        if sort != "episode":
            sort_labels = {
                "release_date": "Release date: oldest",
                "release_date_desc": "Release date: newest",
                "age": "Minimum base age",
                "suitability": "Personalized result",
                "confidence": "Highest confidence",
                "title": "Title",
            }
            active_filters.append(
                {"label": f"Sort: {sort_labels.get(sort, sort)}", "url": without_filter("sort")}
            )
        return render_template(
            "index.html", episodes=episodes, total=total, page=page, per_page=per_page,
            pages=pages, seasons=seasons, series_choices=series_choices,
            previous_args=previous_args, next_args=next_args,
            next_url=next_url, next_fragment_url=next_fragment_url,
            active_filters=active_filters,
            filters={"q": query, "season": season, "series": series, "type": media_type,
                     "max_age": max_age, "match": match_filter,
                     "favorite": favorite, "watched": watched, "sort": sort},
        )

    @app.get("/episode/<episode_code>")
    @app.get("/media/<episode_code>")
    def episode_detail(episode_code: str) -> str:
        connection = db()
        row = connection.execute(
            """
            SELECT ler.*, ep.*, la.limitations_json,
                   COALESCE(eus.favorite, 0) favorite,
                   COALESCE(eus.watched, 0) watched, COALESCE(eus.notes, '') notes,
                   sq.status subtitle_quality_status, sq.issues_json, sq.metrics_json
            FROM latest_episode_ratings ler
            JOIN episodes ep ON ep.episode_code = ler.episode_code
            JOIN subtitle_files sf ON sf.path = ler.subtitle_path
            JOIN current_media_analyses la ON la.id = ler.analysis_id
            LEFT JOIN episode_user_state eus ON eus.episode_code = ler.episode_code
            LEFT JOIN subtitle_quality sq ON sq.subtitle_file_id = sf.id
            WHERE ler.episode_code = ?
            """,
            (episode_code,),
        ).fetchone()
        if row is None:
            abort(404)
        episode = dict(row)
        episode["media_label"] = media.display_label(episode)
        categories, advisories = load_episode_facets(connection, [episode_code])
        episode["categories"] = categories.get(episode_code, {})
        episode["advisories"] = advisories.get(episode_code, [])
        episode["match"] = exclude_corrupt_subtitle(
            score_episode(
                episode, episode["categories"], episode["advisories"], get_profile(connection)
            ),
            episode.get("subtitle_quality_status"),
        )
        episode["limitations"] = json_array(episode.get("limitations_json"))
        episode["quality_issues"] = json_array(episode.get("issues_json"))
        episode["quality_metrics"] = json_object(episode.get("metrics_json"))
        evidence = _dict_rows(
            connection.execute(
                """
                SELECT e.category, e.excerpt, e.transcript_verified
                FROM evidence e JOIN current_media_analyses la ON la.id = e.analysis_id
                JOIN subtitle_files sf ON sf.id = la.subtitle_file_id
                WHERE sf.episode_code = ? ORDER BY e.category, e.id
                """,
                (episode_code,),
            )
        )
        evidence_by_category: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            evidence_by_category.setdefault(item["category"], []).append(item)
        advisory_evidence = {
            row["advisory_id"]: _dict_rows(
                connection.execute(
                    "SELECT excerpt, transcript_verified FROM advisory_evidence WHERE advisory_id = ?",
                    (row["advisory_id"],),
                )
            )
            for row in episode["advisories"]
        }
        age_bands = _dict_rows(
            connection.execute(
                "SELECT * FROM latest_age_band_guidance WHERE episode_code = ? ORDER BY min_age",
                (episode_code,),
            )
        )
        sources = _dict_rows(
            connection.execute(
                "SELECT * FROM episode_sources WHERE episode_code = ? ORDER BY source_name",
                (episode_code,),
            )
        )
        tracks = _dict_rows(connection.execute(
            """SELECT sf.id, sf.path, sf.language, la.id AS analysis_id
               FROM subtitle_files sf LEFT JOIN latest_analyses la ON la.subtitle_file_id=sf.id
               WHERE sf.episode_code=? ORDER BY sf.path""", (episode_code,)))
        for track in tracks:
            track["name"] = Path(track["path"]).name
        return render_template(
            "episode.html", episode=episode, evidence=evidence_by_category,
            advisory_evidence=advisory_evidence, age_bands=age_bands, sources=sources, tracks=tracks,
        )

    @app.get("/compare")
    def compare() -> str:
        codes = [code for code in request.args.getlist("episode") if code][:4]
        connection = db()
        episodes = []
        if codes:
            placeholders = ",".join("?" for _ in codes)
            found = {
                row["episode_code"]: {**dict(row), "media_label": media.display_label(dict(row))}
                for row in connection.execute(
                    f"SELECT * FROM latest_episode_ratings WHERE episode_code IN ({placeholders})", codes
                )
            }
            categories, advisories = load_episode_facets(connection, codes)
            profile = get_profile(connection)
            for code in codes:
                if code not in found:
                    continue
                episode = found[code]
                episode["categories"] = categories.get(code, {})
                episode["advisories"] = advisories.get(code, [])
                episode["match"] = score_episode(episode, episode["categories"], episode["advisories"], profile)
                episodes.append(episode)
        choices = _dict_rows(
            connection.execute(
                "SELECT * FROM latest_episode_ratings ORDER BY series_title, season, episode, title"
            )
        )
        return render_template("compare.html", episodes=episodes, choices=choices, selected=codes)

    @app.route("/profile", methods=["GET", "POST"])
    def profile_settings() -> str:
        connection = db()
        if request.method == "POST":
            display_name = request.form.get("display_name", "My child").strip()[:80] or "My child"
            age = min(17, max(2, request.form.get("age", 8, type=int)))
            sensitivities = {
                key: min(4, max(0, request.form.get(f"sensitivity_{key}", info["default"], type=int)))
                for key, info in PROFILE_GROUPS.items()
            }
            advanced = {}
            for advisory_type in analyzer.ADVISORY_KEYS:
                value = request.form.get(f"advanced_{advisory_type}")
                if value not in (None, ""):
                    try:
                        advanced[advisory_type] = min(4, max(0, int(value)))
                    except ValueError:
                        continue
            connection.execute(
                """
                INSERT INTO user_profile(id, display_name, age, sensitivities_json, advanced_json, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    age = excluded.age,
                    sensitivities_json = excluded.sensitivities_json,
                    advanced_json = excluded.advanced_json,
                    updated_at = excluded.updated_at
                """,
                (display_name, age, json.dumps(sensitivities, sort_keys=True), json.dumps(advanced, sort_keys=True), utc_now()),
            )
            connection.commit()
            flash("Preferences saved.", "success")
            return redirect(url_for("profile_settings"))
        return render_template("profile.html", current=get_profile(connection))

    @app.post("/episode/<episode_code>/state")
    @app.post("/media/<episode_code>/state")
    def update_episode_state(episode_code: str):
        connection = db()
        if connection.execute("SELECT 1 FROM episodes WHERE episode_code = ?", (episode_code,)).fetchone() is None:
            abort(404)
        favorite = int(request.form.get("favorite") == "1")
        watched = int(request.form.get("watched") == "1")
        notes = request.form.get("notes", "")[:10000]
        connection.execute(
            """
            INSERT INTO episode_user_state(episode_code, favorite, watched, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(episode_code) DO UPDATE SET
                favorite = excluded.favorite, watched = excluded.watched,
                notes = excluded.notes, updated_at = excluded.updated_at
            """,
            (episode_code, favorite, watched, notes, utc_now()),
        )
        connection.commit()
        flash("Notes updated.", "success")
        return redirect(request.form.get("next") or url_for("episode_detail", episode_code=episode_code))

    @app.post("/media/<episode_code>/subtitle")
    def select_subtitle(episode_code: str):
        connection = db()
        subtitle_id = request.form.get("subtitle_id", type=int)
        track = connection.execute(
            """SELECT sf.id FROM subtitle_files sf JOIN latest_analyses la ON la.subtitle_file_id=sf.id
               WHERE sf.episode_code=? AND sf.id=?""", (episode_code, subtitle_id)).fetchone()
        if track is None:
            abort(400)
        connection.execute("UPDATE episodes SET preferred_subtitle_id=? WHERE episode_code=?", (subtitle_id, episode_code))
        connection.commit()
        flash("Subtitle track selected.", "success")
        return redirect(url_for("episode_detail", episode_code=episode_code))

    @app.get("/artwork/<episode_code>")
    def artwork(episode_code: str):
        connection = db()
        row = connection.execute(
            "SELECT local_path, status FROM artwork_cache WHERE episode_code = ?", (episode_code,)
        ).fetchone()
        path = Path(row["local_path"]) if row and row["status"] == "success" and row["local_path"] else None
        if path is None or not path.is_file():
            try:
                path = cache_one_artwork(
                    connection, episode_code, artwork_dir=Path(app.config["ARTWORK_DIR"])
                )
            except RuntimeError:
                path = None
        if path is None or not path.is_file():
            return send_file(Path(app.static_folder) / "placeholder.svg", mimetype="image/svg+xml")
        return send_file(path, conditional=True, max_age=86400)

    @app.get("/status")
    def status() -> str:
        connection = db()
        counts = {
            "episodes": connection.execute("SELECT count(*) FROM episodes").fetchone()[0],
            "latest": connection.execute("SELECT count(*) FROM latest_analyses").fetchone()[0],
            "errors": connection.execute("SELECT count(*) FROM analyses WHERE status = 'error'").fetchone()[0],
            "advisories": connection.execute("SELECT count(*) FROM latest_content_advisories").fetchone()[0],
            "fuzzy repaired": connection.execute(
                "SELECT count(*) FROM latest_analyses WHERE fuzzy_json_repaired = 1"
            ).fetchone()[0],
            "sources": connection.execute("SELECT count(*) FROM episode_sources WHERE status = 'success'").fetchone()[0],
            "artwork": connection.execute("SELECT count(*) FROM artwork_cache WHERE status = 'success'").fetchone()[0],
            "corrupt": connection.execute("SELECT count(*) FROM subtitle_quality WHERE status = 'corrupt'").fetchone()[0],
            "warnings": connection.execute("SELECT count(*) FROM subtitle_quality WHERE status = 'warning'").fetchone()[0],
        }
        runs = _dict_rows(connection.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 12"))
        quality = _dict_rows(
            connection.execute(
                """
                SELECT sf.episode_code, sf.path, sq.status, sq.issues_json
                FROM subtitle_quality sq JOIN subtitle_files sf ON sf.id = sq.subtitle_file_id
                WHERE sq.status != 'good' ORDER BY sf.episode_code
                """
            )
        )
        for row in quality:
            row["issues"] = json_array(row.pop("issues_json"))
        return render_template("status.html", counts=counts, runs=runs, quality=quality)

    @app.get("/methodology")
    def methodology() -> str:
        return render_template("methodology.html")

    @app.get("/api/episodes")
    @app.get("/api/media")
    def episodes_api():
        connection = db()
        rows = _dict_rows(connection.execute("SELECT * FROM latest_episode_ratings ORDER BY season, episode"))
        categories, advisories = load_episode_facets(connection, (row["episode_code"] for row in rows))
        profile = get_profile(connection)
        for row in rows:
            code = row["episode_code"]
            row["categories"] = categories.get(code, {})
            row["advisories"] = advisories.get(code, [])
            row["match"] = score_episode(row, row["categories"], row["advisories"], profile)
        key = "media" if request.path == "/api/media" else "episodes"
        return jsonify({key: rows, "count": len(rows)})

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse subtitle age ratings in a local Flask application.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--cache-artwork", action="store_true")
    parser.add_argument("--cache-sources", action="store_true")
    parser.add_argument("--scan-subtitles", action="store_true")
    parser.add_argument("--subtitle-dir", type=Path, default=DEFAULT_SUBTITLE_DIR)
    parser.add_argument("--force-cache", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    initialize_database(args.database)
    if args.init_db and not (args.cache_artwork or args.cache_sources or args.scan_subtitles):
        print(f"Initialized application schema in {args.database}")
        return 0
    if args.cache_sources or args.cache_artwork or args.scan_subtitles:
        connection = _connect(args.database)
        try:
            if args.scan_subtitles:
                print(scan_subtitle_quality(connection, args.subtitle_dir))
            if args.cache_sources:
                print(cache_episode_sources(connection, force=args.force_cache, limit=args.limit))
            if args.cache_artwork:
                print(
                    cache_all_artwork(
                        connection, artwork_dir=DEFAULT_ARTWORK_DIR,
                        force=args.force_cache, limit=args.limit,
                    )
                )
        finally:
            connection.close()
        return 0
    app = create_app(args.database, artwork_dir=DEFAULT_ARTWORK_DIR)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
