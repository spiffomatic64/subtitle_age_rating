from __future__ import annotations

import json
from pathlib import Path

import subtitle_age_rating as analyzer


def valid_result() -> dict:
    categories = {
        key: {"score": 0, "explanation": f"No {label.lower()} detected.", "evidence": []}
        for key, label, _polarity in analyzer.CATEGORIES
    }
    age_bands = {
        key: {"suitability": "appropriate", "explanation": f"Suitable for {label}."}
        for key, label, _minimum, _maximum in analyzer.AGE_BANDS
    }
    return {
        "recommended_age": 8,
        "confidence": 0.9,
        "summary": "A family story.",
        "parent_guidance": "Discuss the mild conflict.",
        "limitations": ["Visual content is not available."],
        "categories": categories,
        "content_advisories": [],
        "age_band_guidance": age_bands,
    }


def test_blank_model_evidence_is_normalized() -> None:
    result = valid_result()
    result["categories"]["language"]["evidence"] = [{"excerpt": "   "}]
    result["content_advisories"] = [
        {
            "type": "mild_profanity",
            "severity": 1,
            "frequency": "isolated",
            "context": "challenged",
            "explanation": "One mild term occurs.",
            "evidence": [{"excerpt": ""}],
        }
    ]
    parsed = analyzer.parse_model_json(json.dumps(result))
    assert parsed["categories"]["language"]["evidence"] == []
    assert parsed["content_advisories"][0]["evidence"] == []
    assert parsed["_fuzzy_json_repaired"] is True
    assert "blank evidence" in parsed["_json_parse_method"]


def test_python_style_model_object_is_repaired() -> None:
    result = valid_result()
    result["limitations"] = []
    parsed = analyzer.parse_model_json(repr(result))
    assert parsed["recommended_age"] == 8
    assert parsed["_fuzzy_json_repaired"] is True
    assert "Python-style object" in parsed["_json_parse_method"]


def test_strict_json_is_not_marked_as_fuzzy() -> None:
    parsed = analyzer.parse_model_json(json.dumps(valid_result()))
    assert parsed["_fuzzy_json_repaired"] is False
    assert parsed["_json_parse_method"] == "strict JSON"


def test_five_point_confidence_is_normalized_and_flagged() -> None:
    result = valid_result()
    result["confidence"] = 4
    parsed = analyzer.parse_model_json(json.dumps(result))
    assert parsed["confidence"] == 0.8
    assert parsed["_fuzzy_json_repaired"] is True
    assert "five-point scale" in parsed["_json_parse_method"]


def test_reanalyze_fuzzy_cli_flag() -> None:
    args = analyzer.parse_args(["--reanalyze-fuzzy", "--limit", "2"])
    assert args.reanalyze_fuzzy is True
    assert args.limit == 2
    needed = analyzer.parse_args(["--reanalyze-needed"])
    assert needed.reanalyze_needed is True


def test_single_arrow_srt_is_still_parsed() -> None:
    raw = "1\n00:00:01,000 -> 00:00:03,000\nHello there.\n\n2\n00:00:04,000 --> 00:00:06,000\nGeneral Kenobi.\n"
    transcript, cue_count = analyzer.parse_transcript(raw, ".srt")
    assert cue_count == 2
    assert transcript == "Hello there.\nGeneral Kenobi."


def test_quality_check_flags_obvious_corruption() -> None:
    document = analyzer.SubtitleDocument(
        path=Path("s01e01.srt"), relative_path="s01e01.srt", episode_code="s01e01",
        season=1, episode=1, sha256="abc", byte_size=1, modified_ns=1,
        encoding="utf-8", transcript=("Broken � Ã â€™ text. " * 20), cue_count=10,
    )
    status, issues, metrics = analyzer.assess_subtitle_quality(document)
    assert status == "corrupt"
    assert issues
    assert metrics["replacement_characters"] == 20


def test_evidence_matching_ignores_punctuation_but_not_paraphrases() -> None:
    transcript = "Don't have a cow, man!\nThis is exact dialogue."
    assert analyzer.evidence_is_verified("Don't have a cow, man", transcript)
    assert not analyzer.evidence_is_verified("Bart tells someone to relax", transcript)


def test_fuzzy_provenance_is_saved_and_selected(tmp_path: Path) -> None:
    database = tmp_path / "ratings.sqlite3"
    document = analyzer.SubtitleDocument(
        path=tmp_path / "s01e01.srt", relative_path="s01e01.srt", episode_code="s01e01",
        season=1, episode=1, sha256="abc", byte_size=10, modified_ns=1,
        encoding="utf-8", transcript="Exact dialogue.", cue_count=1,
    )
    with analyzer.connect_database(database) as connection:
        analyzer.register_prompt(connection)
        subtitle_id = analyzer.upsert_subtitle(connection, document)
        assert analyzer.current_analysis_requirement(
            connection, document.relative_path, document.sha256
        ) == "missing"
        run_id = connection.execute(
            """INSERT INTO analysis_runs(
                   started_at, status, model, server_url, prompt_version, settings_json, requested_count
               ) VALUES ('2026-01-01T00:00:00Z', 'running', 'test', 'http://localhost', ?, '{}', 1)""",
            (analyzer.PROMPT_VERSION,),
        ).lastrowid
        analysis_id = analyzer.begin_analysis(connection, run_id, subtitle_id, document, "test")
        result = analyzer.parse_model_json(repr(valid_result()))
        analyzer.save_success(connection, analysis_id, result, repr(result), document.transcript, 1.0)
        stored = connection.execute(
            "SELECT fuzzy_json_repaired, json_parse_method FROM analyses WHERE id=?",
            (analysis_id,),
        ).fetchone()
        assert stored["fuzzy_json_repaired"] == 1
        assert "Python-style object" in stored["json_parse_method"]
        assert analyzer.latest_fuzzy_repaired_paths(connection) == {"s01e01.srt"}
        assert analyzer.current_analysis_requirement(
            connection, document.relative_path, document.sha256
        ) == "fuzzy"
        assert analyzer.already_analyzed(connection, subtitle_id, document.sha256, "test") is True
        failed_id = analyzer.begin_analysis(connection, run_id, subtitle_id, document, "test")
        analyzer.save_failure(connection, failed_id, "Later forced attempt failed", "{}", 1.0)
        assert analyzer.already_analyzed(connection, subtitle_id, document.sha256, "test") is False
        assert analyzer.current_analysis_requirement(
            connection, document.relative_path, document.sha256
        ) == "fuzzy"

        strict_id = analyzer.begin_analysis(connection, run_id, subtitle_id, document, "preferred")
        strict_result = analyzer.parse_model_json(json.dumps(valid_result()))
        analyzer.save_success(
            connection, strict_id, strict_result, json.dumps(strict_result), document.transcript, 1.0
        )
        assert analyzer.current_analysis_requirement(
            connection, document.relative_path, document.sha256
        ) is None
