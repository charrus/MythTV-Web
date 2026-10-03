import json
import signal
import subprocess
import time

import requests

from mythtv_web import create_app


def test_home_renders_recordings_library():
    response = create_app().test_client().get("/")

    assert response.status_code == 200
    assert b"Recordings" in response.data
    assert b"library.js" in response.data


def test_home_uses_forwarded_prefix_for_assets_and_navigation():
    response = (
        create_app()
        .test_client()
        .get("/", headers={"X-Forwarded-Prefix": "/mythtv-web"})
    )

    assert response.status_code == 200
    assert b'content="/mythtv-web"' in response.data
    assert b'href="/mythtv-web/static/library.css"' in response.data
    assert b'href="/mythtv-web/"' in response.data


def test_program_list_is_proxied_with_pagination(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": [{"Title": "Example show"}],
                    "StartIndex": 10,
                    "Count": 1,
                    "TotalAvailable": 25,
                    "AsOf": "2026-10-03T02:00:00Z",
                }
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    response = app.test_client().get("/api/programs?start=10&count=1")

    assert response.status_code == 200
    assert response.json == {
        "programs": [{"Title": "Example show"}],
        "start": 10,
        "count": 1,
        "total": 25,
        "as_of": "2026-10-03T02:00:00Z",
    }
    assert captured["url"] == "http://backend:6544/Dvr/GetRecordedList"
    assert captured["params"] == {"StartIndex": 10, "Count": 1, "Descending": "true"}
    assert captured["headers"] == {"Accept": "application/json"}


def test_program_list_reports_unavailable_backend(monkeypatch):
    def failed_get(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("mythtv_web.requests.get", failed_get)
    response = create_app().test_client().get("/api/programs")

    assert response.status_code == 502
    assert response.json == {"error": "MythTV backend is unavailable"}


def test_program_groups_aggregate_titles_and_reuse_full_list_cache(
    monkeypatch, tmp_path
):
    requests_made = []
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecGroup": "Seasons"},
            "SubTitle": "Episode two",
            "Season": 3,
            "Episode": 7,
            "Description": "A detailed episode description.",
            "StartTime": "2026-10-03T10:00:00Z",
            "ProgramFlagNames": "WATCHED",
            "Channel": {"ChannelName": "Channel 1"},
            "Artwork": {
                "ArtworkInfos": [
                    {
                        "Type": "coverart",
                        "URL": "/Content/GetImageFile?StorageGroup=Coverart&FileName=%2Fcover.jpg",
                    }
                ]
            },
        },
        {
            "Title": "Example series",
            "Recording": {"RecGroup": "Seasons"},
            "SubTitle": "Episode one",
            "StartTime": "2026-10-02T10:00:00Z",
            "ProgramFlagNames": "NONE",
            "Channel": {"ChannelName": "Channel 1"},
        },
        {
            "Title": "One-off film",
            "Recording": {"RecGroup": "Seasons", "RecordedId": 42},
            "StartTime": "2026-10-01T10:00:00Z",
            "ProgramFlagNames": "WATCHED",
        },
        {
            "Title": "8 Out Of 10 Cats Does Countdown",
            "Recording": {"RecGroup": "Default"},
            "SubTitle": "A numbered-title recording",
        },
        {
            "Title": "Deleted series",
            "Recording": {"RecGroup": "Deleted"},
        },
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": programs,
                    "Count": len(programs),
                    "TotalAvailable": len(programs),
                }
            }

    def fake_get(url, **kwargs):
        requests_made.append((url, kwargs))
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(tmp_path / "recordings.json")
    client = app.test_client()

    groups_response = client.get("/api/program-groups")
    detail_response = client.get("/api/program-group?title=Example%20series")

    assert groups_response.status_code == 200
    assert groups_response.json["total"] == 4
    assert [group["title"] for group in groups_response.json["groups"]] == [
        "8 Out Of 10 Cats Does Countdown",
        "Example series",
        "One-off film",
    ]
    series_group = next(
        group
        for group in groups_response.json["groups"]
        if group["title"] == "Example series"
    )
    assert series_group["recording_count"] == 2
    assert series_group["watched_count"] == 1
    assert series_group["artwork_url"].startswith("/Content/GetImageFile")
    film_group = next(
        group
        for group in groups_response.json["groups"]
        if group["title"] == "One-off film"
    )
    assert film_group["artwork_url"] is None
    assert film_group["preview_url"] == "/api/preview?recorded_id=42"
    assert (
        next(
            group
            for group in groups_response.json["groups"]
            if group["title"] == "8 Out Of 10 Cats Does Countdown"
        )["recording_count"]
        == 1
    )
    assert detail_response.status_code == 200
    assert [program["SubTitle"] for program in detail_response.json["programs"]] == [
        "Episode one",
        "Episode two",
    ]
    assert detail_response.json["programs"][1]["Season"] == 3
    assert detail_response.json["programs"][1]["Episode"] == 7
    assert detail_response.json["programs"][1]["Description"] == (
        "A detailed episode description."
    )
    assert len(requests_made) == 1
    assert requests_made[0][1]["params"]["Count"] == 5000


def test_program_group_requires_a_title():
    response = create_app().test_client().get("/api/program-group")

    assert response.status_code == 400
    assert response.json == {"error": "A program title is required"}


def test_program_groups_sort_naturally_and_ignore_leading_articles(
    monkeypatch, tmp_path
):
    titles = ["The 10 Ball", "An Banana", "A 2 Ball", "The Apple"]
    programs = [
        {"Title": title, "Recording": {"RecGroup": "Seasons"}} for title in titles
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": programs,
                    "Count": len(programs),
                    "TotalAvailable": len(programs),
                }
            }

    monkeypatch.setattr("mythtv_web.requests.get", lambda *args, **kwargs: Response())
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(tmp_path / "recordings.json")

    response = app.test_client().get("/api/program-groups")

    assert [group["title"] for group in response.json["groups"]] == [
        "A 2 Ball",
        "The 10 Ball",
        "The Apple",
        "An Banana",
    ]


def test_preview_proxy_uses_mythtv_recording_preview_endpoint(monkeypatch):
    captured = {}

    class Response:
        content = b"preview image"
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"

    response = app.test_client().get("/api/preview?recorded_id=935")

    assert response.status_code == 200
    assert response.data == b"preview image"
    assert response.content_type == "image/jpeg"
    assert captured["url"] == "http://backend:6544/Content/GetPreviewImage"
    assert captured["params"] == {"RecordedId": 935, "Width": 640, "Format": "JPG"}


def test_preview_proxy_rejects_invalid_recording_id(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("backend should not be called for invalid recording IDs")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)

    response = create_app().test_client().get("/api/preview?recorded_id=-1")

    assert response.status_code == 400
    assert response.json == {"error": "A valid recording ID is required"}


def test_playback_info_returns_mythtv_markers_and_resume_positions(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]
    requests_made = []
    monkeypatch.setattr(
        "mythtv_web.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            '{"streams": [{"index": 3, "codec_type": "subtitle", '
            '"codec_name": "subrip", "tags": {"language": "eng"}}]}',
            "",
        ),
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        requests_made.append((url, kwargs))
        if url.endswith("GetRecordedCommBreak"):
            return Response({"CutList": {"Cuttings": [{"Start": 10000, "End": 30000}]}})
        if url.endswith("GetRecordedCutList"):
            return Response({"CutList": {"Cuttings": []}})
        if url.endswith("GetLastPlayPos"):
            return Response({"long": "45000"})
        if url.endswith("GetSavedBookmark"):
            return Response({"long": "60000"})
        raise AssertionError(f"Unexpected MythTV request: {url}")

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().get("/api/playback/42")

    assert response.status_code == 200
    assert response.json["comm_breaks"] == [{"Start": 10000, "End": 30000}]
    assert response.json["cut_list"] == []
    assert response.json["last_position"] == 45000
    assert response.json["bookmark"] == 60000
    assert response.json["media"]["subtitles"] == [
        {"index": 3, "language": "eng", "title": ""}
    ]
    assert len(requests_made) == 4
    assert all(call[1]["params"]["ChanId"] == 123 for call in requests_made)


def test_recording_stream_forwards_byte_range(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    captured = {}

    class Response:
        status_code = 206
        headers = {
            "Content-Type": "video/x-matroska",
            "Content-Range": "bytes 0-2/100",
            "Content-Length": "3",
            "Accept-Ranges": "bytes",
        }

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"abc"

        def close(self):
            captured["closed"] = True

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().get(
        "/api/playback/42/stream", headers={"Range": "bytes=0-2"}
    )

    assert response.status_code == 206
    assert response.data == b"abc"
    assert response.headers["Content-Range"] == "bytes 0-2/100"
    assert captured["headers"] == {"Range": "bytes=0-2"}
    assert captured["url"] == "http://127.0.0.1:6544/Content/GetRecording?RecordedId=42"
    assert captured["closed"]


def test_playback_position_saves_duration_milliseconds(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().post(
        "/api/playback/42/position", json={"position": 45.5}
    )

    assert response.status_code == 200
    assert response.json == {"saved": True}
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/SetLastPlayPos"
    assert captured["params"]["Offset"] == 45500
    assert captured["params"]["OffsetType"] == "Duration"


def test_transcode_cancel_terminates_only_a_tokenized_process(monkeypatch, tmp_path):
    token = "a" * 32
    transcode_dir = tmp_path / "transcodes"
    transcode_dir.mkdir()
    (transcode_dir / f"{token}.pid").write_text("4321")
    captured = {}

    def fake_kill(process_id, process_signal):
        captured["process_id"] = process_id
        captured["signal"] = process_signal

    monkeypatch.setattr("mythtv_web.os.kill", fake_kill)
    app = create_app()
    app.config["MYTHTV_TRANSCODE_DIR"] = str(transcode_dir)
    client = app.test_client()

    response = client.post("/api/playback/42/transcode/cancel", json={"token": token})
    invalid_response = client.post(
        "/api/playback/42/transcode/cancel", json={"token": "../../etc/passwd"}
    )

    assert response.status_code == 200
    assert response.json == {"cancelled": True}
    assert captured == {"process_id": 4321, "signal": signal.SIGTERM}
    assert invalid_response.status_code == 400


def test_transcode_stream_removes_process_registration_after_completion(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    token = "b" * 32

    class Stdout:
        chunks = [b"fragment", b""]

        def read1(self, _size):
            return self.chunks.pop(0)

    class Process:
        pid = 9876
        stdout = Stdout()

        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = Process()
    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("mythtv_web.subprocess.Popen", lambda *args, **kwargs: process)
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)
    app.config["MYTHTV_TRANSCODE_DIR"] = str(tmp_path / "transcodes")

    response = app.test_client().get(
        f"/api/playback/42/transcode?start=0&token={token}", buffered=False
    )
    process_path = tmp_path / "transcodes" / f"{token}.pid"

    assert response.status_code == 200
    assert process_path.read_text() == "9876"
    assert b"".join(response.response) == b"fragment"
    assert process.terminated
    assert not process_path.exists()


def test_subtitle_endpoint_extracts_only_known_subtitle_tracks(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "mythtv_web._probe_recording",
        lambda app, recorded_id: {
            "video_codec": "hevc",
            "audio_codec": "eac3",
            "subtitles": [{"index": 3, "language": "eng", "title": "English"}],
        },
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, b"WEBVTT\n\n", b"")

    monkeypatch.setattr("mythtv_web.subprocess.run", fake_run)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)
    client = app.test_client()

    response = client.get("/api/playback/42/subtitles/3")
    invalid_response = client.get("/api/playback/42/subtitles/7")

    assert response.status_code == 200
    assert response.content_type.startswith("text/vtt")
    assert response.data == b"WEBVTT\n\n"
    assert captured["command"][captured["command"].index("-map") + 1] == "0:3"
    assert invalid_response.status_code == 404


def test_artwork_proxy_forwards_only_mythtv_image_parameters(monkeypatch):
    captured = {}

    class Response:
        content = b"image bytes"
        headers = {"Content-Type": "image/webp"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    image_url = "/Content/GetImageFile?StorageGroup=Coverart&FileName=%2Fcover.jpg"
    response = app.test_client().get(
        f"/api/artwork?url={image_url.replace('&', '%26')}"
    )

    assert response.status_code == 200
    assert response.data == b"image bytes"
    assert response.content_type == "image/webp"
    assert response.headers["Cache-Control"] == "public, max-age=3600"
    assert captured["url"] == "http://backend:6544/Content/GetImageFile"
    assert captured["params"] == {"StorageGroup": "Coverart", "FileName": "/cover.jpg"}


def test_artwork_proxy_rejects_arbitrary_upstream_urls(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("backend should not be called for invalid artwork URLs")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)
    response = (
        create_app()
        .test_client()
        .get("/api/artwork?url=https%3A%2F%2Fevil.invalid%2FContent%2FGetImageFile")
    )

    assert response.status_code == 400
    assert response.json == {"error": "Invalid artwork URL"}
