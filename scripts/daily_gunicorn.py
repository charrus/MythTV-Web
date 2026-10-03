#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

LOG_PATTERN = re.compile(r"mythtv-web-\d{4}-\d{2}-\d{2}T000000\.log")
LEGACY_LOG_PATTERN = re.compile(r"gunicorn-(\d{4}-\d{2}-\d{2}T000000)\.log")


def seconds_until_next_midnight(now: datetime | None = None) -> float:
    current = now or datetime.now()
    next_day = current.date() + timedelta(days=1)
    next_midnight = time.mktime(
        (next_day.year, next_day.month, next_day.day, 0, 0, 0, 0, 0, -1)
    )
    current_timestamp = (
        time.mktime(current.timetuple()) + current.microsecond / 1_000_000
    )
    return max(0.0, next_midnight - current_timestamp)


class DailyLogWriter:
    def __init__(
        self,
        directory: Path,
        keep: int = 10,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be at least one")
        self.directory = directory
        self.keep = keep
        self.clock = clock
        self.current_path: Path | None = None
        self.stream = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_log()
        self.ensure_period()

    @staticmethod
    def _path_for(moment: datetime, directory: Path) -> Path:
        period_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return directory / f"mythtv-web-{period_start:%Y-%m-%dT%H%M%S}.log"

    def _migrate_legacy_log(self) -> None:
        for legacy_path in sorted(self.directory.glob("gunicorn-*.log")):
            match = LEGACY_LOG_PATTERN.fullmatch(legacy_path.name)
            if match is None:
                continue
            target_path = self.directory / f"mythtv-web-{match.group(1)}.log"
            self._move_or_append_log(legacy_path, target_path)

        legacy_path = self.directory / "gunicorn.log"
        if not legacy_path.exists():
            return

        target_path = self._path_for(self.clock(), self.directory)
        self._move_or_append_log(legacy_path, target_path)

    @staticmethod
    def _move_or_append_log(legacy_path: Path, target_path: Path) -> None:
        if target_path.exists():
            with target_path.open("ab") as target, legacy_path.open("rb") as legacy:
                target.write(legacy.read())
        else:
            legacy_path.replace(target_path)
        if legacy_path.exists():
            legacy_path.unlink()

    def ensure_period(self) -> None:
        target_path = self._path_for(self.clock(), self.directory)
        if target_path == self.current_path:
            return

        if self.stream is not None:
            self.stream.close()
        self.current_path = target_path
        self.stream = target_path.open("ab", buffering=0)
        self._prune_old_logs()

    def _prune_old_logs(self) -> None:
        log_files = sorted(
            path
            for path in self.directory.glob("mythtv-web-*.log")
            if LOG_PATTERN.fullmatch(path.name)
        )
        for old_path in log_files[: -self.keep]:
            old_path.unlink()

    def write(self, contents: bytes) -> None:
        self.ensure_period()
        if self.stream is not None:
            self.stream.write(contents)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def run_gunicorn(project_dir: Path, pid_file: Path, writer: DailyLogWriter) -> int:
    python_gunicorn = project_dir / ".venv" / "bin" / "gunicorn"
    app_target = "mythtv_web:create_app()"
    command = [
        str(python_gunicorn),
        "--workers",
        "2",
        "--bind",
        "0.0.0.0:5000",
        "--pid",
        str(pid_file),
        "--access-logfile",
        "-",
        "--error-logfile",
        "-",
        app_target,
    ]
    process = subprocess.Popen(
        command,
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    def forward_signal(signal_number: int, _frame: object) -> None:
        if process.poll() is None:
            process.send_signal(signal_number)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    if process.stdout is None:
        raise RuntimeError("Gunicorn output pipe was not created")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output_open = True
    try:
        while output_open or process.poll() is None:
            writer.ensure_period()
            events = selector.select(timeout=seconds_until_next_midnight())
            for key, _ in events:
                contents = os.read(key.fileobj.fileno(), 65536)
                if contents:
                    writer.write(contents)
                else:
                    selector.unregister(key.fileobj)
                    output_open = False
        return process.wait()
    finally:
        selector.close()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> int:
    project_dir = Path(__file__).resolve().parents[1]
    run_dir = project_dir / ".run"
    writer = DailyLogWriter(run_dir)
    try:
        return run_gunicorn(project_dir, run_dir / "gunicorn.pid", writer)
    except Exception:
        writer.write(traceback.format_exc().encode())
        return 1
    finally:
        writer.close()


if __name__ == "__main__":
    sys.exit(main())
