import requests

from mythtv_web import create_app


def test_home_renders_recordings_library():
    response = create_app().test_client().get("/")

    assert response.status_code == 200
    assert b"Recordings" in response.data
    assert b"library.js" in response.data


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
            "SubTitle": "Episode two",
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
            "SubTitle": "Episode one",
            "StartTime": "2026-10-02T10:00:00Z",
            "ProgramFlagNames": "NONE",
            "Channel": {"ChannelName": "Channel 1"},
        },
        {
            "Title": "One-off film",
            "StartTime": "2026-10-01T10:00:00Z",
            "ProgramFlagNames": "WATCHED",
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
    assert groups_response.json["total"] == 3
    assert [group["title"] for group in groups_response.json["groups"]] == [
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
    assert detail_response.status_code == 200
    assert [program["SubTitle"] for program in detail_response.json["programs"]] == [
        "Episode one",
        "Episode two",
    ]
    assert len(requests_made) == 1
    assert requests_made[0][1]["params"]["Count"] == 5000


def test_program_group_requires_a_title():
    response = create_app().test_client().get("/api/program-group")

    assert response.status_code == 400
    assert response.json == {"error": "A program title is required"}


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
