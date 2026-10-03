import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.middleware.proxy_fix import ProxyFix

DEFAULT_BACKEND_URL = "http://127.0.0.1:6544"
PRIMARY_RECORDING_GROUP = "Seasons"
ACTIVE_RECORDING_GROUPS = {"Default", PRIMARY_RECORDING_GROUP}
RECORDINGS_PAGE_SIZE = 5000
RECORDINGS_CACHE_SECONDS = 120


def _title_sort_key(title: str) -> tuple:
    normalized = re.sub(r"^(?:a|an|the)\s+", "", title.casefold().strip())
    parts = tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", normalized)
    )
    return parts, title.casefold()


def _recording_preview_url(program: dict) -> str | None:
    recorded_id = (program.get("Recording") or {}).get("RecordedId")
    if not isinstance(recorded_id, int) or recorded_id <= 0:
        return None
    return f"/api/preview?recorded_id={recorded_id}"


def _active_recording(app: Flask, recorded_id: int) -> dict | None:
    return next(
        (
            program
            for program in _load_all_recordings(app)
            if (program.get("Recording") or {}).get("RecordedId") == recorded_id
            and (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
        ),
        None,
    )


def _invalidate_recordings_cache(app: Flask) -> None:
    cache_path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
    lock_path = cache_path.with_suffix(f"{cache_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            cache_path.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _recording_url(app: Flask, recorded_id: int) -> str:
    return f"{app.config['MYTHTV_BACKEND_URL']}/Content/GetRecording?RecordedId={recorded_id}"


def _probe_recording(app: Flask, recorded_id: int) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {
            "video_codec": None,
            "video_profile": None,
            "video_level": None,
            "video_pixel_format": None,
            "audio_codec": None,
            "subtitles": [],
        }
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,profile,level,pix_fmt:stream_tags=language,title",
                "-of",
                "json",
                _recording_url(app, recorded_id),
            ],
            capture_output=True,
            timeout=30,
            check=True,
            text=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        app.logger.info("Recording stream probe failed: %s", error)
        return {
            "video_codec": None,
            "video_profile": None,
            "video_level": None,
            "video_pixel_format": None,
            "audio_codec": None,
            "subtitles": [],
        }

    subtitles = []
    codecs = {
        "video_codec": None,
        "video_profile": None,
        "video_level": None,
        "video_pixel_format": None,
        "audio_codec": None,
    }
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and codecs["video_codec"] is None:
            codecs["video_codec"] = stream.get("codec_name")
            codecs["video_profile"] = stream.get("profile")
            codecs["video_level"] = stream.get("level")
            codecs["video_pixel_format"] = stream.get("pix_fmt")
        elif codec_type == "audio" and codecs["audio_codec"] is None:
            codecs["audio_codec"] = stream.get("codec_name")
        elif codec_type == "subtitle":
            tags = stream.get("tags") or {}
            subtitles.append(
                {
                    "index": stream.get("index"),
                    "codec": stream.get("codec_name"),
                    "language": tags.get("language", "und"),
                    "title": tags.get("title", ""),
                }
            )
    return {**codecs, "subtitles": subtitles}


def _load_all_recordings(app: Flask) -> list[dict]:
    cache_path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(f"{cache_path.suffix}.lock")

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                cached = json.loads(cache_path.read_text())
                cache_age = time.time() - cached["cached_at"]
                if cache_age <= RECORDINGS_CACHE_SECONDS:
                    return cached["programs"]
            except (OSError, ValueError, KeyError, TypeError):
                pass

            programs = []
            start_index = 0
            total = None
            while total is None or start_index < total:
                response = requests.get(
                    f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedList",
                    params={
                        "StartIndex": start_index,
                        "Count": RECORDINGS_PAGE_SIZE,
                        "Descending": "true",
                    },
                    headers={"Accept": "application/json"},
                    timeout=app.config["MYTHTV_GROUPS_TIMEOUT"],
                )
                response.raise_for_status()
                payload = response.json().get("ProgramList", {})
                page = payload.get("Programs", [])
                total = payload.get("TotalAvailable", 0)
                programs.extend(page)
                count = payload.get("Count", len(page))
                if count <= 0:
                    break
                start_index += count

            temporary_path = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.tmp"
            )
            temporary_path.write_text(
                json.dumps({"cached_at": time.time(), "programs": programs})
            )
            os.replace(temporary_path, cache_path)
            return programs
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

    @app.after_request
    def set_browser_wasm_isolation_headers(response):
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        return response

    app.config["MYTHTV_BACKEND_URL"] = os.environ.get(
        "MYTHTV_BACKEND_URL", DEFAULT_BACKEND_URL
    ).rstrip("/")
    app.config["MYTHTV_TIMEOUT"] = 8
    app.config["MYTHTV_GROUPS_TIMEOUT"] = 30
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(
        Path(__file__).resolve().parents[1] / ".run" / "recordings-cache.json"
    )
    app.config["MYTHTV_TRANSCODE_DIR"] = str(
        Path(__file__).resolve().parents[1] / ".run" / "transcodes"
    )

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/programs")
    def programs():
        start_index = max(0, request.args.get("start", default=0, type=int))
        page_count = min(100, max(1, request.args.get("count", default=40, type=int)))
        try:
            response = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedList",
                params={
                    "StartIndex": start_index,
                    "Count": page_count,
                    "Descending": "true",
                },
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            payload = response.json().get("ProgramList", {})
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV recorded-list request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        return jsonify(
            programs=payload.get("Programs", []),
            start=payload.get("StartIndex", 0),
            count=payload.get("Count", 0),
            total=payload.get("TotalAvailable", 0),
            as_of=payload.get("AsOf"),
        )

    @app.get("/api/program-groups")
    def program_groups():
        try:
            programs = _load_all_recordings(app)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV group-list request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        default_programs = [
            program
            for program in programs
            if (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
        ]
        groups = {}
        for program in default_programs:
            title = program.get("Title") or "Untitled recording"
            latest_start = program.get("StartTime") or ""
            group = groups.get(title)
            if group is None:
                artwork = (program.get("Artwork") or {}).get("ArtworkInfos") or []
                cover = next(
                    (
                        item.get("URL")
                        for item in artwork
                        if item.get("Type") == "coverart"
                    ),
                    None,
                )
                group = {
                    "title": title,
                    "recording_count": 0,
                    "watched_count": 0,
                    "latest_start": latest_start,
                    "channel_name": (program.get("Channel") or {}).get("ChannelName"),
                    "category": program.get("Category"),
                    "artwork_url": cover,
                    "preview_url": _recording_preview_url(program),
                }
                groups[title] = group
            elif not group["artwork_url"]:
                artwork = (program.get("Artwork") or {}).get("ArtworkInfos") or []
                group["artwork_url"] = next(
                    (
                        item.get("URL")
                        for item in artwork
                        if item.get("Type") == "coverart"
                    ),
                    None,
                )

            group["recording_count"] += 1
            if "WATCHED" in (program.get("ProgramFlagNames") or "").split("|"):
                group["watched_count"] += 1
            if latest_start > group["latest_start"]:
                group["latest_start"] = latest_start

        sorted_groups = sorted(
            groups.values(), key=lambda group: _title_sort_key(group["title"])
        )
        return jsonify(groups=sorted_groups, total=len(default_programs))

    @app.get("/api/program-group")
    def program_group():
        title = request.args.get("title", "")
        if not title:
            return jsonify(error="A program title is required"), 400

        try:
            programs = _load_all_recordings(app)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV program-group request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        matches = [
            program
            for program in programs
            if (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
            and (program.get("Title") or "Untitled recording") == title
        ]
        if not matches:
            return jsonify(error="Program group not found"), 404
        matches.sort(key=lambda program: program.get("StartTime") or "")
        return jsonify(title=title, programs=matches, total=len(matches))

    @app.delete("/api/recordings/<int:recorded_id>")
    def delete_recording(recorded_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            return jsonify(error="Explicit confirmation is required"), 400
        if not request.headers.get("X-Authenticated-User", "").strip():
            return jsonify(error="An Apache-authenticated user is required"), 401

        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV delete lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        params = {
            "RecordedId": recorded_id,
            "ChanId": (program.get("Channel") or {}).get("ChanId", 0),
            "StartTime": program.get("StartTime", ""),
            "ForceDelete": False,
            "AllowRerecord": False,
        }
        try:
            upstream = requests.post(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/DeleteRecording",
                json=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
            payload = upstream.json()
            deleted = isinstance(payload, dict) and payload.get("bool") is True
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV recording deletion failed: %s", error)
            return jsonify(error="Could not delete recording from MythTV"), 502

        if not deleted:
            return jsonify(error="MythTV did not delete the recording"), 502

        try:
            _invalidate_recordings_cache(app)
        except OSError as error:
            app.logger.warning("Could not invalidate recordings cache: %s", error)
        return jsonify(deleted=True)

    @app.get("/api/artwork")
    def artwork():
        source_url = request.args.get("url", "")
        parsed_url = urlsplit(source_url)
        parameters = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
        if (
            parsed_url.scheme
            or parsed_url.netloc
            or parsed_url.path != "/Content/GetImageFile"
            or set(parameters) != {"StorageGroup", "FileName"}
        ):
            return jsonify(error="Invalid artwork URL"), 400

        try:
            upstream = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Content/GetImageFile",
                params=parameters,
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV artwork request failed: %s", error)
            return Response(status=404)

        return Response(
            upstream.content,
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/preview")
    def preview():
        recorded_id = request.args.get("recorded_id", type=int)
        if recorded_id is None or recorded_id <= 0:
            return jsonify(error="A valid recording ID is required"), 400

        try:
            upstream = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Content/GetPreviewImage",
                params={"RecordedId": recorded_id, "Width": 640, "Format": "JPG"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV preview request failed: %s", error)
            return Response(status=404)

        return Response(
            upstream.content,
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/playback/<int:recorded_id>")
    def playback_info(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        channel_id = (program.get("Channel") or {}).get("ChanId", 0)
        start_time = program.get("StartTime", "")
        params = {
            "RecordedId": recorded_id,
            "ChanId": channel_id,
            "StartTime": start_time,
            "OffsetType": "Duration",
        }
        try:
            comm_breaks = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCommBreak",
                params={**params, "IncludeFps": "false"},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            comm_breaks.raise_for_status()
            cut_list = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCutList",
                params={**params, "IncludeFps": "false"},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            cut_list.raise_for_status()
            last_position = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetLastPlayPos",
                params=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            last_position.raise_for_status()
            bookmark = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetSavedBookmark",
                params=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            bookmark.raise_for_status()
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV playback metadata request failed: %s", error)
            return jsonify(error="Could not load MythTV playback metadata"), 502

        return jsonify(
            program=program,
            media=_probe_recording(app, recorded_id),
            comm_breaks=(comm_breaks.json().get("CutList") or {}).get("Cuttings", []),
            cut_list=(cut_list.json().get("CutList") or {}).get("Cuttings", []),
            last_position=int(last_position.json().get("long", 0)),
            bookmark=int(bookmark.json().get("long", 0)),
        )

    @app.get("/api/playback/<int:recorded_id>/live-status")
    def playback_live_status(recorded_id):
        try:
            cached_program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV live playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if cached_program is None:
            return jsonify(error="Recording not found"), 404

        try:
            current = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecorded",
                params={"RecordedId": recorded_id},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            current.raise_for_status()
            payload = current.json()
            program = payload.get("Program") if isinstance(payload, dict) else None
            if not isinstance(program, dict):
                return jsonify(error="Invalid MythTV recording status response"), 502

            recording = program.get("Recording") or {}
            status_name = str(recording.get("StatusName", ""))
            end_time = program.get("EndTime") or recording.get("EndTs")
            in_progress = status_name.casefold() == "recording"
            comm_breaks = []
            if in_progress:
                markers = requests.get(
                    f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCommBreak",
                    params={
                        "RecordedId": recorded_id,
                        "OffsetType": "Duration",
                        "IncludeFps": "false",
                    },
                    headers={"Accept": "application/json"},
                    timeout=app.config["MYTHTV_TIMEOUT"],
                )
                markers.raise_for_status()
                comm_breaks = (markers.json().get("CutList") or {}).get("Cuttings", [])
        except (requests.RequestException, ValueError, AttributeError) as error:
            app.logger.info("MythTV live playback status refresh failed: %s", error)
            return jsonify(error="Could not refresh MythTV recording status"), 502

        return jsonify(
            in_progress=in_progress,
            end_time=end_time,
            comm_breaks=comm_breaks,
        )

    @app.get("/api/playback/<int:recorded_id>/stream")
    def playback_stream(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        headers = {}
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]
        try:
            upstream = requests.get(
                _recording_url(app, recorded_id),
                headers=headers,
                stream=True,
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV recording stream failed: %s", error)
            return Response(status=404)

        def generate():
            try:
                yield from upstream.iter_content(chunk_size=64 * 1024)
            finally:
                upstream.close()

        response = Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            content_type=upstream.headers.get(
                "Content-Type", "application/octet-stream"
            ),
        )
        for header in (
            "Accept-Ranges",
            "Content-Length",
            "Content-Range",
            "Content-Disposition",
            "Last-Modified",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        return response

    @app.post("/api/playback/<int:recorded_id>/position")
    def save_playback_position(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        position = request.json.get("position") if request.is_json else None
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(position)
            or position < 0
        ):
            return jsonify(error="A non-negative playback position is required"), 400
        try:
            response = requests.post(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/SetLastPlayPos",
                params={
                    "RecordedId": recorded_id,
                    "ChanId": (program.get("Channel") or {}).get("ChanId", 0),
                    "StartTime": program.get("StartTime", ""),
                    "OffsetType": "Duration",
                    "Offset": round(position * 1000),
                },
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
        except requests.RequestException as error:
            app.logger.warning("MythTV playback position save failed: %s", error)
            return jsonify(error="Could not save playback position"), 502
        return jsonify(saved=True)

    @app.get("/api/playback/<int:recorded_id>/transcode")
    def playback_transcode(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return jsonify(error="FFmpeg is required to transcode this recording"), 501
        start = request.args.get("start", default=0.0, type=float)
        token = request.args.get("token", "")
        if (
            start is None
            or not math.isfinite(start)
            or start < 0
            or start > 7 * 24 * 60 * 60
        ):
            return jsonify(error="Invalid playback start position"), 400
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return jsonify(error="A valid transcode token is required"), 400
        transcode_dir = Path(app.config["MYTHTV_TRANSCODE_DIR"])
        transcode_dir.mkdir(parents=True, exist_ok=True)
        process_path = transcode_dir / f"{token}.pid"
        if process_path.exists():
            return jsonify(error="Transcode token is already active"), 409

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-i",
            _recording_url(app, recorded_id),
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            app.logger.warning("Could not start FFmpeg: %s", error)
            return jsonify(error="Could not start FFmpeg"), 502
        process_path.write_text(str(process.pid))

        def generate():
            try:
                while chunk := process.stdout.read1(64 * 1024):
                    yield chunk
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process_path.unlink(missing_ok=True)

        return Response(
            stream_with_context(generate()),
            content_type="video/mp4",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/playback/<int:recorded_id>/transcode/cancel")
    def cancel_playback_transcode(recorded_id):
        payload = request.get_json(silent=True) or {}
        token = payload.get("token", "")
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return jsonify(error="A valid transcode token is required"), 400
        process_path = Path(app.config["MYTHTV_TRANSCODE_DIR"]) / f"{token}.pid"
        try:
            process_id = int(process_path.read_text())
            os.kill(process_id, signal.SIGTERM)
        except FileNotFoundError:
            return jsonify(cancelled=False), 404
        except (OSError, ValueError):
            return jsonify(cancelled=False), 404
        return jsonify(cancelled=True)

    @app.get("/api/playback/<int:recorded_id>/subtitles/<int:stream_index>")
    def playback_subtitles(recorded_id, stream_index):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        subtitle_indexes = {
            track["index"] for track in _probe_recording(app, recorded_id)["subtitles"]
        }
        if stream_index not in subtitle_indexes:
            return jsonify(error="Subtitle track not found"), 404

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return jsonify(error="FFmpeg is required to extract subtitles"), 501
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    _recording_url(app, recorded_id),
                    "-map",
                    f"0:{stream_index}",
                    "-f",
                    "webvtt",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            app.logger.warning("Subtitle extraction failed: %s", error)
            return jsonify(error="Could not extract recording subtitles"), 502
        if result.returncode:
            app.logger.info("Subtitle extraction returned %s", result.returncode)
            return Response(status=404)
        return Response(result.stdout, content_type="text/vtt; charset=utf-8")

    return app
