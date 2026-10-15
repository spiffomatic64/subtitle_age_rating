from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import subtitle_age_rating as analyzer
import subtitle_age_rating_media as media
import subtitle_age_rating_enrichment as enrichment
from subtitle_age_rating_web import create_app, scan_subtitle_quality
from test_subtitle_age_rating_core import valid_result
from test_subtitle_age_rating_web import seed_database


def write_subtitle(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello there. This is a family story about kindness and friendship.\n", encoding="utf-8")
    return path


def snapshot(connection):
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if name == "schema_metadata":
            continue
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
        result[name] = (columns, [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')])
    return result


def test_v3_migration_preserves_all_existing_rows_and_legacy_cache(tmp_path, monkeypatch):
    database = tmp_path / "ratings.sqlite3"
    # Build the actual previous schema by disabling only the additive migration.
    with monkeypatch.context() as old:
        old.setattr(media, "migrate", lambda _connection: None)
        old.setattr(analyzer, "SCHEMA_VERSION", 3)
        old.setattr(analyzer, "PROMPT_VERSION", "csm-subtitle-v5")
        old.setattr(analyzer, "SYSTEM_PROMPT", "Original stored Simpsons prompt")
        seed_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user_profile VALUES(1, 'Test child', 11, '{}', '{}', 'original')")
        connection.execute("INSERT INTO episode_user_state VALUES('s01e01', 1, 1, 'Keep this note', 'original')")
        connection.execute("INSERT INTO artwork_cache(episode_code, local_path, status, updated_at) VALUES('s01e01','original.jpg','success','original')")
        connection.commit()
        before = snapshot(connection)

    with analyzer.connect_database(database) as connection:
        for name, (columns, rows) in before.items():
            projection = ','.join(f'"{col}"' for col in columns)
            assert [tuple(row) for row in connection.execute(f'SELECT {projection} FROM "{name}" ORDER BY rowid')] == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = connection.execute("SELECT * FROM episodes").fetchone()
        assert row["episode_code"] == "s01e01"
        assert row["series_key"] == "tvmaze:83"
        assert analyzer.already_analyzed(connection, 1, "abc", "test-model")
        assert analyzer.current_analysis_requirement(connection, "s01e01.srt", "abc") is None
        analyzer.register_prompt(connection)
        assert connection.execute("SELECT system_prompt FROM prompts WHERE version='csm-subtitle-v5'").fetchone()[0] == "Original stored Simpsons prompt"
        options = media.ImportOptions(existing=media.existing_bindings(connection), legacy_root=tmp_path / "subtitle_cache")
        info = media.identify(tmp_path / "subtitle_cache" / "s01e01.srt", tmp_path / "subtitle_cache", options)
        assert info["episode_code"] == "s01e01"
        assert info["storage_path"] == "s01e01.srt"

    with analyzer.connect_database(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    backups = list(tmp_path.glob("*.backup-media-v4-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert "media_type" not in {row[1] for row in backup.execute("PRAGMA table_info(episodes)")}
        assert backup.execute("SELECT notes FROM episode_user_state").fetchone()[0] == "Keep this note"
    client = create_app(database, artwork_dir=tmp_path / "artwork", testing=True).test_client()
    assert client.get("/episode/s01e01").status_code == 200
    assert b"Keep this note" in client.get("/media/s01e01").data


def test_discovery_accepts_movies_plain_text_and_both_episode_styles(tmp_path):
    paths = [write_subtitle(tmp_path / name) for name in (
        "Movie.2020.en.srt", "interview.txt", "First.Show.S01E01.srt", "Second.Show.1x01.srt")]
    assert set(analyzer.discover_subtitles(tmp_path, set())) == set(paths)
    ids = [analyzer.load_subtitle(path, tmp_path).episode_code for path in paths]
    assert len(set(ids)) == 4
    assert analyzer.load_subtitle(paths[0], tmp_path).media_info["media_type"] == "transcript"
    first = analyzer.load_subtitle(paths[2], tmp_path)
    second = analyzer.load_subtitle(paths[3], tmp_path)
    assert first.media_info["series_title"] == "First Show"
    assert second.media_info["series_title"] == "Second Show"
    assert (first.season, first.episode) == (second.season, second.episode) == (1, 1)


def test_movie_years_and_identical_relative_paths_do_not_collide(tmp_path):
    first = write_subtitle(tmp_path / "first" / "movie.en.srt")
    second = write_subtitle(tmp_path / "second" / "movie.en.srt")
    old = analyzer.load_subtitle(first, first.parent, media.ImportOptions(media_type="movie", title="A remake", year=1990))
    new = analyzer.load_subtitle(second, second.parent, media.ImportOptions(media_type="movie", title="A remake", year=2020))
    assert old.episode_code != new.episode_code
    assert old.relative_path != new.relative_path
    with analyzer.connect_database(tmp_path / "ratings.sqlite3") as connection:
        assert analyzer.upsert_subtitle(connection, old) != analyzer.upsert_subtitle(connection, new)


def test_manifest_maps_unstructured_names_and_explicit_sources(tmp_path):
    path = write_subtitle(tmp_path / "01.srt")
    manifest_path = tmp_path / "metadata.json"
    manifest_path.write_text(json.dumps({"files": {"01.srt": {
        "type": "episode", "show": "Example Show", "season": 1, "episode": 2,
        "title": "Pilot", "tvmaze_show_id": 999, "language": "es",
        "wikipedia_title": "Explicit episode page"}}}), encoding="utf-8")
    options = media.ImportOptions(manifest=media.load_manifest(manifest_path))
    document = analyzer.load_subtitle(path, tmp_path, options)
    assert document.media_info["title"] == "Pilot"
    assert document.language == "es"
    assert document.episode == 2
    assert document.media_info["series_key"] == "tvmaze:999"
    assert json.loads(document.media_info["provider_ids_json"])["wikipedia_title"] == "Explicit episode page"


def test_quality_language_is_configurable_and_utf16_is_decoded(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "langdetect", SimpleNamespace(
        DetectorFactory=SimpleNamespace(seed=None), detect_langs=lambda text: [SimpleNamespace(lang="es", prob=1.0)]))
    path = tmp_path / "spanish.txt"
    path.write_text("Una historia sobre la familia y la amistad.", encoding="utf-16")
    document = analyzer.load_subtitle(path, tmp_path, media.ImportOptions(language="auto"))
    assert document.encoding == "utf-16"
    assert analyzer.assess_subtitle_quality(document)[0] == "good"
    english = analyzer.load_subtitle(path, tmp_path, media.ImportOptions(language="en"))
    assert analyzer.assess_subtitle_quality(english)[0] == "corrupt"


def test_long_transcripts_cover_all_input_and_merge_conservatively(tmp_path, monkeypatch):
    text = ("A quiet scene with family and friends.\n" * 70) + "A mature final scene."
    chunks = analyzer.split_transcript(text, 1000)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 1000 for chunk in chunks)
    document = analyzer.SubtitleDocument(tmp_path / "film.txt", "film.txt", "film", 0, 0,
                                         "abc", len(text), 1, "utf-8", text, 1)
    seen = []
    def analyze_segment(document, metadata, args):
        seen.append(document.transcript)
        result = valid_result()
        if "mature final" in document.transcript:
            result["recommended_age"] = 16
            result["categories"]["violence_scariness"]["score"] = 4
        return result, json.dumps(result)
    monkeypatch.setattr(analyzer, "request_single_analysis", analyze_segment)
    result, raw = analyzer.request_analysis(document, {}, SimpleNamespace(max_transcript_chars=1000))
    assert "".join(seen) == text
    assert result["recommended_age"] == 16
    assert result["categories"]["violence_scariness"]["score"] == 4
    assert "segments separately" in result["limitations"][0]
    assert len(json.loads(raw)["segments"]) == len(chunks)


def test_generic_media_does_not_fetch_simpsons_metadata(tmp_path, monkeypatch):
    root = tmp_path / "subtitles"
    write_subtitle(root / "Movie.2020.srt")
    calls = []
    monkeypatch.setattr(analyzer, "preflight_server", lambda *args: ["test"])
    monkeypatch.setattr(analyzer, "fetch_tvmaze_metadata", lambda *args: pytest.fail("Unexpected metadata lookup"))
    monkeypatch.setattr(enrichment, "PoliteClient", lambda: pytest.fail("Unexpected online source lookup"))
    monkeypatch.setattr(analyzer, "assess_subtitle_quality", lambda document: ("good", [], {}))
    def analyze(document, metadata, args):
        calls.append(document.episode_code)
        return valid_result(), json.dumps(valid_result())
    monkeypatch.setattr(analyzer, "request_analysis", analyze)
    args = analyzer.parse_args(["--subtitle-dir", str(root), "--database", str(tmp_path / "ratings.sqlite3"),
                                "--media-type", "movie", "--model", "test"])
    assert analyzer.run(args) == 0
    assert analyzer.run(args) == 0
    assert len(calls) == 1
    client = create_app(args.database, testing=True).test_client()
    items = client.get("/api/media").get_json()["media"]
    assert len(items) == 1
    assert items[0]["title"] == "Movie"
    assert items[0]["release_year"] == 2020
    assert items[0]["media_type"] == "movie"
    assert client.get("/?type=movie").status_code == 200
    assert b"Movie" in client.get("/media/" + calls[0]).data


def test_subtitle_variants_use_one_consistent_analysis_and_can_be_selected(tmp_path):
    database = tmp_path / "ratings.sqlite3"
    seed_database(database)
    document = analyzer.SubtitleDocument(tmp_path / "alternate.srt", "alternate.srt", "s01e01", 1, 1,
                                         "different", 10, 1, "utf-8", "Different evidence.", 1)
    with analyzer.connect_database(database) as connection:
        subtitle_id = analyzer.upsert_subtitle(connection, document)
        analysis_id = analyzer.begin_analysis(connection, 1, subtitle_id, document, "test-model")
        result = valid_result()
        result["recommended_age"] = 17
        result["categories"]["language"] = {"score": 5, "explanation": "Different track warning",
                                            "evidence": [{"excerpt": "Different evidence."}]}
        analyzer.save_success(connection, analysis_id, result, json.dumps(result), document.transcript, 1)
        assert connection.execute("SELECT COUNT(*) FROM latest_episode_ratings").fetchone()[0] == 1
    client = create_app(database, testing=True).test_client()
    data = client.get("/media/s01e01").data
    assert b"Different track warning" in data
    assert b"Different evidence." in data
    assert b"You little monster" not in data
    assert client.post("/media/s01e01/subtitle", data={"subtitle_id": 1}).status_code == 302
    data = client.get("/episode/s01e01").data
    assert b"You little monster" in data
    assert b"Different track warning" not in data
    assert client.post("/media/s01e01/subtitle", data={"subtitle_id": 999}).status_code == 400


def test_quality_scan_preserves_stored_identity_and_language(tmp_path, monkeypatch):
    root = tmp_path / "subs"
    path = write_subtitle(root / "movie.srt")
    doc = analyzer.load_subtitle(path, root, media.ImportOptions(media_type="movie", title="Original title", language="auto"))
    monkeypatch.setattr(analyzer, "assess_subtitle_quality", lambda document: ("good", [], {"language": document.language}))
    with analyzer.connect_database(tmp_path / "ratings.sqlite3") as connection:
        analyzer.upsert_subtitle(connection, doc)
        connection.commit()
        assert scan_subtitle_quality(connection, root)["good"] == 1
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
        assert connection.execute("SELECT language FROM subtitle_files").fetchone()[0] == "auto"


def test_explicit_show_metadata_cannot_overwrite_simpsons(tmp_path):
    database = tmp_path / "ratings.sqlite3"
    seed_database(database)
    with analyzer.connect_database(database) as connection:
        analyzer.update_tvmaze_metadata(connection, [{"season": 1, "number": 1, "name": "Other pilot"}], 999, "Other show")
        assert connection.execute("SELECT title FROM episodes WHERE episode_code='s01e01'").fetchone()[0] == "Simpsons Roasting on an Open Fire"
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2


def test_mixed_library_filters_and_notes_are_isolated(tmp_path, monkeypatch):
    root = tmp_path / "library"
    for name in ("First.Show.S01E01.srt", "Second.Show.S01E01.srt", "film.srt"):
        write_subtitle(root / name)
    manifest = tmp_path / "media.json"
    manifest.write_text(json.dumps({"files": {"film.srt": {"type": "movie", "title": "A film", "year": 2020}}}), encoding="utf-8")
    monkeypatch.setattr(analyzer, "preflight_server", lambda *args: ["test"])
    monkeypatch.setattr(analyzer, "assess_subtitle_quality", lambda document: ("good", [], {}))
    monkeypatch.setattr(analyzer, "request_analysis", lambda *args: (valid_result(), json.dumps(valid_result())))
    args = analyzer.parse_args(["--subtitle-dir", str(root), "--database", str(tmp_path / "ratings.sqlite3"),
                                "--manifest", str(manifest), "--no-fetch-metadata", "--no-fetch-external-sources"])
    assert analyzer.run(args) == 0
    client = create_app(args.database, testing=True).test_client()
    rows = client.get("/api/media").get_json()["media"]
    assert len(rows) == 3
    first = next(row for row in rows if row["series_title"] == "First Show")
    second = next(row for row in rows if row["series_title"] == "Second Show")
    assert first["media_id"] != second["media_id"]
    movie_page = client.get("/?type=movie").data
    assert b"A film" in movie_page
    assert first["media_id"].encode() not in movie_page
    show_page = client.get("/", query_string={"series": first["series_key"]}).data
    assert first["media_id"].encode() in show_page
    assert second["media_id"].encode() not in show_page
    assert b"A film" not in show_page
    client.post(f"/media/{first['media_id']}/state", data={"favorite": "1", "notes": "Only the first show"})
    assert b"Only the first show" in client.get(f"/media/{first['media_id']}").data
    assert b"Only the first show" not in client.get(f"/media/{second['media_id']}").data
    comparison = client.get("/compare", query_string=[("episode", row["media_id"]) for row in rows])
    assert comparison.status_code == 200
    assert all(label in comparison.data for label in (b"First Show", b"Second Show", b"A film"))


def test_single_file_import_and_metadata_corrections_preserve_identity(tmp_path):
    root = tmp_path / "subtitles"
    first = write_subtitle(root / "first.txt")
    write_subtitle(root / "other.txt")
    args = analyzer.parse_args(["--subtitle-file", str(first), "--title", "Manual title"])
    assert args.subtitle_dir == root
    with analyzer.connect_database(tmp_path / "ratings.sqlite3") as connection:
        doc = analyzer.load_subtitle(first, root)
        subtitle_id = analyzer.upsert_subtitle(connection, doc)
        options = media.ImportOptions(title="Corrected title", language="auto", existing=media.existing_bindings(connection))
        corrected = analyzer.load_subtitle(first, root, options)
        assert corrected.episode_code == doc.episode_code
        assert analyzer.upsert_subtitle(connection, corrected) == subtitle_id
        assert connection.execute("SELECT title FROM episodes").fetchone()[0] == "Corrected title"


def test_explicit_same_simpsons_identity_keeps_legacy_subtitle_id(tmp_path):
    database = tmp_path / "ratings.sqlite3"
    seed_database(database)
    root = tmp_path / "subtitle_cache"
    path = write_subtitle(root / "s01e01.srt")
    with analyzer.connect_database(database) as connection:
        options = media.ImportOptions(show_title="The Simpsons", existing=media.existing_bindings(connection), legacy_root=root)
        document = analyzer.load_subtitle(path, root, options)
        assert document.relative_path == "s01e01.srt"
        assert analyzer.upsert_subtitle(connection, document) == 1
