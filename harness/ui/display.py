from __future__ import annotations

import unicodedata


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if char in {"\n", "\r"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    if max_width <= 3:
        return "." * max_width
    current_width = 0
    chars: list[str] = []
    for char in text:
        char_width = display_width(char)
        if current_width + char_width > max_width - 3:
            return "".join(chars) + "..."
        chars.append(char)
        current_width += char_width
    return "".join(chars)


def pad_display(text: str, width: int) -> str:
    clipped = truncate_display(text, width)
    return clipped + (" " * max(0, width - display_width(clipped)))
