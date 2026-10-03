# MythTV Web Server

A Flask application intended to provide a web interface to the MythTV services on this host. Apache is expected to handle external access and authentication, then reverse-proxy requests to this application.

## Setup

From the project root, create and activate the local virtual environment, then install dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements.txt` contains runtime dependencies; `requirements-dev.txt` adds pytest, Ruff, and Black.

## Recordings library

The home page displays the recorded-program library from MythTV's `Dvr/GetRecordedList` service. Flask proxies the API and cover art locally, so the browser does not connect to the MythTV backend directly. The default backend URL is `http://127.0.0.1:6544`; set `MYTHTV_BACKEND_URL` if the local backend uses another address. The page supports title/channel search over loaded results, watched-state filters, and pagination.

## Quality checks

Run these checks before committing Python changes:

```sh
black .
ruff check .
pytest
```

## Run

For local development:

```sh
flask --app 'mythtv_web:create_app' run --host 0.0.0.0 --port 5000
```

For the Apache-facing server process:

```sh
gunicorn --workers 2 --bind 0.0.0.0:5000 'mythtv_web:create_app()'
```

The landing page is at `/`; `/healthz` returns a small JSON health response. Stop the process with Ctrl+C when running it in the foreground.

## Background service

Use the launcher to manage Gunicorn in the background. It records its PID and writes logs under the ignored `.run/` directory:

```sh
./scripts/mythtv-web.sh start
./scripts/mythtv-web.sh status
./scripts/mythtv-web.sh restart
./scripts/mythtv-web.sh stop
```

The launcher requires dependencies to be installed in `.venv/`. It only stops the Gunicorn process identified by its own PID file and refuses to signal a different process. Gunicorn access and error output is written to `.run/gunicorn-YYYY-MM-DDT000000.log`, where the timestamp is the start of that log day in the server's local timezone. Rotation happens at local midnight; at most 10 dated log files are retained, including the active file.

## Apache reverse proxy

Enable Apache's `proxy`, `proxy_http`, and `auth_basic` modules as needed. A virtual host can proxy to the service while requiring authentication, for example:

```apache
<Location />
    AuthType Basic
    AuthName "MythTV"
    AuthUserFile /etc/apache2/mythtv.htpasswd
    Require valid-user
</Location>

ProxyPreserveHost On
ProxyPass        / http://127.0.0.1:5000/
ProxyPassReverse / http://127.0.0.1:5000/
```

Use HTTPS at Apache when credentials cross an untrusted network. Although Gunicorn binds to `0.0.0.0` as requested, port 5000 must not be exposed to untrusted clients: restrict it with the host firewall or network policy so only Apache and trusted administration hosts can connect. Apache should be the only public entry point and should enforce authentication before proxying.
