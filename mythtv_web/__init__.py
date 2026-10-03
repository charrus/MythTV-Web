import fcntl
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
from flask import Flask, Response, jsonify, render_template, request

DEFAULT_BACKEND_URL = "http://127.0.0.1:6544"
RECORDINGS_PAGE_SIZE = 5000
RECORDINGS_CACHE_SECONDS = 120


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
    app.config["MYTHTV_BACKEND_URL"] = os.environ.get(
        "MYTHTV_BACKEND_URL", DEFAULT_BACKEND_URL
    ).rstrip("/")
    app.config["MYTHTV_TIMEOUT"] = 8
    app.config["MYTHTV_GROUPS_TIMEOUT"] = 30
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(
        Path(__file__).resolve().parents[1] / ".run" / "recordings-cache.json"
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

        groups = {}
        for program in programs:
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
                }
                groups[title] = group

            group["recording_count"] += 1
            if "WATCHED" in (program.get("ProgramFlagNames") or "").split("|"):
                group["watched_count"] += 1
            if latest_start > group["latest_start"]:
                group["latest_start"] = latest_start

        sorted_groups = sorted(
            groups.values(), key=lambda group: group["title"].casefold()
        )
        return jsonify(groups=sorted_groups, total=len(programs))

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
            if (program.get("Title") or "Untitled recording") == title
        ]
        if not matches:
            return jsonify(error="Program group not found"), 404
        matches.sort(key=lambda program: program.get("StartTime") or "")
        return jsonify(title=title, programs=matches, total=len(matches))

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

    return app
