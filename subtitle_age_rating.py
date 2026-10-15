#!/usr/bin/env python3
"""Analyze local subtitle files for age appropriateness with llama.cpp.

The script uses llama.cpp's OpenAI-compatible HTTP API and stores versioned,
resumable results in SQLite. Ratings are transcript-based estimates inspired by
Common Sense Media's published methodology; they are not official Common Sense
Media ratings.

Typical use (with llama-server running; see README.md for setup):

    python subtitle_age_rating.py
    python subtitle_age_rating.py --episode s01e01
    python subtitle_age_rating.py --limit 5 --model subtitle-rating-fast
    python subtitle_age_rating.py --force --episode s01e01
    python subtitle_age_rating.py --fresh

Install the enrichment and quality-check dependencies from requirements.txt.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import queue
import re
import ssl
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import subtitle_age_rating_media as media


DEFAULT_SUBTITLE_DIR = "subtitle_cache"
DEFAULT_DATABASE = "subtitle_age_ratings.sqlite3"
DEFAULT_SERVER_URL = "http://127.0.0.1:11435"
DEFAULT_MODEL = "qwen3.6-27b"
TVMAZE_EPISODES_URL = "https://api.tvmaze.com/shows/{show_id}/episodes?specials=1"
RUBRIC_URL = "https://www.commonsensemedia.org/about-us/our-mission/about-our-ratings"
PROMPT_VERSION = "csm-subtitle-v6-media"
COMPATIBLE_PROMPT_VERSIONS = (PROMPT_VERSION, "csm-subtitle-v5")
SCHEMA_VERSION = 4
USER_AGENT = "SubtitleAgeRating/1.0"
ALLOWED_SUFFIXES = {".srt", ".sub", ".vtt", ".ass", ".ssa", ".txt"}
EPISODE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])s(?P<season>\d{1,3})e(?P<episode>\d{1,3})(?:[^a-z0-9]|$)")


CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("positive_messages", "Positive Messages", "positive"),
    ("positive_role_models", "Positive Role Models", "positive"),
    ("diverse_representations", "Diverse Representations", "positive"),
    ("violence_scariness", "Violence & Scariness", "concern"),
    ("sex_romance_nudity", "Sex, Romance & Nudity", "concern"),
    ("language", "Language", "concern"),
    ("products_purchases", "Products & Purchases", "concern"),
    ("drinking_drugs_smoking", "Drinking, Drugs & Smoking", "concern"),
    ("educational_value", "Educational Value", "positive"),
)
CATEGORY_KEYS = tuple(item[0] for item in CATEGORIES)
AGE_BANDS: tuple[tuple[str, str, int, int], ...] = (
    ("ages_2_5", "Ages 2–5", 2, 5),
    ("ages_6_8", "Ages 6–8", 6, 8),
    ("ages_9_11", "Ages 9–11", 9, 11),
    ("ages_12_14", "Ages 12–14", 12, 14),
    ("ages_15_17", "Ages 15–17", 15, 17),
)
AGE_BAND_KEYS = tuple(item[0] for item in AGE_BANDS)
ADVISORY_TYPES: tuple[tuple[str, str, str], ...] = (
    ("cartoon_fantasy_violence", "Cartoon or Fantasy Violence", "violence_scariness"),
    ("realistic_violence", "Realistic Violence", "violence_scariness"),
    ("gore_injury", "Gore or Injury", "violence_scariness"),
    ("peril_frightening_scenes", "Peril or Frightening Scenes", "violence_scariness"),
    ("cruelty_bullying_humiliation", "Cruelty, Bullying, or Humiliation", "violence_scariness"),
    ("romance_flirting", "Romance or Flirting", "sex_romance_nudity"),
    ("sexual_innuendo", "Sexual Innuendo", "sex_romance_nudity"),
    ("sexual_behavior", "Sexual Behavior", "sex_romance_nudity"),
    ("nudity_references", "Nudity References", "sex_romance_nudity"),
    ("consent_coercion_unsafe_behavior", "Consent, Coercion, or Unsafe Behavior", "sex_romance_nudity"),
    ("infidelity_mature_relationships", "Infidelity or Mature Relationships", "sex_romance_nudity"),
    ("mild_profanity", "Mild Profanity", "language"),
    ("strong_profanity", "Strong Profanity", "language"),
    ("insults_name_calling", "Insults or Name-Calling", "language"),
    ("hate_speech_slurs", "Hate Speech or Slurs", "language"),
    ("alcohol", "Alcohol", "drinking_drugs_smoking"),
    ("tobacco", "Tobacco", "drinking_drugs_smoking"),
    ("drugs", "Drugs", "drinking_drugs_smoking"),
    ("underage_substance_use", "Underage Substance Use", "drinking_drugs_smoking"),
    ("substance_glamorization", "Substance Glamorization", "drinking_drugs_smoking"),
    ("crime_scams_dishonesty", "Crime, Scams, or Dishonesty", "positive_messages"),
    ("dangerous_imitable_behavior", "Dangerous Imitable Behavior", "positive_messages"),
    ("consumerism_advertising", "Consumerism or Advertising", "products_purchases"),
    ("sexism_misogyny", "Sexism or Misogyny", "diverse_representations"),
    ("racism_xenophobia", "Racism or Xenophobia", "diverse_representations"),
    ("lgbtq_prejudice", "LGBTQ+ Prejudice", "diverse_representations"),
    ("religious_prejudice", "Religious Prejudice", "diverse_representations"),
    ("disability_mental_health_stereotypes", "Disability or Mental-Health Stereotypes", "diverse_representations"),
    ("body_shaming", "Body-Shaming", "diverse_representations"),
    ("age_class_stereotypes", "Age or Socioeconomic Stereotypes", "diverse_representations"),
)
ADVISORY_KEYS = tuple(item[0] for item in ADVISORY_TYPES)
ADVISORY_INFO = {key: {"label": label, "category": category} for key, label, category in ADVISORY_TYPES}
ADVISORY_FREQUENCIES = ("isolated", "occasional", "recurring", "pervasive")
ADVISORY_CONTEXTS = ("endorsed", "unchallenged", "challenged", "satirical", "consequential")
CATEGORY_RUBRIC = {
    "positive_messages": (
        "Assess the overall takeaway, constructive lessons or values, responsible behavior, "
        "and problematic viewpoints or stereotypes."
    ),
    "positive_role_models": (
        "Assess whether characters are realistic and relatable, whether children might sensibly "
        "emulate their behavior, and whether adults act responsibly."
    ),
    "diverse_representations": (
        "Assess represented marginalized identities, whether portrayals are stereotyped or "
        "well-rounded, character agency, and whether different identities are valued equitably."
    ),
    "violence_scariness": (
        "Assess intensity and realism, fantasy or cartoon context, injury or gore described in the "
        "transcript, consequences, peril, upsetting separation, threats, and cruel behavior."
    ),
    "sex_romance_nudity": (
        "Assess innuendo, sexual banter, flirting, sexual activity or nudity explicitly indicated "
        "by words or sound cues, safety, and consequences."
    ),
    "language": (
        "Identify the kinds and frequency of profanity, hate words, slurs, insults, and name-calling."
    ),
    "products_purchases": (
        "Assess spoken brand references, advertising, merchandising, consumer pressure, and apparent "
        "intent to sell to children, while acknowledging unseen visual placement."
    ),
    "drinking_drugs_smoking": (
        "Assess alcohol, tobacco, and drug use; frequency; glamorization; consequences; and whether "
        "characters are portrayed as underage or of legal age."
    ),
    "educational_value": (
        "Identify what children could learn and whether the educational value appears intentional "
        "or incidental."
    ),
}


SYSTEM_PROMPT = """You are a careful parent-facing media reviewer analyzing a movie, television episode, or standalone transcript and supplied source summaries.

Apply the methodology described by Common Sense Media's public ratings rubric, but do not claim that this is an official Common Sense Media review. Recommend the youngest appropriate age as an integer from 2 through 18. Rate every requested content category from 0 through 5, where 0 means absent/not evident and 5 means a great deal. For positive categories, a higher score means more positive content; for concern categories, a higher score means more potentially concerning content.

Base conclusions only on the supplied episode metadata, attributed source summaries, and subtitle transcript, even if you recognize the series or characters. Do not import outside knowledge, official ratings, or unsupported visual details. Treat source summaries as secondary context and the transcript as the only source for verbatim evidence. Subtitles cannot reliably reveal purely visual material. Explicitly mention uncertainty for visual violence, scariness, nudity, representation, product placement, or any other category that cannot be established from words, sound cues, or an attributed summary. Do not treat lack of transcript evidence as proof that visual content is absent.

Consider context, frequency, intensity, realism, consequences, glamorization, character ages, stereotypes, developmental comprehension, and the episode's overall takeaway. Satirical, quoted, condemned, or educational content still deserves mention, but context can affect severity. The recommended age must be consistent with the most maturity-demanding material and the maturity needed to understand sophisticated satire or adult behavior. Do not assign an early-childhood age merely because drinking, gambling, sexual wordplay, profanity, cruelty, or risky behavior is comedic or non-graphic.

Create content_advisories only for content that is present or meaningfully indicated; do not emit zero-severity placeholders. Distinguish frequency, severity, and whether problematic behavior is endorsed, left unchallenged, challenged, satirical, or shown with consequences. Pay special attention to sexism or misogyny, racism or xenophobia, LGBTQ+ prejudice, religious prejudice, disability or mental-health stereotypes, body-shaming, and age or class stereotypes. Satire does not erase the underlying content, although it can change context and severity.

Give a brief developmental suitability assessment for every requested age band. Keep the response concise: summary and parent_guidance must each be no more than two sentences; category and advisory explanations must each be no more than two sentences; age-band explanations must each be one sentence; and limitations must contain at most three short items.

Each evidence excerpt must be copied verbatim from one contiguous span of the supplied transcript text. Do not fix spelling, expand contractions, paraphrase, or combine distant lines. Prefer one excerpt and use no more than two excerpts per category and one per advisory. Keep each excerpt under 200 characters by selecting the shortest exact phrase that supports the finding. If no exact transcript text supports a category or advisory, return an empty evidence array and explain the uncertainty. Do not invent scenes, identities, visuals, or quotations.

Return only JSON matching the supplied schema."""


@dataclass(frozen=True)
class SubtitleDocument:
    path: Path
    relative_path: str
    episode_code: str
    season: int
    episode: int
    sha256: str
    byte_size: int
    modified_ns: int
    encoding: str
    transcript: str
    cue_count: int
    media_info: dict[str, Any] = field(default_factory=dict)
    language: str = "en"


class UserFacingError(RuntimeError):
    """An error that should be displayed without a traceback."""


class AnalysisResponseError(UserFacingError):
    """An inference error that preserves the model's last raw response."""

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", cleaned)


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UserFacingError(f"Invalid --server-url: {value!r}")
    return value


def _request_json_blocking(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    verify_tls: bool = True,
) -> Any:
    data = None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    ssl_context = None if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise UserFacingError(f"HTTP {exc.code} from {url}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UserFacingError(f"Could not reach {url}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw[:500].decode("utf-8", errors="replace")
        raise UserFacingError(f"Invalid JSON from {url}: {preview}") from exc


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    verify_tls: bool = True,
) -> Any:
    """Run blocking urllib I/O off the main thread so Ctrl+C stays responsive.

    Windows can defer KeyboardInterrupt while the main thread is blocked inside
    a socket read. The worker is deliberately a daemon: if the user interrupts,
    database cleanup can finish on the main thread and process exit will close
    the abandoned request socket without waiting for the inference timeout.
    """
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result = _request_json_blocking(
                url, method=method, body=body, timeout=timeout, verify_tls=verify_tls
            )
        except Exception as exc:
            result_queue.put((False, exc))
        else:
            result_queue.put((True, result))

    threading.Thread(target=worker, name="subtitle-age-rating-http", daemon=True).start()
    while True:
        try:
            succeeded, value = result_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if succeeded:
            return value
        if isinstance(value, Exception):
            raise value
        raise UserFacingError(f"Unexpected HTTP worker result: {value!r}")


def preflight_server(server_url: str, model: str, timeout: float) -> list[str]:
    """Fail before scanning or fetching metadata if llama.cpp/model is unavailable."""
    models_url = f"{server_url}/v1/models"
    try:
        payload = request_json(models_url, timeout=timeout)
    except UserFacingError as exc:
        raise UserFacingError(
            f"llama.cpp is not ready at {server_url}. Start llama-server as described in README.md "
            f"and try again. Details: {exc}"
        ) from exc

    entries = payload.get("data", []) if isinstance(payload, dict) else []
    ids = sorted(
        str(item.get("id"))
        for item in entries
        if isinstance(item, dict) and item.get("id") is not None
    )
    if not ids:
        raise UserFacingError(f"llama.cpp returned no models from {models_url}")
    if model not in ids:
        raise UserFacingError(
            f"Model {model!r} is not advertised by llama.cpp. Available models: {', '.join(ids)}"
        )
    return ids


def decode_subtitle(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def clean_cue_text(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\\N", " ").replace("\\n", " ")
    value = re.sub(r"(?i)<br\s*/?>", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\{\\[^}]+}", " ", value)
    value = re.sub(r"(?i)\[(?:/?(?:colf|style|size|font))[^]]*]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_timestamp(value: str) -> str:
    match = re.match(r"\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", value)
    if not match:
        return value.strip()
    hours, minutes, seconds, fraction = match.groups()
    millis = int(fraction.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{millis:03d}"


def render_cues(cues: Iterable[tuple[str, str]]) -> tuple[str, int]:
    rendered: list[str] = []
    previous = ""
    for _timestamp, text in cues:
        text = clean_cue_text(text)
        if not text:
            continue
        # Remove only exact adjacent duplicates, common in overlapping subtitle tracks.
        if text == previous:
            continue
        previous = text
        # Preserve subtitle boundaries and ordering, but do not spend context
        # tokens on timestamps or generated identifiers. Evidence is grounded
        # afterward by matching the model's exact excerpt against this text.
        rendered.append(text)
    return "\n".join(rendered), len(rendered)


def parse_srt_or_vtt(raw: str) -> tuple[str, int]:
    raw = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    time_re = re.compile(
        # Some cached files use the malformed but unambiguous single-hyphen
        # arrow "->" instead of the SRT-standard "-->".
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-{1,2}>\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    )
    cues: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith("WEBVTT") and len(lines) == 1:
            continue
        time_index = next((index for index, line in enumerate(lines) if time_re.search(line)), None)
        if time_index is None:
            continue
        match = time_re.search(lines[time_index])
        assert match is not None
        text = " ".join(lines[time_index + 1 :])
        cues.append((normalize_timestamp(match.group("start")), text))
    if cues:
        return render_cues(cues)
    return parse_plain_text(raw)


def parse_subviewer(raw: str) -> tuple[str, int]:
    raw = raw.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    time_re = re.compile(
        r"^(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*,\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})$"
    )
    lines = raw.splitlines()
    cues: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        match = time_re.match(lines[index].strip())
        if not match:
            index += 1
            continue
        timestamp = normalize_timestamp(match.group("start"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip() and not time_re.match(lines[index].strip()):
            text_lines.append(lines[index].strip())
            index += 1
        cues.append((timestamp, " ".join(text_lines).replace("[br]", " ")))
    if cues:
        return render_cues(cues)
    return parse_microdvd(raw)


def parse_microdvd(raw: str) -> tuple[str, int]:
    pattern = re.compile(r"^\{(?P<start>\d+)}\{\d+}(?P<text>.*)$")
    cues: list[tuple[str, str]] = []
    for line in raw.splitlines():
        match = pattern.match(line.strip())
        if match:
            cues.append((f"frame:{match.group('start')}", match.group("text").replace("|", " ")))
    if cues:
        return render_cues(cues)
    return parse_plain_text(raw)


def parse_ass(raw: str) -> tuple[str, int]:
    cues: list[tuple[str, str]] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if not line.lower().startswith("dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            cues.append((normalize_timestamp(parts[1]), parts[9]))
    if cues:
        return render_cues(cues)
    return parse_plain_text(raw)


def parse_plain_text(raw: str) -> tuple[str, int]:
    cues = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"\d+", line) or "-->" in line:
            continue
        if line.startswith("[") and line.endswith("]") and len(line) < 40:
            continue
        cues.append(("", line))
    return render_cues(cues)


def parse_transcript(raw: str, suffix: str) -> tuple[str, int]:
    suffix = suffix.lower()
    if suffix in {".srt", ".vtt"}:
        return parse_srt_or_vtt(raw)
    if suffix == ".sub":
        return parse_subviewer(raw)
    if suffix in {".ass", ".ssa"}:
        return parse_ass(raw)
    return parse_plain_text(raw)


def parse_episode_code(path: Path) -> tuple[str, int, int]:
    match = EPISODE_RE.search(path.stem)
    if not match:
        raise UserFacingError(f"Cannot determine season/episode from subtitle filename: {path.name}")
    season = int(match.group("season"))
    episode = int(match.group("episode"))
    return f"s{season:02d}e{episode:02d}", season, episode


def load_subtitle(path: Path, root: Path, options: media.ImportOptions | None = None) -> SubtitleDocument:
    data = path.read_bytes()
    raw, encoding = decode_subtitle(data)
    transcript, cue_count = parse_transcript(raw, path.suffix)
    if not transcript:
        raise UserFacingError(f"No usable transcript cues found in {path}")
    try:
        info = media.identify(path, root, options)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    episode_code, season, episode = info["episode_code"], info["season"], info["episode"]
    stat = path.stat()
    return SubtitleDocument(
        path=path,
        relative_path=info["storage_path"],
        episode_code=episode_code,
        season=season,
        episode=episode,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        modified_ns=stat.st_mtime_ns,
        encoding=encoding,
        transcript=transcript,
        cue_count=cue_count,
        media_info=info,
        language=info.get("language", "en"),
    )


def assess_subtitle_quality(document: SubtitleDocument) -> tuple[str, list[str], dict[str, Any]]:
    """Detect text damage or a confidently non-English subtitle before inference."""
    text = document.transcript
    replacement_chars = text.count("\ufffd")
    mojibake_hits = sum(text.count(marker) for marker in ("Â", "Ã", "â€", "â€™", "â€œ", "â€"))
    issues: list[str] = []
    status = "good"
    if replacement_chars:
        issues.append(f"Contains {replacement_chars} Unicode replacement character(s)")
        status = "corrupt" if replacement_chars > 2 else "warning"
    if mojibake_hits:
        issues.append(f"Contains {mojibake_hits} likely mojibake marker(s)")
        status = "corrupt" if mojibake_hits >= 10 else "warning"

    detected_language = None
    language_confidence = None
    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        candidates = detect_langs(text[:20000])
        if candidates:
            detected_language = candidates[0].lang
            language_confidence = float(candidates[0].prob)
            if document.language != "auto" and detected_language != document.language and language_confidence >= 0.98:
                issues.append(
                    f"Detected language {detected_language!r} with {language_confidence:.1%} confidence"
                )
                status = "corrupt"
    except (ImportError, ValueError):
        pass

    metrics = {
        "replacement_characters": replacement_chars,
        "mojibake_hits": mojibake_hits,
        "detected_language": detected_language,
        "language_confidence": language_confidence,
        "cue_count": document.cue_count,
        "transcript_chars": len(text),
    }
    return status, issues, metrics


def save_subtitle_quality(
    connection: sqlite3.Connection,
    subtitle_file_id: int,
    status: str,
    issues: Sequence[str],
    metrics: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO subtitle_quality(subtitle_file_id, status, issues_json, metrics_json, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subtitle_file_id) DO UPDATE SET
            status = excluded.status,
            issues_json = excluded.issues_json,
            metrics_json = excluded.metrics_json,
            checked_at = excluded.checked_at
        """,
        (
            subtitle_file_id,
            status,
            json.dumps(list(issues), ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            utc_now(),
        ),
    )


def discover_subtitles(root: Path, episode_filters: set[str], options: media.ImportOptions | None = None) -> list[Path]:
    if not root.is_dir():
        raise UserFacingError(f"Subtitle directory does not exist: {root}")
    selected: list[tuple[int, int, str, Path]] = []
    invalid: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        try:
            info = media.identify(path, root, options)
            season, episode = info["season"], info["episode"]
        except ValueError:
            invalid.append(str(path))
            continue
        if not episode_filters or f"s{season:02d}e{episode:02d}" in episode_filters:
            selected.append((season, episode, path.name.lower(), path))
    if invalid:
        raise UserFacingError(f"Cannot identify {len(invalid)} subtitle file(s); check import options or manifest: {invalid[0]}")
    selected.sort(key=lambda item: (item[0], item[1], item[2]))
    if episode_filters:
        found = {f"s{season:02d}e{episode:02d}" for season, episode, _, _ in selected}
        missing = sorted(episode_filters - found)
        if missing:
            raise UserFacingError(f"No subtitle file found for: {', '.join(missing)}")
    if not selected:
        raise UserFacingError(f"No supported subtitle files found in {root}")
    return [item[3] for item in selected]


def normalize_episode_filter(value: str) -> str:
    match = re.fullmatch(r"(?i)s?(\d{1,3})e(\d{1,3})", value.strip())
    if not match:
        alt = re.fullmatch(r"(\d{1,3})x(\d{1,3})", value.strip())
        if alt:
            match = alt
    if not match:
        raise argparse.ArgumentTypeError(f"invalid episode {value!r}; expected s01e01 or 1x01")
    return f"s{int(match.group(1)):02d}e{int(match.group(2)):02d}"


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    # SQLite's backup API includes committed WAL data and is safe for a live DB.
    # Back up before changing the first legacy schema; never move its files.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(episodes)")}
    if columns and "media_type" not in columns:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = path.with_name(f"{path.name}.backup-media-v4-{timestamp}")
        with sqlite3.connect(backup_path) as backup:
            connection.backup(backup)
        print(f"Backed up legacy database to {backup_path}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        create_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def archive_database(path: Path) -> Path | None:
    """Move an existing database aside so a fresh run remains recoverable."""
    if not path.exists():
        return None
    if not path.is_file():
        raise UserFacingError(f"Database path is not a file: {path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    path.replace(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.replace(Path(f"{backup}{suffix}"))
    return backup


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            episode_code TEXT PRIMARY KEY,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            tvmaze_id INTEGER,
            title TEXT,
            airdate TEXT,
            airtime TEXT,
            runtime_minutes INTEGER,
            episode_type TEXT,
            tvmaze_rating REAL,
            summary TEXT,
            tvmaze_url TEXT,
            image_medium TEXT,
            image_original TEXT,
            metadata_json TEXT,
            metadata_updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subtitle_files (
            id INTEGER PRIMARY KEY,
            episode_code TEXT NOT NULL REFERENCES episodes(episode_code),
            path TEXT NOT NULL UNIQUE,
            format TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            encoding TEXT NOT NULL,
            cue_count INTEGER NOT NULL,
            transcript_chars INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prompts (
            version TEXT PRIMARY KEY,
            rubric_url TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            response_schema_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            model TEXT NOT NULL,
            server_url TEXT NOT NULL,
            prompt_version TEXT NOT NULL REFERENCES prompts(version),
            settings_json TEXT NOT NULL,
            requested_count INTEGER NOT NULL DEFAULT 0,
            succeeded_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
            subtitle_file_id INTEGER NOT NULL REFERENCES subtitle_files(id),
            source_sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL REFERENCES prompts(version),
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds REAL,
            recommended_age INTEGER CHECK (recommended_age BETWEEN 2 AND 18),
            confidence REAL CHECK (confidence BETWEEN 0 AND 1),
            summary TEXT,
            parent_guidance TEXT,
            limitations_json TEXT,
            raw_response TEXT,
            fuzzy_json_repaired INTEGER NOT NULL DEFAULT 0 CHECK (fuzzy_json_repaired IN (0, 1)),
            json_parse_method TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS run_errors (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
            path TEXT NOT NULL,
            episode_code TEXT,
            stage TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS category_ratings (
            analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            label TEXT NOT NULL,
            polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'concern')),
            score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 5),
            explanation TEXT NOT NULL,
            PRIMARY KEY (analysis_id, category)
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY,
            analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            timestamp TEXT,
            transcript_verified INTEGER NOT NULL CHECK (transcript_verified IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS content_advisories (
            id INTEGER PRIMARY KEY,
            analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            advisory_type TEXT NOT NULL,
            label TEXT NOT NULL,
            parent_category TEXT NOT NULL,
            severity INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 5),
            frequency TEXT NOT NULL CHECK (frequency IN ('isolated', 'occasional', 'recurring', 'pervasive')),
            context TEXT NOT NULL CHECK (context IN ('endorsed', 'unchallenged', 'challenged', 'satirical', 'consequential')),
            explanation TEXT NOT NULL,
            UNIQUE (analysis_id, advisory_type)
        );

        CREATE TABLE IF NOT EXISTS advisory_evidence (
            id INTEGER PRIMARY KEY,
            advisory_id INTEGER NOT NULL REFERENCES content_advisories(id) ON DELETE CASCADE,
            excerpt TEXT NOT NULL,
            transcript_verified INTEGER NOT NULL CHECK (transcript_verified IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS age_band_guidance (
            analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            age_band TEXT NOT NULL,
            label TEXT NOT NULL,
            min_age INTEGER NOT NULL,
            max_age INTEGER NOT NULL,
            suitability TEXT NOT NULL CHECK (suitability IN ('appropriate', 'review', 'inappropriate')),
            explanation TEXT NOT NULL,
            PRIMARY KEY (analysis_id, age_band)
        );

        CREATE TABLE IF NOT EXISTS episode_sources (
            id INTEGER PRIMARY KEY,
            episode_code TEXT NOT NULL REFERENCES episodes(episode_code) ON DELETE CASCADE,
            source_name TEXT NOT NULL,
            source_url TEXT,
            page_title TEXT,
            summary TEXT,
            raw_json TEXT,
            status TEXT NOT NULL DEFAULT 'success',
            error TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE (episode_code, source_name)
        );

        CREATE TABLE IF NOT EXISTS subtitle_quality (
            subtitle_file_id INTEGER PRIMARY KEY REFERENCES subtitle_files(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('good', 'warning', 'corrupt')),
            issues_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT NOT NULL DEFAULT 'My child',
            age INTEGER NOT NULL DEFAULT 8 CHECK (age BETWEEN 2 AND 17),
            sensitivities_json TEXT NOT NULL DEFAULT '{}',
            advanced_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episode_user_state (
            episode_code TEXT PRIMARY KEY REFERENCES episodes(episode_code) ON DELETE CASCADE,
            favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
            watched INTEGER NOT NULL DEFAULT 0 CHECK (watched IN (0, 1)),
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artwork_cache (
            episode_code TEXT PRIMARY KEY REFERENCES episodes(episode_code) ON DELETE CASCADE,
            source_url TEXT,
            local_path TEXT,
            content_type TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_subtitle_episode ON subtitle_files(episode_code);
        CREATE INDEX IF NOT EXISTS idx_analysis_lookup
            ON analyses(subtitle_file_id, source_sha256, model, prompt_version, status);
        CREATE INDEX IF NOT EXISTS idx_analysis_age ON analyses(recommended_age);
        CREATE INDEX IF NOT EXISTS idx_category_score ON category_ratings(category, score);
        CREATE INDEX IF NOT EXISTS idx_run_errors_run ON run_errors(run_id, stage);
        CREATE INDEX IF NOT EXISTS idx_advisory_analysis ON content_advisories(analysis_id, advisory_type);
        CREATE INDEX IF NOT EXISTS idx_advisory_severity ON content_advisories(advisory_type, severity);
        CREATE INDEX IF NOT EXISTS idx_source_episode ON episode_sources(episode_code, source_name);
        CREATE INDEX IF NOT EXISTS idx_user_state_flags ON episode_user_state(favorite, watched);

        CREATE VIEW IF NOT EXISTS latest_analyses AS
        SELECT a.*
        FROM analyses AS a
        WHERE a.status = 'success'
          AND a.id = (
              SELECT a2.id
              FROM analyses AS a2
              WHERE a2.subtitle_file_id = a.subtitle_file_id
                AND a2.status = 'success'
              ORDER BY a2.completed_at DESC, a2.id DESC
              LIMIT 1
          );

        CREATE VIEW IF NOT EXISTS latest_episode_ratings AS
        SELECT
            sf.episode_code,
            ep.season,
            ep.episode,
            ep.title,
            ep.airdate,
            sf.path AS subtitle_path,
            la.recommended_age,
            la.confidence,
            la.summary,
            la.parent_guidance,
            la.model,
            la.prompt_version,
            la.completed_at
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN episodes AS ep ON ep.episode_code = sf.episode_code;

        CREATE VIEW IF NOT EXISTS latest_category_ratings AS
        SELECT
            sf.episode_code,
            cr.category,
            cr.label,
            cr.polarity,
            cr.score,
            cr.explanation,
            la.id AS analysis_id
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN category_ratings AS cr ON cr.analysis_id = la.id;

        CREATE VIEW IF NOT EXISTS latest_verified_evidence AS
        SELECT
            sf.episode_code,
            e.category,
            e.timestamp,
            e.excerpt,
            la.id AS analysis_id
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN evidence AS e ON e.analysis_id = la.id
        WHERE e.transcript_verified = 1;

        CREATE VIEW IF NOT EXISTS latest_content_advisories AS
        SELECT
            sf.episode_code,
            ca.id AS advisory_id,
            ca.advisory_type,
            ca.label,
            ca.parent_category,
            ca.severity,
            ca.frequency,
            ca.context,
            ca.explanation,
            la.id AS analysis_id
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN content_advisories AS ca ON ca.analysis_id = la.id;

        CREATE VIEW IF NOT EXISTS latest_age_band_guidance AS
        SELECT
            sf.episode_code,
            ab.age_band,
            ab.label,
            ab.min_age,
            ab.max_age,
            ab.suitability,
            ab.explanation,
            la.id AS analysis_id
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN age_band_guidance AS ab ON ab.analysis_id = la.id;
        """
    )
    # Early v1/v2 model responses sometimes echoed the transcript's square
    # brackets around timestamps. Keep this column uniform for web clients.
    connection.execute(
        "UPDATE evidence SET timestamp = TRIM(timestamp, '[]') "
        "WHERE timestamp LIKE '[%' AND timestamp LIKE '%]'"
    )
    analysis_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(analyses)")
    }
    if "fuzzy_json_repaired" not in analysis_columns:
        connection.execute(
            "ALTER TABLE analyses ADD COLUMN fuzzy_json_repaired INTEGER NOT NULL DEFAULT 0"
        )
    if "json_parse_method" not in analysis_columns:
        connection.execute("ALTER TABLE analyses ADD COLUMN json_parse_method TEXT")
    # Recreate this public view so existing databases expose newly added
    # parse-provenance columns as well as fresh databases.
    connection.execute("DROP VIEW IF EXISTS latest_episode_ratings")
    connection.execute(
        """
        CREATE VIEW latest_episode_ratings AS
        SELECT
            sf.episode_code,
            ep.season,
            ep.episode,
            ep.title,
            ep.airdate,
            sf.path AS subtitle_path,
            la.recommended_age,
            la.confidence,
            la.summary,
            la.parent_guidance,
            la.model,
            la.prompt_version,
            la.fuzzy_json_repaired,
            la.json_parse_method,
            la.completed_at
        FROM latest_analyses AS la
        JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
        JOIN episodes AS ep ON ep.episode_code = sf.episode_code
        """
    )
    media.migrate(connection)
    connection.execute(
        "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


def response_json_schema() -> dict[str, Any]:
    evidence_schema = {
        "type": "object",
        "properties": {
            "excerpt": {"type": "string"},
        },
        "required": ["excerpt"],
        "additionalProperties": False,
    }
    category_schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 5},
            "explanation": {"type": "string"},
            "evidence": {"type": "array", "items": evidence_schema, "maxItems": 2},
        },
        "required": ["score", "explanation", "evidence"],
        "additionalProperties": False,
    }
    advisory_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(ADVISORY_KEYS)},
            "severity": {"type": "integer", "minimum": 1, "maximum": 5},
            "frequency": {"type": "string", "enum": list(ADVISORY_FREQUENCIES)},
            "context": {"type": "string", "enum": list(ADVISORY_CONTEXTS)},
            "explanation": {"type": "string"},
            "evidence": {"type": "array", "items": evidence_schema, "maxItems": 1},
        },
        "required": ["type", "severity", "frequency", "context", "explanation", "evidence"],
        "additionalProperties": False,
    }
    age_band_schema = {
        "type": "object",
        "properties": {
            "suitability": {
                "type": "string",
                "enum": ["appropriate", "review", "inappropriate"],
            },
            "explanation": {"type": "string"},
        },
        "required": ["suitability", "explanation"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "recommended_age": {"type": "integer", "minimum": 2, "maximum": 18},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "summary": {"type": "string"},
            "parent_guidance": {"type": "string"},
            "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "categories": {
                "type": "object",
                "properties": {key: category_schema for key in CATEGORY_KEYS},
                "required": list(CATEGORY_KEYS),
                "additionalProperties": False,
            },
            "content_advisories": {
                "type": "array",
                "items": advisory_schema,
                "maxItems": len(ADVISORY_KEYS),
            },
            "age_band_guidance": {
                "type": "object",
                "properties": {key: age_band_schema for key in AGE_BAND_KEYS},
                "required": list(AGE_BAND_KEYS),
                "additionalProperties": False,
            },
        },
        "required": [
            "recommended_age",
            "confidence",
            "summary",
            "parent_guidance",
            "limitations",
            "categories",
            "content_advisories",
            "age_band_guidance",
        ],
        "additionalProperties": False,
    }


def register_prompt(connection: sqlite3.Connection) -> None:
    schema_text = json.dumps(response_json_schema(), ensure_ascii=False, sort_keys=True)
    existing = connection.execute(
        "SELECT system_prompt, response_schema_json FROM prompts WHERE version = ?", (PROMPT_VERSION,)
    ).fetchone()
    if existing and (existing["system_prompt"] != SYSTEM_PROMPT or existing["response_schema_json"] != schema_text):
        raise UserFacingError(
            f"Prompt version {PROMPT_VERSION!r} already exists with different content. "
            "Increment PROMPT_VERSION before changing the prompt or JSON schema."
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO prompts(version, rubric_url, system_prompt, response_schema_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (PROMPT_VERSION, RUBRIC_URL, SYSTEM_PROMPT, schema_text, utc_now()),
    )
    connection.commit()


def ensure_episode(connection: sqlite3.Connection, document: SubtitleDocument) -> None:
    connection.execute(
        """
        INSERT INTO episodes(episode_code, season, episode)
        VALUES (?, ?, ?)
        ON CONFLICT(episode_code) DO UPDATE SET
            season = excluded.season,
            episode = excluded.episode
        """,
        (document.episode_code, document.season, document.episode),
    )
    if document.media_info:
        media.register_record(connection, document.media_info)


def upsert_subtitle(connection: sqlite3.Connection, document: SubtitleDocument) -> int:
    ensure_episode(connection, document)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO subtitle_files(
            episode_code, path, format, sha256, byte_size, modified_ns, encoding,
            cue_count, transcript_chars, first_seen_at, last_seen_at, language
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            episode_code = excluded.episode_code,
            format = excluded.format,
            sha256 = excluded.sha256,
            byte_size = excluded.byte_size,
            modified_ns = excluded.modified_ns,
            encoding = excluded.encoding,
            cue_count = excluded.cue_count,
            transcript_chars = excluded.transcript_chars,
            language = excluded.language,
            last_seen_at = excluded.last_seen_at
        """,
        (
            document.episode_code,
            document.relative_path,
            document.path.suffix.lower().lstrip("."),
            document.sha256,
            document.byte_size,
            document.modified_ns,
            document.encoding,
            document.cue_count,
            len(document.transcript),
            now,
            now,
            document.language,
        ),
    )
    row = connection.execute("SELECT id FROM subtitle_files WHERE path = ?", (document.relative_path,)).fetchone()
    assert row is not None
    return int(row["id"])


def fetch_tvmaze_metadata(timeout: float, show_id: int = 83) -> list[dict[str, Any]]:
    url = TVMAZE_EPISODES_URL.format(show_id=show_id)
    try:
        payload = request_json(url, timeout=timeout)
    except UserFacingError as exc:
        cause: BaseException | None = exc
        certificate_failure = False
        while cause is not None:
            if isinstance(cause, ssl.SSLCertVerificationError):
                certificate_failure = True
                break
            if isinstance(cause, urllib.error.URLError) and isinstance(
                cause.reason, ssl.SSLCertVerificationError
            ):
                certificate_failure = True
                break
            cause = cause.__cause__
        if not certificate_failure:
            raise
        print(
            "Warning: TVMaze certificate verification failed; retrying this metadata request "
            "without certificate verification.",
            file=sys.stderr,
        )
        payload = request_json(url, timeout=timeout, verify_tls=False)
    if not isinstance(payload, list):
        raise UserFacingError("TVMaze episode response was not a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def update_tvmaze_metadata(connection: sqlite3.Connection, episodes: Sequence[dict[str, Any]],
                           show_id: int = 83, show_title: str | None = None) -> int:
    updated = 0
    now = utc_now()
    for item in episodes:
        season = item.get("season")
        episode = item.get("number")
        if not isinstance(season, int) or not isinstance(episode, int):
            continue
        code = f"s{season:02d}e{episode:02d}"
        series_key = f"tvmaze:{show_id}"
        if show_id != 83:
            code = f"{media.stable_id('tv', series_key)}-{code}"
        image = item.get("image") if isinstance(item.get("image"), dict) else {}
        rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
        connection.execute(
            """
            INSERT INTO episodes(
                episode_code, season, episode, tvmaze_id, title, airdate, airtime,
                runtime_minutes, episode_type, tvmaze_rating, summary, tvmaze_url,
                image_medium, image_original, metadata_json, metadata_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(episode_code) DO UPDATE SET
                tvmaze_id = excluded.tvmaze_id,
                title = excluded.title,
                airdate = excluded.airdate,
                airtime = excluded.airtime,
                runtime_minutes = excluded.runtime_minutes,
                episode_type = excluded.episode_type,
                tvmaze_rating = excluded.tvmaze_rating,
                summary = excluded.summary,
                tvmaze_url = excluded.tvmaze_url,
                image_medium = excluded.image_medium,
                image_original = excluded.image_original,
                metadata_json = excluded.metadata_json,
                metadata_updated_at = excluded.metadata_updated_at
            """,
            (
                code,
                season,
                episode,
                item.get("id"),
                item.get("name"),
                item.get("airdate"),
                item.get("airtime"),
                item.get("runtime"),
                item.get("type"),
                rating.get("average"),
                strip_html(item.get("summary")),
                item.get("url"),
                image.get("medium"),
                image.get("original"),
                json.dumps(item, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        updated += 1
        stored_providers = json.loads(connection.execute(
            "SELECT provider_ids_json FROM episodes WHERE episode_code=?", (code,)).fetchone()[0])
        stored_providers["tvmaze_show_id"] = show_id
        connection.execute(
            "UPDATE episodes SET media_type='episode', series_key=?, series_title=?, provider_ids_json=? "
            "WHERE episode_code=?",
            (series_key, show_title or ("The Simpsons" if show_id == 83 else f"TVMaze show {show_id}"),
             json.dumps(stored_providers), code),
        )
    connection.commit()
    return updated


def get_episode_metadata(connection: sqlite3.Connection, episode_code: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM episodes WHERE episode_code = ?", (episode_code,)).fetchone()
    metadata = dict(row) if row else {"episode_code": episode_code}
    metadata["external_sources"] = [
        dict(source)
        for source in connection.execute(
            """
            SELECT source_name, source_url, page_title, summary
            FROM episode_sources
            WHERE episode_code = ? AND status = 'success' AND summary IS NOT NULL
            ORDER BY source_name
            """,
            (episode_code,),
        )
    ]
    return metadata


def build_user_prompt(document: SubtitleDocument, metadata: dict[str, Any]) -> str:
    details = {
        "media_type": metadata.get("media_type", document.media_info.get("media_type", "episode")),
        "series": metadata.get("series_title"),
        "year": metadata.get("release_year"),
        "subtitle_language": document.language,
        "episode_code": document.episode_code,
        "title": metadata.get("title"),
        "airdate": metadata.get("airdate"),
        "runtime_minutes": metadata.get("runtime_minutes"),
        "episode_summary": metadata.get("summary"),
    }
    source_summaries = [
        {
            "source": item.get("source_name"),
            "url": item.get("source_url"),
            "page_title": item.get("page_title"),
            "summary": item.get("summary"),
        }
        for item in metadata.get("external_sources", [])
        if isinstance(item, dict) and item.get("summary")
    ]
    category_guide = {
        key: {
            "label": label,
            "score_direction": "more positive content" if polarity == "positive" else "more concern content",
            "review_questions": CATEGORY_RUBRIC[key],
        }
        for key, label, polarity in CATEGORIES
    }
    return (
        "Analyze this media using the rubric and constraints in the system message.\n\n"
        f"MEDIA METADATA\n{json.dumps(details, ensure_ascii=False, indent=2)}\n\n"
        f"ATTRIBUTED SOURCE SUMMARIES\n{json.dumps(source_summaries, ensure_ascii=False, indent=2)}\n\n"
        f"CATEGORY KEYS\n{json.dumps(category_guide, ensure_ascii=False, indent=2)}\n\n"
        f"CONTENT ADVISORY TYPES\n{json.dumps(ADVISORY_INFO, ensure_ascii=False, indent=2)}\n\n"
        f"AGE BANDS\n{json.dumps({key: label for key, label, _, _ in AGE_BANDS}, ensure_ascii=False, indent=2)}\n\n"
        "SUBTITLE TRANSCRIPT\n"
        f"{document.transcript}"
    )


def chat_completion_payload(document: SubtitleDocument, metadata: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    schema = response_json_schema()
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(document, metadata)},
        ],
        "temperature": args.temperature,
        "seed": args.seed,
        # Qwen3.x can otherwise spend the entire completion budget in
        # reasoning_content and return an empty content field. This task uses a
        # constrained schema, so direct/non-thinking generation is preferable.
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "subtitle_age_rating",
                "strict": True,
                "schema": schema,
            },
        },
    }
    # llama.cpp treats an omitted max_tokens field as unlimited generation
    # (bounded only by EOS and the available context window).
    if args.max_output_tokens:
        payload["max_tokens"] = args.max_output_tokens
    return payload


def extract_message_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise UserFacingError(f"Unexpected chat completion response: {json.dumps(payload, ensure_ascii=False)[:2000]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise UserFacingError("Chat completion returned empty message content")
    return content.strip()


def extract_json_object(text: str) -> str | None:
    """Extract the first balanced object, ignoring braces inside quoted strings."""
    for start, char in enumerate(text):
        if char != "{":
            continue
        stack: list[str] = []
        quote: str | None = None
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    quote = None
                continue
            if current in {'"', "'"}:
                quote = current
            elif current in "{[":
                stack.append("}" if current == "{" else "]")
            elif current in "}]":
                if not stack or current != stack.pop():
                    break
                if not stack:
                    return text[start : index + 1]
        else:
            # Once a plausible outer object reaches EOF, retain that object
            # even if it ends inside a string. Do not continue scanning and
            # accidentally treat one of its complete nested objects as the
            # model's top-level response.
            if stack:
                return text[start:]
    return None


def repair_json_candidate(text: str) -> str:
    """Repair conservative, common JSON formatting errors outside strings."""
    output: list[str] = []
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {'"', "'"}:
            quote = char
            output.append(char)
            index += 1
            continue

        # Remove a comma immediately before a closing object/array token.
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                index += 1
                continue

        # Quote JavaScript-style bare object keys without touching prose in
        # string values. Opening braces are handled here so the first key is
        # covered as well as keys following commas.
        if char in "{,":
            if char == "{":
                stack.append("}")
            output.append(char)
            index += 1
            whitespace_start = index
            while index < length and text[index].isspace():
                index += 1
            output.append(text[whitespace_start:index])
            key_match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)(\s*:)", text[index:])
            if key_match:
                output.extend(('"', key_match.group(1), '"', key_match.group(2)))
                index += key_match.end()
            continue
        if char == "[":
            stack.append("]")
            output.append(char)
            index += 1
            continue
        if char in "}]":
            if stack and stack[-1] == char:
                stack.pop()
            output.append(char)
            index += 1
            continue

        literal_match = re.match(r"(?:True|False|None)\b", text[index:])
        if literal_match:
            output.append({"True": "true", "False": "false", "None": "null"}[literal_match.group(0)])
            index += literal_match.end()
            continue

        output.append(char)
        index += 1

    if quote is None:
        output.extend(reversed(stack))
    return "".join(output)


def parse_model_json(content: str) -> dict[str, Any]:
    raw_candidate = content.strip()
    candidate = raw_candidate
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    if candidate.startswith("<think>") and "</think>" in candidate:
        candidate = candidate.split("</think>", 1)[1].strip()

    extracted = extract_json_object(candidate)
    candidates: list[tuple[str, str]] = []
    for source, value in (("response", candidate), ("extracted object", extracted)):
        if value and all(existing != value for _name, existing in candidates):
            candidates.append((source, value))
    errors: list[str] = []
    for source, original in candidates:
        repaired = repair_json_candidate(original)
        attempts = (
            ("strict JSON", original, lambda value: json.loads(value)),
            ("JSON with control characters", original, lambda value: json.loads(value, strict=False)),
            ("Python-style object", original, ast.literal_eval),
            ("repaired JSON", repaired, lambda value: json.loads(value, strict=False)),
            ("repaired Python-style object", repaired, ast.literal_eval),
        )
        seen_attempts: set[tuple[str, str]] = set()
        for label, value, loader in attempts:
            attempt_key = (label, value)
            if attempt_key in seen_attempts:
                continue
            seen_attempts.add(attempt_key)
            try:
                result = loader(value)
                if not isinstance(result, dict):
                    raise UserFacingError("Model JSON response must be an object")
                value_normalizations = normalize_common_model_values(result)
                blank_evidence_removed = normalize_empty_evidence(result)
                validate_analysis(result)
                fuzzy_repaired = bool(
                    candidate != raw_candidate
                    or source != "response"
                    or label != "strict JSON"
                    or value_normalizations
                    or blank_evidence_removed
                )
                method_parts = []
                if candidate != raw_candidate:
                    method_parts.append("response wrapper removed")
                if source != "response":
                    method_parts.append(source)
                method_parts.append(label)
                method_parts.extend(value_normalizations)
                if blank_evidence_removed:
                    method_parts.append(f"removed {blank_evidence_removed} blank evidence item(s)")
                result["_fuzzy_json_repaired"] = fuzzy_repaired
                result["_json_parse_method"] = " + ".join(method_parts)
                return result
            except (ValueError, SyntaxError, UserFacingError) as exc:
                errors.append(f"{label}: {exc}")

    detail = errors[-1] if errors else "no JSON object found"
    raise UserFacingError(f"Model returned invalid JSON ({detail}): {content[:1000]}")


def normalize_common_model_values(result: dict[str, Any]) -> list[str]:
    """Normalize unambiguous confidence-scale variants and record what changed."""
    confidence = result.get("confidence")
    if isinstance(confidence, bool):
        return []
    original = confidence
    if isinstance(confidence, str):
        text = confidence.strip()
        percent = text.endswith("%")
        if percent:
            text = text[:-1].strip()
        try:
            confidence = float(text)
        except ValueError:
            return []
        if percent:
            confidence /= 100
    if not isinstance(confidence, (int, float)):
        return []
    if 1 < confidence <= 5:
        result["confidence"] = float(confidence) / 5
        return [f"confidence {original!r} normalized from a five-point scale"]
    if 5 < confidence <= 100:
        result["confidence"] = float(confidence) / 100
        return [f"confidence {original!r} normalized from a percentage scale"]
    if isinstance(original, str) and 0 <= confidence <= 1:
        result["confidence"] = float(confidence)
        return [f"confidence {original!r} normalized from text"]
    return []


def normalize_empty_evidence(result: dict[str, Any]) -> int:
    """Treat blank excerpt placeholders as omitted evidence.

    Some otherwise conforming models emit ``{"excerpt": ""}`` when a
    category has no transcript evidence. The prompt's intended representation
    is an empty array, so discard only those unambiguous blank placeholders;
    all other malformed evidence still fails schema validation.
    """
    removed = 0
    categories = result.get("categories")
    if not isinstance(categories, dict):
        return removed
    for category in categories.values():
        if not isinstance(category, dict):
            continue
        evidence = category.get("evidence")
        if not isinstance(evidence, list):
            continue
        filtered = [
            item
            for item in evidence
            if not (
                isinstance(item, dict)
                and isinstance(item.get("excerpt"), str)
                and not item["excerpt"].strip()
            )
        ]
        removed += len(evidence) - len(filtered)
        category["evidence"] = filtered
    advisories = result.get("content_advisories")
    if isinstance(advisories, list):
        for advisory in advisories:
            if not isinstance(advisory, dict) or not isinstance(advisory.get("evidence"), list):
                continue
            filtered = [
                item
                for item in advisory["evidence"]
                if not (
                    isinstance(item, dict)
                    and isinstance(item.get("excerpt"), str)
                    and not item["excerpt"].strip()
                )
            ]
            removed += len(advisory["evidence"]) - len(filtered)
            advisory["evidence"] = filtered
    return removed


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserFacingError(f"Model field {field!r} must be a non-empty string")
    return value.strip()


def validate_analysis(result: dict[str, Any]) -> None:
    age = result.get("recommended_age")
    confidence = result.get("confidence")
    if isinstance(age, bool) or not isinstance(age, int) or not 2 <= age <= 18:
        raise UserFacingError("Model recommended_age must be an integer from 2 through 18")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise UserFacingError("Model confidence must be a number from 0 through 1")
    require_string(result.get("summary"), "summary")
    require_string(result.get("parent_guidance"), "parent_guidance")
    limitations = result.get("limitations")
    if (
        not isinstance(limitations, list)
        or len(limitations) > 3
        or not all(isinstance(item, str) for item in limitations)
    ):
        raise UserFacingError("Model limitations must be an array of at most 3 strings")
    categories = result.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(CATEGORY_KEYS):
        raise UserFacingError(f"Model categories must contain exactly: {', '.join(CATEGORY_KEYS)}")
    for key in CATEGORY_KEYS:
        item = categories[key]
        if not isinstance(item, dict):
            raise UserFacingError(f"Model category {key!r} must be an object")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
            raise UserFacingError(f"Model category {key!r} score must be an integer from 0 through 5")
        require_string(item.get("explanation"), f"categories.{key}.explanation")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 2:
            raise UserFacingError(f"Model category {key!r} evidence must be an array of at most 2 items")
        for evidence_item in evidence:
            if not isinstance(evidence_item, dict):
                raise UserFacingError(f"Evidence for {key!r} must be an object")
            require_string(evidence_item.get("excerpt"), f"categories.{key}.evidence.excerpt")

    advisories = result.get("content_advisories")
    if not isinstance(advisories, list) or len(advisories) > len(ADVISORY_KEYS):
        raise UserFacingError("Model content_advisories must be an appropriately sized array")
    seen_advisories: set[str] = set()
    for index, advisory in enumerate(advisories):
        if not isinstance(advisory, dict):
            raise UserFacingError(f"Model content_advisories[{index}] must be an object")
        advisory_type = advisory.get("type")
        if advisory_type not in ADVISORY_INFO:
            raise UserFacingError(f"Unknown content advisory type: {advisory_type!r}")
        if advisory_type in seen_advisories:
            raise UserFacingError(f"Duplicate content advisory type: {advisory_type}")
        seen_advisories.add(advisory_type)
        severity = advisory.get("severity")
        if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 5:
            raise UserFacingError(f"Advisory {advisory_type!r} severity must be an integer from 1 through 5")
        if advisory.get("frequency") not in ADVISORY_FREQUENCIES:
            raise UserFacingError(f"Advisory {advisory_type!r} has an invalid frequency")
        if advisory.get("context") not in ADVISORY_CONTEXTS:
            raise UserFacingError(f"Advisory {advisory_type!r} has an invalid context")
        require_string(advisory.get("explanation"), f"content_advisories.{advisory_type}.explanation")
        evidence_items = advisory.get("evidence")
        if not isinstance(evidence_items, list) or len(evidence_items) > 1:
            raise UserFacingError(f"Advisory {advisory_type!r} evidence must contain at most one item")
        for evidence_item in evidence_items:
            if not isinstance(evidence_item, dict):
                raise UserFacingError(f"Advisory {advisory_type!r} evidence must be an object")
            require_string(evidence_item.get("excerpt"), f"content_advisories.{advisory_type}.evidence.excerpt")

    age_guidance = result.get("age_band_guidance")
    if not isinstance(age_guidance, dict) or set(age_guidance) != set(AGE_BAND_KEYS):
        raise UserFacingError(f"Model age_band_guidance must contain exactly: {', '.join(AGE_BAND_KEYS)}")
    for key in AGE_BAND_KEYS:
        guidance = age_guidance[key]
        if not isinstance(guidance, dict):
            raise UserFacingError(f"Age-band guidance {key!r} must be an object")
        if guidance.get("suitability") not in {"appropriate", "review", "inappropriate"}:
            raise UserFacingError(f"Age-band guidance {key!r} has an invalid suitability")
        require_string(guidance.get("explanation"), f"age_band_guidance.{key}.explanation")


def normalize_for_verification(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"\[\d{2}:\d{2}:\d{2}(?:\.\d{3})?]", " ", value)
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def evidence_is_verified(excerpt: str, transcript: str) -> bool:
    normalized_excerpt = normalize_for_verification(excerpt)
    normalized_transcript = normalize_for_verification(transcript)
    return bool(normalized_excerpt) and normalized_excerpt in normalized_transcript


def already_analyzed(
    connection: sqlite3.Connection,
    subtitle_file_id: int,
    sha256: str,
    model: str,
) -> bool:
    row = connection.execute(
        """
        SELECT status FROM analyses
        WHERE subtitle_file_id = ? AND source_sha256 = ? AND model = ?
          AND prompt_version IN (?, ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (subtitle_file_id, sha256, model, *COMPATIBLE_PROMPT_VERSIONS),
    ).fetchone()
    return row is not None and row["status"] == "success"


def latest_fuzzy_repaired_paths(connection: sqlite3.Connection) -> set[str]:
    """Return subtitle paths whose latest successful analysis used fuzzy JSON recovery."""
    return {
        str(row["path"])
        for row in connection.execute(
            """
            SELECT sf.path
            FROM latest_analyses AS la
            JOIN subtitle_files AS sf ON sf.id = la.subtitle_file_id
            WHERE la.fuzzy_json_repaired = 1
            """
        )
    }


def current_analysis_requirement(
    connection: sqlite3.Connection, relative_path: str, sha256: str
) -> str | None:
    """Return why a current subtitle needs analysis, regardless of which model succeeded."""
    row = connection.execute(
        """
        SELECT an.fuzzy_json_repaired
        FROM analyses AS an
        JOIN subtitle_files AS sf ON sf.id = an.subtitle_file_id
        WHERE sf.path = ? AND an.source_sha256 = ?
          AND an.prompt_version IN (?, ?) AND an.status = 'success'
        ORDER BY an.id DESC
        LIMIT 1
        """,
        (relative_path, sha256, *COMPATIBLE_PROMPT_VERSIONS),
    ).fetchone()
    if row is None:
        return "missing"
    if row["fuzzy_json_repaired"]:
        return "fuzzy"
    return None


def begin_analysis(
    connection: sqlite3.Connection,
    run_id: int,
    subtitle_file_id: int,
    document: SubtitleDocument,
    model: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO analyses(
            run_id, subtitle_file_id, source_sha256, model, prompt_version, status, started_at
        ) VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (run_id, subtitle_file_id, document.sha256, model, PROMPT_VERSION, utc_now()),
    )
    connection.commit()
    return int(cursor.lastrowid)


def save_success(
    connection: sqlite3.Connection,
    analysis_id: int,
    result: dict[str, Any],
    raw_response: str,
    transcript: str,
    duration: float,
) -> None:
    connection.execute(
        """
        UPDATE analyses SET
            status = 'success', completed_at = ?, duration_seconds = ?,
            recommended_age = ?, confidence = ?, summary = ?, parent_guidance = ?,
            limitations_json = ?, raw_response = ?, fuzzy_json_repaired = ?,
            json_parse_method = ?, error = NULL
        WHERE id = ?
        """,
        (
            utc_now(),
            duration,
            result["recommended_age"],
            float(result["confidence"]),
            result["summary"].strip(),
            result["parent_guidance"].strip(),
            json.dumps(result["limitations"], ensure_ascii=False),
            raw_response,
            int(bool(result.get("_fuzzy_json_repaired"))),
            result.get("_json_parse_method") or "strict JSON",
            analysis_id,
        ),
    )
    for key, label, polarity in CATEGORIES:
        item = result["categories"][key]
        connection.execute(
            """
            INSERT INTO category_ratings(analysis_id, category, label, polarity, score, explanation)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (analysis_id, key, label, polarity, item["score"], item["explanation"].strip()),
        )
        for evidence_item in item["evidence"]:
            excerpt = evidence_item["excerpt"].strip()
            connection.execute(
                """
                INSERT INTO evidence(analysis_id, category, excerpt, timestamp, transcript_verified)
                VALUES (?, ?, ?, ?, ?)
                """,
                (analysis_id, key, excerpt, None, int(evidence_is_verified(excerpt, transcript))),
            )
    for advisory in result["content_advisories"]:
        info = ADVISORY_INFO[advisory["type"]]
        cursor = connection.execute(
            """
            INSERT INTO content_advisories(
                analysis_id, advisory_type, label, parent_category, severity,
                frequency, context, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                advisory["type"],
                info["label"],
                info["category"],
                advisory["severity"],
                advisory["frequency"],
                advisory["context"],
                advisory["explanation"].strip(),
            ),
        )
        advisory_id = int(cursor.lastrowid)
        for evidence_item in advisory["evidence"]:
            excerpt = evidence_item["excerpt"].strip()
            connection.execute(
                """
                INSERT INTO advisory_evidence(advisory_id, excerpt, transcript_verified)
                VALUES (?, ?, ?)
                """,
                (advisory_id, excerpt, int(evidence_is_verified(excerpt, transcript))),
            )
    age_band_meta = {key: (label, min_age, max_age) for key, label, min_age, max_age in AGE_BANDS}
    for key, guidance in result["age_band_guidance"].items():
        label, min_age, max_age = age_band_meta[key]
        connection.execute(
            """
            INSERT INTO age_band_guidance(
                analysis_id, age_band, label, min_age, max_age, suitability, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                key,
                label,
                min_age,
                max_age,
                guidance["suitability"],
                guidance["explanation"].strip(),
            ),
        )
    connection.commit()


def save_failure(connection: sqlite3.Connection, analysis_id: int, error: str, raw_response: str, duration: float) -> None:
    connection.execute(
        """
        UPDATE analyses SET status = 'error', completed_at = ?, duration_seconds = ?, raw_response = ?, error = ?
        WHERE id = ?
        """,
        (utc_now(), duration, raw_response or None, error[:10000], analysis_id),
    )
    connection.commit()


def save_interrupted(connection: sqlite3.Connection, analysis_id: int, duration: float) -> None:
    connection.execute(
        """
        UPDATE analyses SET
            status = 'interrupted', completed_at = ?, duration_seconds = ?, error = ?
        WHERE id = ?
        """,
        (utc_now(), duration, "Interrupted by user", analysis_id),
    )
    connection.commit()


def split_transcript(text: str, maximum: int) -> list[str]:
    """Bound each request while retaining every character of the transcript."""
    segments = []
    while len(text) > maximum:
        boundary = text.rfind("\n", maximum // 2, maximum)
        if boundary < 0:
            boundary = text.rfind(" ", maximum // 2, maximum)
        end = boundary + 1 if boundary >= 0 else maximum
        segments.append(text[:end])
        text = text[end:]
    if text:
        segments.append(text)
    return segments


def merge_segment_analyses(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Use conservative ratings; retain exact evidence from the selected segments."""
    result = dict(max(results, key=lambda item: item["recommended_age"]))
    result["confidence"] = min(item["confidence"] for item in results)
    result["categories"] = {
        key: (min if polarity == "positive" else max)(
            (item["categories"][key] for item in results), key=lambda category: category["score"]
        )
        for key, _label, polarity in CATEGORIES
    }
    advisories = {}
    for item in results:
        for advisory in item["content_advisories"]:
            previous = advisories.get(advisory["type"])
            if previous is None or advisory["severity"] > previous["severity"]:
                advisories[advisory["type"]] = advisory
    result["content_advisories"] = list(advisories.values())
    suitability = {"appropriate": 0, "review": 1, "inappropriate": 2}
    result["age_band_guidance"] = {
        key: max((item["age_band_guidance"][key] for item in results),
                 key=lambda band: suitability[band["suitability"]])
        for key in AGE_BAND_KEYS
    }
    result["limitations"] = [
        f"Analyzed all {len(results)} transcript segments separately; summary describes the most mature segment. "
        "Ratings use conservative segment scores; narrative context and frequency across segments may be incomplete.",
        *result["limitations"][:2],
    ]
    result["_fuzzy_json_repaired"] = any(item.get("_fuzzy_json_repaired") for item in results)
    result["_json_parse_method"] = "; ".join(dict.fromkeys(item.get("_json_parse_method", "strict JSON") for item in results))
    validate_analysis(result)
    return result


def request_analysis(
    document: SubtitleDocument,
    metadata: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    segments = split_transcript(document.transcript, args.max_transcript_chars)
    if len(segments) <= 1:
        return request_single_analysis(document, metadata, args)
    results, responses = [], []
    for index, segment in enumerate(segments, 1):
        print(f"  Analyzing transcript segment {index}/{len(segments)}")
        segment_metadata = dict(metadata)
        segment_metadata["summary"] = (
            f"This is transcript segment {index} of {len(segments)}. Assess only this segment. "
            + (metadata.get("summary") or "")
        )
        result, raw = request_single_analysis(replace(document, transcript=segment), segment_metadata, args)
        results.append(result)
        responses.append(raw)
    return merge_segment_analyses(results), json.dumps({"segments": responses}, ensure_ascii=False)


def request_single_analysis(
    document: SubtitleDocument,
    metadata: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    url = f"{args.server_url}/v1/chat/completions"
    payload = chat_completion_payload(document, metadata, args)
    last_error: Exception | None = None
    raw_response = ""
    for attempt in range(args.retries + 1):
        try:
            response = request_json(url, method="POST", body=payload, timeout=args.request_timeout)
            raw_response = extract_message_content(response)
            try:
                finish_reason = response["choices"][0].get("finish_reason")
            except (KeyError, IndexError, TypeError, AttributeError):
                finish_reason = None
            if finish_reason == "length":
                if "max_tokens" in payload:
                    last_error = UserFacingError(
                        f"Model response reached the {payload['max_tokens']}-token completion limit; "
                        "increase --max-output-tokens, use 0 for no explicit limit, or shorten the response"
                    )
                else:
                    last_error = UserFacingError(
                        "Model response exhausted the available context window with no explicit "
                        "output-token limit; increase the model context or shorten the prompt/response"
                    )
                break
            return parse_model_json(raw_response), raw_response
        except (UserFacingError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= args.retries:
                break
            delay = min(2**attempt, 8)
            print(f"  Attempt {attempt + 1} failed; retrying in {delay}s: {exc}", file=sys.stderr)
            time.sleep(delay)
    assert last_error is not None
    raise AnalysisResponseError(str(last_error), raw_response) from last_error


def save_run_error(
    connection: sqlite3.Connection,
    run_id: int,
    path: Path,
    stage: str,
    error: str,
) -> None:
    match = EPISODE_RE.search(path.stem)
    episode_code = None
    if match:
        episode_code = f"s{int(match.group('season')):02d}e{int(match.group('episode')):02d}"
    connection.execute(
        """
        INSERT INTO run_errors(run_id, path, episode_code, stage, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, path.as_posix(), episode_code, stage, error[:10000], utc_now()),
    )
    connection.commit()


def create_run(connection: sqlite3.Connection, args: argparse.Namespace, requested_count: int) -> int:
    settings = {
        "subtitle_dir": str(args.subtitle_dir),
        "database": str(args.database),
        "limit": args.limit,
        "episodes": args.episode,
        "force": args.force,
        "reanalyze_fuzzy": args.reanalyze_fuzzy,
        "reanalyze_needed": args.reanalyze_needed,
        "fresh": args.fresh,
        "fetch_metadata": args.fetch_metadata,
        "fetch_external_sources": args.fetch_external_sources,
        "temperature": args.temperature,
        "seed": args.seed,
        "max_output_tokens": args.max_output_tokens,
        "request_timeout": args.request_timeout,
        "retries": args.retries,
        "media_type": args.media_type,
        "language": args.language,
        "max_transcript_chars": args.max_transcript_chars,
        "show_title": args.show_title,
        "tvmaze_show_id": args.tvmaze_show_id,
    }
    cursor = connection.execute(
        """
        INSERT INTO analysis_runs(
            started_at, status, model, server_url, prompt_version, settings_json, requested_count
        ) VALUES (?, 'running', ?, ?, ?, ?, ?)
        """,
        (utc_now(), args.model, args.server_url, PROMPT_VERSION, json.dumps(settings, sort_keys=True), requested_count),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    status: str,
    succeeded: int,
    failed: int,
    skipped: int,
) -> None:
    connection.execute(
        """
        UPDATE analysis_runs SET
            completed_at = ?, status = ?, succeeded_count = ?, failed_count = ?, skipped_count = ?
        WHERE id = ?
        """,
        (utc_now(), status, succeeded, failed, skipped, run_id),
    )
    connection.commit()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze subtitle age appropriateness with a separately running llama.cpp server."
    )
    parser.add_argument("--subtitle-dir", type=Path, default=Path(DEFAULT_SUBTITLE_DIR))
    parser.add_argument("--subtitle-file", type=Path, help="Import just one subtitle file")
    parser.add_argument("--database", type=Path, default=Path(DEFAULT_DATABASE))
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--episode", action="append", type=normalize_episode_filter, default=[])
    parser.add_argument("--media-type", choices=("auto", "episode", "movie", "transcript"), default="auto")
    parser.add_argument("--show-title", help="Series title for episode subtitles")
    parser.add_argument("--tvmaze-show-id", type=int, help="Explicit TVMaze show ID; never guessed from a title")
    parser.add_argument("--title", help="Title override (only for a single subtitle file)")
    parser.add_argument("--year", type=int, help="Release year; distinguishes remakes")
    parser.add_argument("--manifest", type=Path, help="JSON metadata keyed by relative subtitle filename")
    parser.add_argument("--language", help="Expected language code, or auto for any language (new files default to en)")
    parser.add_argument("--max-transcript-chars", type=int, default=60000,
                        help="Maximum transcript characters per model request; longer files are analyzed in segments")
    parser.add_argument("--limit", type=int, default=0, help="Maximum files to consider; 0 means all")
    parser.add_argument("--force", action="store_true", help="Analyze even if this hash/model/prompt already succeeded")
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "--reanalyze-fuzzy",
        action="store_true",
        help="Reanalyze only subtitles whose latest successful analysis required fuzzy JSON repair",
    )
    selection_group.add_argument(
        "--reanalyze-needed",
        action="store_true",
        help=(
            "Analyze only current subtitles with no valid analysis from any model, "
            "or whose latest valid analysis required fuzzy JSON repair"
        ),
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Archive the existing database, then reanalyze every subtitle into a new database",
    )
    parser.add_argument(
        "--fetch-metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch TV episode metadata only for explicitly identified provider IDs or existing library bindings",
    )
    parser.add_argument(
        "--fetch-external-sources",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Cache verified Wikipedia pages, plus Wikisimpsons for identified Simpsons episodes",
    )
    parser.add_argument("--metadata-timeout", type=float, default=30.0)
    parser.add_argument("--preflight-timeout", type=float, default=5.0)
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=10000,
        help="Maximum completion tokens; 0 omits the limit (default: 10000)",
    )
    parser.add_argument("--check-server", action="store_true", help="Check the server and selected model, then exit")
    parser.add_argument("--init-db", action="store_true", help="Create/validate the database schema without requiring a server")
    args = parser.parse_args(argv)
    args.server_url = normalize_server_url(args.server_url)
    if args.subtitle_file:
        if not args.subtitle_file.is_file() or args.subtitle_file.suffix.lower() not in ALLOWED_SUFFIXES:
            parser.error("--subtitle-file must name an existing supported text subtitle")
        args.subtitle_file = args.subtitle_file.resolve()
        args.subtitle_dir = args.subtitle_file.parent
    if args.tvmaze_show_id is not None and args.tvmaze_show_id <= 0:
        parser.error("--tvmaze-show-id must be positive")
    if args.year is not None and not 1800 <= args.year <= 2200:
        parser.error("--year must be between 1800 and 2200")
    if args.max_transcript_chars < 1000:
        parser.error("--max-transcript-chars must be at least 1000")
    if args.language and not re.fullmatch(r"[a-z]{2,3}|auto", args.language):
        parser.error("--language must be a language code such as en, es, or auto")
    if args.limit < 0:
        parser.error("--limit cannot be negative")
    if args.fresh and (args.episode or args.limit or args.reanalyze_fuzzy or args.reanalyze_needed):
        parser.error(
            "--fresh processes all subtitles and cannot be combined with "
            "--episode, --limit, --reanalyze-fuzzy, or --reanalyze-needed"
        )
    if args.fresh and (args.check_server or args.init_db):
        parser.error("--fresh cannot be combined with --check-server or --init-db")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.max_output_tokens != 0 and args.max_output_tokens < 256:
        parser.error("--max-output-tokens must be 0 (unlimited) or at least 256")
    if args.request_timeout <= 0 or args.preflight_timeout <= 0 or args.metadata_timeout <= 0:
        parser.error("timeout values must be positive")
    return args


def import_options(args: argparse.Namespace) -> media.ImportOptions:
    try:
        manifest = media.load_manifest(args.manifest)
    except (ValueError, OSError) as exc:
        raise UserFacingError(f"Cannot load media manifest: {exc}") from exc
    existing: dict[str, dict[str, Any]] = {}
    if args.database.is_file():
        with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='subtitle_files'").fetchone():
                existing = media.existing_bindings(connection)
    return media.ImportOptions(
        media_type=args.media_type, show_title=args.show_title, title=args.title, year=args.year,
        tvmaze_show_id=args.tvmaze_show_id, language=args.language, manifest=manifest, existing=existing,
        legacy_root=args.database.resolve().parent / DEFAULT_SUBTITLE_DIR,
    )


def run(args: argparse.Namespace) -> int:
    if args.init_db:
        with connect_database(args.database) as connection:
            register_prompt(connection)
        print(f"Initialized database schema at {args.database}")
        return 0

    # This is deliberately first so a missing server fails before corpus scanning,
    # metadata network access, or database writes.
    models = preflight_server(args.server_url, args.model, args.preflight_timeout)
    print(f"llama.cpp ready at {args.server_url}; selected model: {args.model}")
    if args.check_server:
        print(f"Available models: {', '.join(models)}")
        return 0

    options = import_options(args)
    paths = ([args.subtitle_file] if args.subtitle_file else
             discover_subtitles(args.subtitle_dir, set(args.episode), options))
    if args.title and len(paths) != 1:
        raise UserFacingError("--title requires exactly one subtitle; use a manifest for multiple titles")

    if args.fresh:
        backup = archive_database(args.database)
        if backup is None:
            print(f"Fresh run requested; no existing database found at {args.database}")
        else:
            print(f"Fresh run requested; archived existing database to {backup}")

    connection = connect_database(args.database)
    run_id: int | None = None
    succeeded = failed = skipped = 0
    try:
        register_prompt(connection)
        if args.reanalyze_fuzzy:
            fuzzy_paths = latest_fuzzy_repaired_paths(connection)
            root = args.subtitle_dir.resolve()
            paths = [
                path
                for path in paths
                if media.identify(path, root, options)["storage_path"] in fuzzy_paths
            ]
            print(f"Selected {len(paths)} latest fuzzy-repaired analysis(es) for reanalysis")
        elif args.reanalyze_needed:
            root = args.subtitle_dir.resolve()
            selected_paths: list[Path] = []
            missing_count = fuzzy_count = 0
            for path in paths:
                relative_path = media.identify(path, root, options)["storage_path"]
                try:
                    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    selected_paths.append(path)
                    missing_count += 1
                    continue
                requirement = current_analysis_requirement(connection, relative_path, sha256)
                if requirement == "missing":
                    selected_paths.append(path)
                    missing_count += 1
                elif requirement == "fuzzy":
                    selected_paths.append(path)
                    fuzzy_count += 1
            paths = selected_paths
            print(
                f"Selected {len(paths)} analysis(es) needing replacement: "
                f"{missing_count} without a valid current analysis, "
                f"{fuzzy_count} fuzzy-repaired"
            )
        if args.limit:
            paths = paths[: args.limit]
        print(f"Discovered {len(paths)} subtitle file(s) to consider")
        if not paths:
            print("No matching subtitles require analysis.")
            return 0

        # Register identities before enrichment, including newly imported titles.
        identities = [media.identify(path, args.subtitle_dir, options) for path in paths]
        for info in identities:
            media.register_record(connection, info)
        connection.commit()
        if args.fetch_metadata:
            shows = {}
            for info in identities:
                provider_id = json.loads(info.get("provider_ids_json", "{}")).get("tvmaze_show_id")
                if provider_id and info.get("media_type") == "episode":
                    shows[provider_id] = info.get("series_title")
            for show_id, show_title in shows.items():
                try:
                    count = update_tvmaze_metadata(connection, fetch_tvmaze_metadata(args.metadata_timeout, show_id),
                                                   show_id, show_title)
                    print(f"Updated metadata for {count} TV episode(s) in {show_title or show_id}")
                except UserFacingError as exc:
                    print(f"Warning: metadata refresh failed; using local metadata: {exc}", file=sys.stderr)

        if args.fetch_external_sources:
            try:
                from subtitle_age_rating_enrichment import cache_episode_sources

                episode_codes = {info["episode_code"] for info in identities}
                source_counts = cache_episode_sources(
                    connection, episode_codes=episode_codes
                )
                print(f"External episode-source cache: {source_counts}")
            except (ImportError, RuntimeError, OSError) as exc:
                print(
                    f"Warning: external source refresh failed; continuing with cached/basic metadata: {exc}",
                    file=sys.stderr,
                )

        run_id = create_run(connection, args, len(paths))
        for index, path in enumerate(paths, start=1):
            label = f"[{index}/{len(paths)}] {path.name}"
            try:
                document = load_subtitle(path, args.subtitle_dir, options)
                subtitle_file_id = upsert_subtitle(connection, document)
                quality_status, quality_issues, quality_metrics = assess_subtitle_quality(document)
                save_subtitle_quality(
                    connection, subtitle_file_id, quality_status, quality_issues, quality_metrics
                )
                connection.commit()
            except (OSError, UserFacingError) as exc:
                failed += 1
                save_run_error(connection, run_id, path, "parse", str(exc))
                print(f"{label}: parse failure: {exc}", file=sys.stderr)
                continue

            if quality_status == "corrupt":
                skipped += 1
                detail = "; ".join(quality_issues) or "Subtitle failed quality checks"
                save_run_error(connection, run_id, path, "quality", detail)
                print(f"{label}: corrupt subtitle skipped: {detail}", file=sys.stderr)
                continue

            if not (args.force or args.reanalyze_fuzzy or args.reanalyze_needed) and already_analyzed(
                connection, subtitle_file_id, document.sha256, args.model
            ):
                skipped += 1
                print(f"{label}: already analyzed; skipped")
                continue

            analysis_id = begin_analysis(connection, run_id, subtitle_file_id, document, args.model)
            started = time.monotonic()
            raw_response = ""
            print(f"{label}: analyzing {document.cue_count} cues / {len(document.transcript)} chars")
            try:
                metadata = get_episode_metadata(connection, document.episode_code)
                result, raw_response = request_analysis(document, metadata, args)
                duration = time.monotonic() - started
                save_success(connection, analysis_id, result, raw_response, document.transcript, duration)
                succeeded += 1
                repair_note = ""
                if result.get("_fuzzy_json_repaired"):
                    repair_note = f"; fuzzy JSON: {result.get('_json_parse_method')}"
                print(
                    f"{label}: age {result['recommended_age']}+, confidence {float(result['confidence']):.2f} "
                    f"({duration:.1f}s{repair_note})"
                )
            except KeyboardInterrupt:
                duration = time.monotonic() - started
                save_interrupted(connection, analysis_id, duration)
                print(f"{label}: interrupted", file=sys.stderr, flush=True)
                raise
            except (UserFacingError, OSError, ValueError) as exc:
                duration = time.monotonic() - started
                raw_response = getattr(exc, "raw_response", raw_response)
                save_failure(connection, analysis_id, str(exc), raw_response, duration)
                failed += 1
                print(f"{label}: analysis failure: {exc}", file=sys.stderr)

        status = "completed_with_errors" if failed else "completed"
        finish_run(connection, run_id, status, succeeded, failed, skipped)
    except KeyboardInterrupt:
        if run_id is not None:
            finish_run(connection, run_id, "interrupted", succeeded, failed, skipped)
        print("Interrupted; completed results are safely stored and the next run can resume.", file=sys.stderr)
        return 130
    except Exception:
        if run_id is not None:
            finish_run(connection, run_id, "aborted", succeeded, failed, skipped)
        raise
    finally:
        connection.close()

    print(
        f"Run complete: {succeeded} succeeded, {skipped} skipped, {failed} failed. "
        f"Database: {args.database}"
    )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except UserFacingError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
