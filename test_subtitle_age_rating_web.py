from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

import subtitle_age_rating as analyzer
from subtitle_age_rating_web import create_app, default_sensitivities, score_episode
import subtitle_age_rating_web as web
from test_subtitle_age_rating_core import valid_result


def seed_database(path: Path) -> None:
    with analyzer.connect_database(path) as connection:
        analyzer.register_prompt(connection)
        connection.execute(
            """INSERT INTO episodes(episode_code, season, episode, title, airdate, summary)
               VALUES ('s01e01', 1, 1, 'Simpsons Roasting on an Open Fire', '1989-12-17',
                       'The family adopts Santa’s Little Helper.')"""
        )
        connection.execute(
            """INSERT INTO subtitle_files(
                   episode_code, path, format, sha256, byte_size, modified_ns, encoding,
                   cue_count, transcript_chars, first_seen_at, last_seen_at
               ) VALUES ('s01e01', 's01e01.srt', '.srt', 'abc', 100, 1, 'utf-8', 10, 100,
                         '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO analysis_runs(
                   started_at, completed_at, status, model, server_url, prompt_version,
                   settings_json, requested_count, succeeded_count
               ) VALUES ('2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 'completed',
                         'test-model', 'http://localhost', ?, '{}', 1, 1)""",
            (analyzer.PROMPT_VERSION,),
        )
        connection.execute(
            """INSERT INTO analyses(
                   run_id, subtitle_file_id, source_sha256, model, prompt_version, status,
                   started_at, completed_at, recommended_age, confidence, summary,
                   parent_guidance, limitations_json, raw_response
               ) VALUES (1, 1, 'abc', 'test-model', ?, 'success',
                         '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 8, .9,
                         'A warm holiday story.', 'Some insults and mild peril.',
                         '[\"Visual-only content cannot be assessed.\"]', '{}')""",
            (analyzer.PROMPT_VERSION,),
        )
        for key, label, polarity in analyzer.CATEGORIES:
            score = 4 if key in {"positive_messages", "positive_role_models"} else 0
            connection.execute(
                """INSERT INTO category_ratings(
                       analysis_id, category, label, polarity, score, explanation
                   ) VALUES (1, ?, ?, ?, ?, ?)""",
                (key, label, polarity, score, f"Explanation for {label}"),
            )
        connection.execute(
            """INSERT INTO evidence(analysis_id, category, excerpt, transcript_verified)
               VALUES (1, 'language', 'You little monster!', 1)"""
        )
        advisory_id = connection.execute(
            """INSERT INTO content_advisories(
                   analysis_id, advisory_type, label, parent_category, severity,
                   frequency, context, explanation
               ) VALUES (1, 'insults_name_calling', 'Insults and name-calling', 'language',
                         2, 'occasional', 'challenged', 'Family members trade mild insults.')"""
        ).lastrowid
        connection.execute(
            """INSERT INTO advisory_evidence(advisory_id, excerpt, transcript_verified)
               VALUES (?, 'You little monster!', 1)""",
            (advisory_id,),
        )
        for key, label, min_age, max_age in analyzer.AGE_BANDS:
            suitability = "appropriate" if min_age >= 6 else "review"
            connection.execute(
                """INSERT INTO age_band_guidance(
                       analysis_id, age_band, label, min_age, max_age, suitability, explanation
                   ) VALUES (1, ?, ?, ?, ?, ?, 'Age-specific explanation.')""",
                (key, label, min_age, max_age, suitability),
            )
        connection.execute(
            """INSERT INTO subtitle_quality(subtitle_file_id, status, issues_json, metrics_json, checked_at)
               VALUES (1, 'good', '[]', '{}', '2026-01-01T00:00:00Z')"""
        )
        connection.commit()


def make_client(tmp_path: Path):
    database = tmp_path / "ratings.sqlite3"
    seed_database(database)
    app = create_app(database, artwork_dir=tmp_path / "artwork", testing=True)
    return app.test_client(), database


def test_session_keys_are_random_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SUBTITLE_AGE_RATING_SECRET_KEY", raising=False)
    database = tmp_path / "ratings.sqlite3"
    first = create_app(database, testing=True)
    second = create_app(database, testing=True)
    assert len(first.secret_key) >= 64
    assert first.secret_key != second.secret_key


def test_session_key_can_be_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUBTITLE_AGE_RATING_SECRET_KEY", "test-session-signing-key")
    app = create_app(tmp_path / "ratings.sqlite3", testing=True)
    assert app.secret_key == "test-session-signing-key"


def test_main_routes_and_search(tmp_path: Path) -> None:
    client, _database = make_client(tmp_path)
    for url in ("/", "/status", "/profile", "/compare", "/methodology", "/api/episodes"):
        assert client.get(url).status_code == 200
    response = client.get("/?q=monster")
    assert b"Simpsons Roasting" in response.data
    filtered_response = client.get("/?q=monster&season=1&sort=age")
    assert b'aria-label="Active filters"' in filtered_response.data
    assert "Search: “monster”".encode() in filtered_response.data
    assert b"Season 1" in filtered_response.data
    assert b"Sort: Minimum base age" in filtered_response.data
    default_response = client.get("/")
    assert b'data-auto-submit' in default_response.data
    assert b'value="episode" selected' in default_response.data
    assert b'id="episode-results"' in default_response.data
    fragment_response = client.get("/?fragment=1")
    assert fragment_response.status_code == 200
    assert fragment_response.is_json
    fragment = fragment_response.get_json()
    assert b"Simpsons Roasting" in fragment["html"].encode()
    assert fragment["loaded"] == 1
    assert fragment["next_url"] is None
    assert fragment["next_fragment_url"] is None
    for sort in (
        "episode", "release_date", "release_date_desc", "age",
        "suitability", "confidence", "title", "invalid",
    ):
        assert client.get(f"/?sort={sort}").status_code == 200
    assert client.get("/episode/s01e01").status_code == 200
    assert b"Visual-only content" in client.get("/episode/s01e01").data
    assert client.get("/episode/s99e99").status_code == 404


@pytest.mark.parametrize("query", ["s06e02", "S6E2", "6x02", " s006e002 "])
def test_episode_code_search_finds_each_series_and_respects_filters(tmp_path: Path, query: str) -> None:
    client, database = make_client(tmp_path)
    with analyzer.connect_database(database) as connection:
        for code, series in (("s06e02", "The Simpsons"), ("other-show-s06e02", "Other Show")):
            document = analyzer.SubtitleDocument(
                path=tmp_path / f"{code}.srt", relative_path=f"{code}.srt", episode_code=code,
                season=6, episode=2, sha256=code, byte_size=10, modified_ns=1,
                encoding="utf-8", transcript="Exact dialogue.", cue_count=1,
            )
            subtitle_id = analyzer.upsert_subtitle(connection, document)
            connection.execute("UPDATE episodes SET series_key=?, series_title=?, title=? WHERE episode_code=?",
                               (series, series, f"{series} episode", code))
            analysis_id = analyzer.begin_analysis(connection, 1, subtitle_id, document, "test-model")
            analyzer.save_success(connection, analysis_id, valid_result(), "{}", document.transcript, 1)
    result = client.get("/", query_string={"q": query})
    assert result.status_code == 200
    assert result.data.count(b'class="episode-card"') == 2
    assert b"Simpsons Roasting" not in result.data
    filtered = client.get("/", query_string={"q": query, "series": "Other Show"})
    assert filtered.data.count(b'class="episode-card"') == 1
    assert b"Other Show episode" in filtered.data
    for filters in ({"season": 5}, {"type": "movie"}):
        assert b'class="episode-card"' not in client.get("/", query_string={"q": query, **filters}).data


def test_search_preserves_categories_advisories_and_verified_dialogue(tmp_path: Path) -> None:
    client, database = make_client(tmp_path)
    with analyzer.connect_database(database) as connection:
        connection.execute("UPDATE category_ratings SET explanation='categoryneedle' WHERE category='language'")
        connection.execute("UPDATE content_advisories SET explanation='advisoryneedle'")
        connection.execute("UPDATE evidence SET excerpt='dialogueneedle', transcript_verified=1")
        connection.commit()
    for query in ("categoryneedle", "advisoryneedle", "dialogueneedle", "Insults and name-calling"):
        response = client.get("/", query_string={"q": query})
        assert response.status_code == 200
        assert response.data.count(b'class="episode-card"') == 1
    with analyzer.connect_database(database) as connection:
        connection.execute("UPDATE evidence SET transcript_verified=0")
        connection.commit()
    assert b'class="episode-card"' not in client.get("/?q=dialogueneedle").data


def test_search_uses_current_selected_analysis_not_history_or_other_tracks(tmp_path: Path) -> None:
    client, database = make_client(tmp_path)
    with analyzer.connect_database(database) as connection:
        for path, needle in (("s01e01.srt", "currenttrackneedle"), ("alternate.srt", "alternatetrackneedle")):
            document = analyzer.SubtitleDocument(
                tmp_path / path, path, "s01e01", 1, 1, "abc", 10, 1, "utf-8", needle, 1)
            subtitle_id = analyzer.upsert_subtitle(connection, document)
            analysis_id = analyzer.begin_analysis(connection, 1, subtitle_id, document, "test-model")
            result = valid_result()
            result["categories"]["language"]["explanation"] = needle
            analyzer.save_success(connection, analysis_id, result, "{}", document.transcript, 1)
    assert b'class="episode-card"' not in client.get("/?q=monster").data
    assert b'class="episode-card"' not in client.get("/?q=currenttrackneedle").data
    assert client.get("/?q=alternatetrackneedle").data.count(b'class="episode-card"') == 1
    assert client.post("/media/s01e01/subtitle", data={"subtitle_id": 1}).status_code == 302
    assert client.get("/?q=currenttrackneedle").data.count(b'class="episode-card"') == 1
    assert b'class="episode-card"' not in client.get("/?q=alternatetrackneedle").data
    assert b'class="episode-card"' not in client.get("/?q=monster").data


def test_episode_code_search_does_not_read_dialogue_table(tmp_path: Path, monkeypatch) -> None:
    client, _database = make_client(tmp_path)
    original = web._connect
    def connect(path):
        connection = original(path)
        def authorize(action, table, _column, _database, _source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "evidence" else sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return connection
    monkeypatch.setattr(web, "_connect", connect)
    response = client.get("/?q=s01e01")
    assert response.status_code == 200
    assert response.data.count(b'class="episode-card"') == 1


def test_profile_and_episode_state_persist(tmp_path: Path) -> None:
    client, database = make_client(tmp_path)
    form = {"display_name": "Alex", "age": "11"}
    form.update({f"sensitivity_{key}": "2" for key in default_sensitivities()})
    response = client.post("/profile", data=form)
    assert response.status_code == 302
    response = client.post(
        "/episode/s01e01/state",
        data={"favorite": "1", "watched": "1", "notes": "A family favorite"},
    )
    assert response.status_code == 302
    with analyzer.connect_database(database) as connection:
        profile = connection.execute("SELECT display_name, age FROM user_profile WHERE id=1").fetchone()
        state = connection.execute("SELECT favorite, watched, notes FROM episode_user_state").fetchone()
    assert tuple(profile) == ("Alex", 11)
    assert tuple(state) == (1, 1, "A family favorite")


def test_scoring_hard_limit_and_positive_tiebreaker() -> None:
    episode = {"recommended_age": 9}
    categories = {
        "positive_messages": {"score": 5},
        "positive_role_models": {"score": 5},
        "educational_value": {"score": 4},
        "diverse_representations": {"score": 4},
    }
    profile = {"age": 8, "sensitivities": default_sensitivities(), "advanced": {}}
    result = score_episode(episode, categories, [], profile)
    assert result["label"] == "Worth it"

    advisory = {
        "advisory_type": "sexism_misogyny", "label": "Sexism or misogyny", "severity": 2
    }
    result = score_episode(episode, categories, [advisory], profile)
    assert result["label"] == "Poor match"
    assert "Sexism or misogyny" in result["hard_violations"]


def test_corrupt_subtitle_is_excluded(tmp_path: Path) -> None:
    client, database = make_client(tmp_path)
    with analyzer.connect_database(database) as connection:
        connection.execute(
            "UPDATE subtitle_quality SET status='corrupt', issues_json=?",
            (json.dumps(["Detected non-English text"]),),
        )
        connection.commit()
    response = client.get("/")
    assert b"Corrupt subtitle" in response.data
    assert b"Poor match" in response.data


def test_fuzzy_repair_provenance_is_visible_and_selectable(tmp_path: Path) -> None:
    client, database = make_client(tmp_path)
    with analyzer.connect_database(database) as connection:
        connection.execute(
            "UPDATE analyses SET fuzzy_json_repaired=1, json_parse_method='repaired JSON' WHERE id=1"
        )
        connection.commit()
        assert analyzer.latest_fuzzy_repaired_paths(connection) == {"s01e01.srt"}
    response = client.get("/episode/s01e01")
    assert response.status_code == 200
    assert b"Fuzzy JSON repair used" in response.data
    assert b"repaired JSON" in response.data
