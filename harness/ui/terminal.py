from __future__ import annotations

import sys
import threading
from typing import TextIO


class TerminalStatusLine:
    _lock = threading.RLock()
    _active = False

    @classmethod
    def write_line(cls, line: str, stream: TextIO | None = None) -> None:
        stream = stream or sys.stdout
        with cls._lock:
            cls.clear(stream)
            stream.write(f"{line}\n")
            stream.flush()

    @classmethod
    def write_status(cls, line: str, stream: TextIO | None = None) -> None:
        stream = stream or sys.stdout
        with cls._lock:
            stream.write(f"\r\x1b[2K{line}")
            stream.flush()
            cls._active = True

    @classmethod
    def write_live(cls, text: str, stream: TextIO) -> None:
        with cls._lock:
            if cls._active and _is_tty(stream):
                stream.write("\r\x1b[2K")
                cls._active = False
            stream.write(text)
            stream.flush()

    @classmethod
    def clear(cls, stream: TextIO | None = None) -> None:
        stream = stream or sys.stdout
        if cls._active:
            stream.write("\r\x1b[2K")
            cls._active = False


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())
