import os
from urllib.parse import parse_qsl, urlsplit

import requests
from flask import Flask, Response, jsonify, render_template, request

DEFAULT_BACKEND_URL = "http://127.0.0.1:6544"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MYTHTV_BACKEND_URL"] = os.environ.get(
        "MYTHTV_BACKEND_URL", DEFAULT_BACKEND_URL
    ).rstrip("/")
    app.config["MYTHTV_TIMEOUT"] = 8

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
