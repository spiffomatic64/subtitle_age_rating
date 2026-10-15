# Media Compass

Explainable, subtitle-based age guidance for movies, TV episodes, and other transcripts. Analyze your own subtitle files with a local llama.cpp model, then browse a searchable library tailored to a child's age and content sensitivities.

The library includes category scores, detailed content advisories, supporting dialogue, age-band guidance, favorites, watched state, and notes. Metadata and artwork are optional. Analysis runs from the command line; the web app browses the saved results.

These are model-generated estimates inspired by Common Sense Media's public methodology, not official classifications. Subtitles can miss visual violence, nudity, frightening imagery, and other non-dialogue content. Use the explanations and uncertainty notes when deciding what to watch.

## Quick start

### 1. Install Python dependencies

Use Python **3.10 or newer**; the project is tested with Python 3.11. Open a terminal in the repository root and create a virtual environment:

```sh
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS/Linux:

```sh
source .venv/bin/activate
```

Then install the dependencies:

```sh
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use `.venv/Scripts/python.exe` instead of `python` in the following commands. Activation is optional when you call the environment's interpreter directly.

### 2. Start a local model server

Install an appropriate [llama.cpp build](https://github.com/ggml-org/llama.cpp#quick-start) and obtain an instruction-tuned GGUF model that can reliably produce structured JSON in your subtitle language. Model weights are not bundled. Choose a model and context size that fit your RAM/VRAM.

Create `models/` and `subtitles/` directories. Put your GGUF at `models/model.gguf` or substitute its actual path. With `llama-server` on your `PATH`, run:

```sh
llama-server --model "models/model.gguf" --alias subtitle-rater --host 127.0.0.1 --port 11435 --ctx-size 32768 --parallel 1 --jinja
```

Keep this terminal open. On Windows, use `llama-server.exe`; if it is in the current directory rather than on `PATH`, invoke `.\llama-server.exe`. For a CPU-only run, add `--n-gpu-layers 0`. See the [official server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) for hardware configuration.

The examples below use the server alias `subtitle-rater`. If your server advertises another model ID, substitute that ID in `--model`. Omitting `--model` uses the application's existing default, `qwen3.6-27b`; the alias does not require a particular model architecture.

Check the connection before analyzing files:

```sh
python subtitle_age_rating.py --model subtitle-rater --check-server
```

### 3. Analyze a subtitle file

Put a complete subtitle in `subtitles/`, then run:

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-file "subtitles/Example Film.2020.en.srt" --media-type movie --title "Example Film" --year 2020
```

Start with one file or use `--limit 2` for a directory. Processing time depends on the model, hardware, and transcript length. The analyzer checks the server, parses and checks the subtitles, requests ratings, and saves results to SQLite.

### 4. Browse the results

In another terminal, activate the same environment and run:

```sh
python subtitle_age_rating_web.py
```

Open <http://127.0.0.1:5005>. Select a show or media type, search titles and dialogue, tune the profile's sensitivities, and open a title to inspect its evidence. The web app can browse saved results without the model server running. Restart an already-running web process after updating application code.

## Where to get subtitles

These sites provide searchable subtitle libraries:

| Source | Useful for |
| --- | --- |
| [OpenSubtitles](https://www.opensubtitles.com/) | Movie and TV subtitles in multiple languages. |
| [Addic7ed](https://www.addic7ed.com/) | Community subtitles and translations, including TV episodes and movies. |
| [TVsubtitles](https://www.tvsubtitles.net/) | TV subtitles organized by show, season, and episode. |

Download the subtitle file, extract it if it is in an archive, and place the text file in `subtitles/`. Account requirements and download limits depend on the site. These links are for manual downloads; the generic importer does not search or download subtitles for you.

Match the exact title, year, episode, language, and cut of the film. Prefer a **complete dialogue track**: a forced-only track usually covers just selected lines and can give an incomplete picture of the content. SDH/closed-caption tracks can add useful sound cues. Check the beginning and end for missing text, unrelated material, or obvious decoding problems.

## Extract subtitles from a local video with FFmpeg

Install [FFmpeg](https://ffmpeg.org/download.html) with `ffmpeg` and `ffprobe` on your `PATH`, and create the `subtitles/` output directory. List the embedded subtitle tracks, including their codec, language, title, and forced/default flags:

```sh
ffprobe -v error -select_streams s -show_entries "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced" -of json "video.mkv"
```

Export the **first text subtitle track** as SRT:

```sh
ffmpeg -i "video.mkv" -map 0:s:0 -c:s srt "subtitles/Example Film.2020.en.srt"
```

This also converts text codecs such as ASS or MP4 `mov_text` to SRT. `0:s:0` means the first subtitle track; use `0:s:1` for the second. To select a global stream index shown by `ffprobe`, use `0:3` for index 3 instead. Only the mapped subtitle is written; the video is not re-encoded or modified.

To preserve an ASS track's original format, select that track and stream-copy to a matching extension:

```sh
ffmpeg -i "video.mkv" -map 0:s:0 -c:s copy "subtitles/Example Film.2020.en.ass"
```

The copy example requires an ASS input track. See [FFmpeg stream selection](https://ffmpeg.org/ffmpeg.html#Stream-selection) and [ffprobe documentation](https://ffmpeg.org/ffprobe.html) for details.

Image-based tracks such as PGS (`hdmv_pgs_subtitle`) and VobSub (`dvd_subtitle`) need OCR before this application can read them. [Subtitle Edit's OCR tools](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/features/ocr.md) can convert subtitle images into text. Burned-in captions are part of the video image, not an extractable subtitle stream. If no subtitle tracks exist, obtain a transcript through transcription or another subtitle source first.

## Supported files and library organization

| Input | Support |
| --- | --- |
| `.srt`, `.vtt` | Text subtitle cues. |
| `.ass`, `.ssa` | Dialogue text; styling is removed for analysis. |
| `.sub` | Text SubViewer/MicroDVD only; bitmap VobSub is not supported. |
| `.txt` | Plain-text transcripts. |
| Video, `.sup`, bitmap `.sub`/`.idx`, ZIP/RAR archives | Extract, OCR, or unpack to a supported text format first. |

Use UTF-8 when possible. UTF-16 with a byte-order mark and Western legacy encodings are also handled; convert other encodings to UTF-8 before import.

Run commands from the repository root so the default database and artwork directory resolve consistently. Directory imports scan recursively. One possible layout is:

```text
subtitles/
  movies/Example Film.2020.en.srt
  shows/Example Series/Season 1/Example.Series.S01E01.en.srt
  other/interview.txt
```

Keep the library's file locations stable after import. New subtitle paths are stored as absolute paths; moving a file can create another track record. Multiple tracks for the same identified title share a library entry, and the detail page lets you select the analyzed track that supplies its ratings and evidence. Other analyses remain saved.

### TV episodes

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-dir "subtitles/shows/Example Series" --show-title "Example Series"
```

Filenames may contain `S01E01` or `1x01`. The importer can infer a show name from the filename prefix or folder; `--show-title` makes it explicit. Set `--year` to distinguish shows with the same name. Show-specific IDs keep episodes from different series separate even when both are `S01E01`.

For a known TVMaze catalog, supply its numeric show ID with `--tvmaze-show-id`; the ID is the number in a TVMaze show URL. Choose this binding when first importing the show. Adding a provider binding later can change the media identity. TV metadata is fetched only for explicitly identified providers or existing library bindings.

### Movies and standalone transcripts

Use `--media-type movie`, a title, and a year as in the quick start. A directory of movie subtitles can use `--media-type movie`; review inferred titles and use a manifest for release filenames or ambiguous names.

A file without an episode code becomes a standalone transcript unless you explicitly identify it as a movie. For an interview or other text file:

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-file "subtitles/other/interview.txt" --title "An interview"
```

`--title` requires a single selected file. Use a manifest for multiple title overrides.

### Mixed libraries and filenames without episode codes

Copy [media-manifest.example.json](media-manifest.example.json) to `media-manifest.json` and edit it. The `files` keys must be paths relative to `--subtitle-dir`, not absolute paths:

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-dir subtitles --manifest media-manifest.json
```

Each entry can specify:

| Fields | Purpose |
| --- | --- |
| `type`, `title`, `year` | Identify an `episode`, `movie`, or `transcript`. |
| `show`, `season`, `episode` | Identify a TV episode, including files named only `01.srt`. |
| `edition` | Distinguish cuts of a movie. |
| `language` | Expected language code or `auto`. |
| `tvmaze_show_id` | Explicit TVMaze show binding. |
| `summary`, `artwork_url`, `source_url` | Optional local synopsis, artwork location on the web, and information link. |
| `wikipedia_title` | Exact Wikipedia page title for optional source enrichment. |

File-specific manifest values override the matching command-line defaults. Generic Wikipedia enrichment requires an explicit page title; ambiguous titles are not searched automatically. Local files need no metadata account or API key.

## Languages, long transcripts, and repeat runs

New files default to English quality checks. Choose another language or accept automatically detected languages:

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-dir subtitles --language es
python subtitle_age_rating.py --model subtitle-rater --subtitle-dir subtitles --language auto
```

The expected language is stored per track. Language acceptance does not translate the text; your model must understand it. Confident language mismatches, decoding damage, and corrupt text are flagged or skipped.

Long transcripts are split into requests of at most `--max-transcript-chars` characters, retaining all input text. Combined age and concern scores use the strongest segment; positive scores use the weakest. The summary describes the most mature segment, and limitations explain the missing cross-segment narrative context. Character counts are not token counts: allow room for the rubric, metadata, and model response within your server's context window.

| Option | Behavior |
| --- | --- |
| `--server-url URL` | Model server base URL; default `http://127.0.0.1:11435`. |
| `--database PATH` | SQLite library; default `subtitle_age_ratings.sqlite3`. Use the same path in the web app. |
| `--subtitle-dir PATH` | Recursively scan a directory; default `subtitle_cache/`. |
| `--subtitle-file PATH` | Select one supported file. |
| `--limit N` | Consider at most N files; helpful for a first run. |
| `--episode s01e01` | Filter episode numbers within the chosen directory; repeat for several episodes. |
| `--max-transcript-chars N` | Segment limit; default 60000, minimum 1000. Lower it for smaller context windows. |
| `--max-output-tokens N` | Completion allowance; default 10000. Zero removes the explicit output limit. |
| `--reanalyze-needed` | Analyze files missing a valid compatible result or with a fuzzy-repaired result, regardless of which model succeeded. |
| `--reanalyze-fuzzy` | Replace only the latest successful results that needed JSON repair. |
| `--force` | Analyze again while retaining history. |
| `--fresh` | Archive the current database and start a new library. Use only when you intend to replace the active library. |

Normal reruns skip unchanged files already analyzed with the selected model and a compatible prompt. Changing the model can trigger new analyses. Interrupted runs retain completed results. Fuzzy-repaired JSON results are labeled in the web app; a later strict valid result clears the current flag.

Use `python subtitle_age_rating.py --help` and `python subtitle_age_rating_web.py --help` for all options.

## Optional metadata, artwork, and network access

Subtitle analysis is sent to the configured model endpoint. With a local endpoint, inference stays on your machine. TVMaze, Wikipedia, and artwork requests use the internet when applicable; Wikisimpsons is used only for identified Simpsons episodes. Generic subtitle downloads are manual.

Disable metadata and source refresh while analyzing:

```sh
python subtitle_age_rating.py --model subtitle-rater --subtitle-dir subtitles --no-fetch-metadata --no-fetch-external-sources
```

These flags do not erase or ignore source summaries already cached in the database. Missing artwork may still be downloaded when browsing a detail page. Bulk caching is optional and resumable:

```sh
python subtitle_age_rating_web.py --cache-sources
python subtitle_age_rating_web.py --cache-artwork
```

Add `--limit 10` for a small batch or `--force-cache` to refresh cached entries. Refresh quality checks without a model server:

```sh
python subtitle_age_rating_web.py --scan-subtitles --subtitle-dir subtitles
```

For a separate library, pass the same `--database "my-library.sqlite3"` to both programs. To initialize an empty library without a model server, use `python subtitle_age_rating_web.py --init-db`.

## Data, privacy, and upgrades

The SQLite database stores profiles, preferences, favorites, watched state, notes, source provenance, artwork records, analysis history, and subtitle paths. Keep it private. Model weights, downloaded content, database files/backups, local manifests, and personal configuration are excluded from normal Git additions. Put local media under the ignored `subtitles/`, `subtitle_cache/`, `videos/`, or `downloads/` directories; use `models/` for weights.

Git ignores do not protect already-tracked files, forced additions, or direct uploads. Before publishing changes, review `git status --short --untracked-files=all` and `git add --dry-run .`.

The web app binds to localhost and has no login. `--host` can expose it to a trusted local network; anyone who can reach it can access the shared profile and notes. Avoid `--debug` on a shared interface. Sessions use a random signing key on each app instance; set `SUBTITLE_AGE_RATING_SECRET_KEY` in the process environment if sessions must survive restarts. `.env` files are not loaded automatically.

Back up an existing library with SQLite's backup API, which includes committed WAL data:

```sh
python -c "import sqlite3; source=sqlite3.connect('subtitle_age_ratings.sqlite3'); backup=sqlite3.connect('ratings-backup.sqlite3'); source.backup(backup); backup.close(); source.close()"
```

Use your actual database filenames and a new destination name. To restore a backup, stop the analyzer and web app first, then restore it as a separate database and point both programs at that file with `--database`.

Earlier Simpsons libraries upgrade automatically. The first legacy-schema upgrade creates a SQLite backup named `subtitle_age_ratings.sqlite3.backup-media-v4-<timestamp>` before adding fields. Existing IDs, paths, analyses, evidence, preferences, notes, sources, artwork records, and `/episode/s01e01` links remain valid. Successful compatible v5 analyses remain reusable.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Server unavailable | Keep `llama-server` running and verify its host/port match `--server-url`. |
| Model not advertised | Use `--check-server` and choose an advertised ID; the quick start uses `--alias subtitle-rater` and `--model subtitle-rater`. |
| Out of memory or context exhausted | Use a smaller model, adjust server context/offload settings, or reduce `--max-transcript-chars` (for example, 20000). Leave room for the JSON response. |
| Empty or truncated model output | Check model/template compatibility and `--max-output-tokens`; incomplete responses are not treated as successful ratings. |
| No titles in the browser | Run the analyzer first and confirm both programs use the same working directory and `--database`. |
| Episode numbers missing from filenames | Use `S01E01`/`1x01` or a manifest with explicit season/episode fields. |
| Wrong or missing names/artwork | Use explicit titles, year, provider IDs, or manifest metadata; transcript-only analysis works without artwork. |
| Subtitle marked corrupt | Check encoding, configured language, complete dialogue, and whether the file is text rather than bitmap subtitles. |
| FFmpeg cannot convert subtitles | Inspect `codec_name`; image-based tracks require OCR, and stream-copy needs a matching output format. |

## Project files and development

| File | Role |
| --- | --- |
| `subtitle_age_rating.py` | Subtitle parsing, quality checks, model requests, validation, and saved analysis history. |
| `subtitle_age_rating_media.py` | Media identities, manifest import, and additive schema migration. |
| `subtitle_age_rating_enrichment.py` | Optional source summaries and artwork caching. |
| `subtitle_age_rating_web.py` | Local Flask library browser and profile settings. |
| `templates/`, `static/` | Browser interface. |
| `media-manifest.example.json` | Shareable metadata example. |
| `llamacpp_models.example.ini` | Optional model-router preset; copy to ignored `llamacpp_models.ini` and configure your GGUF path. |
| `test_subtitle_age_rating_*.py` | Synthetic-data regression tests, including legacy migration and mixed-media behavior. |

The optional INI uses the same `subtitle-rater` alias as the quick start. It is for llama.cpp router mode; direct single-model startup above does not need it. The [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) describes router configuration.

Run the test suite:

```sh
python -m pytest -q
```

Tests use temporary databases and mocked inference; they do not require model weights, subtitle downloads, or your populated library. When extending the schema, preserve existing IDs and history, keep migrations additive, and test upgrades from a populated legacy schema.

The JSON API is available at `/api/media` with a `media` collection. `/api/episodes` retains the historical `episodes` collection; `/media/<id>` and historical `/episode/<id>` detail routes both work.
