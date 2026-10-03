import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from scripts.daily_gunicorn import DailyLogWriter, seconds_until_next_midnight


class DailyLogWriterTests(unittest.TestCase):
    def test_midnight_delay_respects_local_daylight_saving_transition(self):
        original_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Australia/Sydney"
            time.tzset()
            delay = seconds_until_next_midnight(datetime(2026, 10, 4, 1, 30))
            self.assertEqual(delay, 21.5 * 60 * 60)
        finally:
            if original_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_timezone
            time.tzset()

    def test_uses_local_midnight_start_in_filename_and_rotates(self):
        current_time = [datetime(2026, 10, 3, 23, 59, 59)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            writer = DailyLogWriter(directory, clock=lambda: current_time[0])
            writer.write(b"before midnight\n")

            current_time[0] = datetime(2026, 10, 4, 0, 0, 0)
            writer.write(b"after midnight\n")
            writer.close()

            first_log = directory / "gunicorn-2026-10-03T000000.log"
            second_log = directory / "gunicorn-2026-10-04T000000.log"
            self.assertEqual(first_log.read_bytes(), b"before midnight\n")
            self.assertEqual(second_log.read_bytes(), b"after midnight\n")

    def test_keeps_ten_period_files_including_current(self):
        current_time = [datetime(2026, 10, 12, 8, 0, 0)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for day in range(1, 12):
                (directory / f"gunicorn-2026-10-{day:02d}T000000.log").touch()

            writer = DailyLogWriter(directory, keep=10, clock=lambda: current_time[0])
            writer.close()

            log_files = sorted(directory.glob("gunicorn-*.log"))
            self.assertEqual(len(log_files), 10)
            self.assertEqual(log_files[0].name, "gunicorn-2026-10-03T000000.log")
            self.assertEqual(log_files[-1].name, "gunicorn-2026-10-12T000000.log")

    def test_migrates_existing_fixed_log_into_current_period(self):
        current_time = datetime(2026, 10, 3, 12, 0, 0)
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            legacy_log = directory / "gunicorn.log"
            legacy_log.write_bytes(b"existing output\n")

            writer = DailyLogWriter(directory, clock=lambda: current_time)
            writer.close()

            migrated_log = directory / "gunicorn-2026-10-03T000000.log"
            self.assertEqual(migrated_log.read_bytes(), b"existing output\n")
            self.assertFalse(legacy_log.exists())


if __name__ == "__main__":
    unittest.main()
