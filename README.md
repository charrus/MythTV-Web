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

Browser-side FFmpeg requires Node.js 20 or newer. Build its FFmpeg.wasm assets after checkout or dependency updates:

```sh
npm ci
npm run build:browser
```

## Recordings library

The home page groups recordings by program title, like MythFrontend. Selecting a group shows all recordings in that group. Flask loads the full recorded-program list from MythTV's `Dvr/GetRecordedList` service and caches it under `.run/` for two minutes so every Gunicorn worker can serve complete groups consistently. The first group load can take several seconds for a large library. Flask also proxies cover art locally, so the browser does not connect to MythTV directly. The default backend URL is `http://127.0.0.1:6544`; set `MYTHTV_BACKEND_URL` if the local backend uses another address. Search and watched-state filters are available in both views.

## Browser playback

Recordings play through MythTV's `Content/GetRecording` service proxied by Flask, preserving byte-range requests for seeking. The player uses MythTV commercial-break markers for smart/advert skipping and stores its resume position through `Dvr/SetLastPlayPos`. Browser-supported recordings stream directly. For codecs the browser cannot decode (including DVB-T MPEG-2/MP2), FFmpeg.wasm reads the recording through a bounded shared-memory pipe and streams fragmented MP4 into MediaSource; it does not stage the full recording in memory. Compatible High Profile H.264 video is copied without re-encoding, while audio is converted to AAC. Other unsupported video formats are converted to H.264/AAC. Seeking within buffered media stays local. Seeking outside the buffered window restarts at that position through the server FFmpeg fallback. If browser isolation, WASM conversion, or MSE playback is unavailable, the player also falls back to server FFmpeg. Install `ffmpeg` on the server for fallback and subtitle extraction, and `ffprobe` for codec probing. DVB bitmap subtitle tracks may not be convertible to browser text subtitles. Commercial skipping is available only when MythTV has commercial-break markers for that recording.

The WASM pipe reads shared input in 64 KB batches and flushes output in 8 KB batches through a bounded 1 MB input ring and 4 MB output ring; MediaSource receives up to 256 KB per append and backpressure controls conversion. It requires a cross-origin-isolated page, so Flask sends `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp`; a reverse proxy must preserve these headers. The FFmpeg core is about 32 MB and is loaded only for recordings needing conversion. The server FFmpeg endpoint remains available as a fallback.

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
flask --app 'mythtv_web:create_app' run --host 127.0.0.1 --port 6555
```

For the Apache-facing server process:

```sh
gunicorn --workers 2 --worker-class gthread --threads 8 --timeout 120 --bind 127.0.0.1:6555 'mythtv_web:create_app()'
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

The launcher requires dependencies to be installed in `.venv/`. It only stops the Gunicorn process identified by its own PID file and refuses to signal a different process. Gunicorn access and error output is written to `.run/mythtv-web-YYYY-MM-DDT000000.log`, where the timestamp is the start of that log day in the server's local timezone. Rotation happens at local midnight; at most 10 dated log files are retained, including the active file. Existing logs with the previous `gunicorn-` prefix are migrated when the service starts.

## Apache reverse proxy

The example Apache snippet is [`config/apache/mythtv-web.conf`](config/apache/mythtv-web.conf). It serves the app at `https://mythtv.example.com/mythtv-web/`, redirects HTTP to HTTPS, and requires Apache Basic Auth. Replace `mythtv.example.com` in the snippet with the canonical DNS name covered by your HTTPS certificate. The Flask/Gunicorn service binds to loopback on port 6555; Apache forwards the `/mythtv-web/` prefix so the page's static files and API requests use the same path.

On Debian or Ubuntu, install the snippet and create the password file as root:

```sh
sudo a2enmod access_compat proxy proxy_http auth_basic headers rewrite
sudo install -o root -g root -m 644 config/apache/mythtv-web.conf /etc/apache2/conf-available/mythtv-web.conf
sudo htpasswd -cB /etc/apache2/mythtv-web.htpasswd mythtv-user
sudo chown root:www-data /etc/apache2/mythtv-web.htpasswd
sudo chmod 640 /etc/apache2/mythtv-web.htpasswd
```

`htpasswd` prompts for the password without putting it in shell history. The `-c` option creates a new file and must only be used for the first account; add later accounts with `sudo htpasswd -B /etc/apache2/mythtv-web.htpasswd another-user` so existing accounts are retained.

Include the snippet inside both the HTTP and HTTPS `<VirtualHost>` blocks for your chosen hostname in the Apache site configuration:

```apache
Include /etc/apache2/conf-available/mythtv-web.conf
```

The Basic Auth rule is scoped to the backend proxy target and sets `Satisfy all` to prevent a broad legacy proxy allowlist from bypassing the login. Apache overwrites `X-Authenticated-User` with the authenticated account name; Flask requires this proxy-supplied identity and an explicit confirmation for recording deletion. The deletion request uses MythTV's non-forced delete options. The example assumes Apache and Flask run on the same host. Keep the Flask listener on `127.0.0.1:6555`; Apache should be the public entry point.

Start or restart the Flask service, then validate and reload Apache:

```sh
./scripts/mythtv-web.sh restart
sudo apache2ctl -t
sudo systemctl reload apache2
```

An unauthenticated request should return `401`; authenticated requests should load the homepage and its `/mythtv-web/static/` assets. Use HTTPS whenever credentials cross a network.
