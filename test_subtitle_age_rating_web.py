from __future__ import annotations

import json
from pathlib import Path

import subtitle_age_rating as analyzer
from subtitle_age_rating_web import create_app, default_sensitivities, score_episode


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
