"""Cooperative stop for a GUI-owned child. Never terminate an in-flight order."""
import asyncio
import os
from pathlib import Path
import time

STOP_FILE_ENV = "KIWOOM_DESKTOP_STOP_FILE"


def stop_requested() -> bool:
    value = os.environ.get(STOP_FILE_ENV)
    return bool(value and Path(value).is_file())


async def sleep_or_stop(seconds: float) -> bool:
    if not os.environ.get(STOP_FILE_ENV):
        await asyncio.sleep(seconds)
        return False
    deadline = time.monotonic() + max(0, seconds)
    while not stop_requested():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.25, remaining))
    return True


def cleanup_stop_record():
    value = os.environ.get(STOP_FILE_ENV)
    if value:
        path = Path(value)
        try:
            path.with_suffix('.owner').unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        except OSError:
            pass
