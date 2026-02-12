"""Shared health state — avoids circular imports between bot.py and poller.py."""

import time

last_poll_time: float = time.time()
error_count: int = 0
consecutive_errors: int = 0


def report_poll_success():
    global last_poll_time, consecutive_errors
    last_poll_time = time.time()
    consecutive_errors = 0


def report_poll_error():
    global error_count, consecutive_errors
    error_count += 1
    consecutive_errors += 1
