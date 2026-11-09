"""Чтение и запись текстовых файлов с сохранением кодировки и переводов строк."""
from __future__ import annotations

import difflib
import os
from typing import List, NamedTuple

MAX_FILE_BYTES = 2 * 1024 * 1024
BINARY_PROBE_BYTES = 4096
UTF8_BOM = b"\xef\xbb\xbf"
DIFF_LINE_LIMIT = 60


class ToolError(Exception):
    """Ошибка инструмента: её текст уходит модели, чтобы она исправилась."""


class TextFile(NamedTuple):
    text: str        # содержимое, переводы строк приведены к \n
    encoding: str    # в какой кодировке файл лежит на диске
    crlf: bool       # были ли в файле переводы строк Windows


def read_text(path: str) -> TextFile:
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ToolError("File is larger than 2 MB; use search or run_command to inspect it.")
    with open(path, "rb") as handle:
        raw = handle.read()
    if b"\0" in raw[:BINARY_PROBE_BYTES]:
        raise ToolError("This is a binary file, it cannot be read as text.")
    if raw.startswith(UTF8_BOM):
        encoding, text = "utf-8-sig", raw[len(UTF8_BOM):].decode("utf-8", "replace")
    else:
        try:
            encoding, text = "utf-8", raw.decode("utf-8")
        except UnicodeDecodeError:
            # Старые учебные файлы на русском обычно в Windows-1251.
            encoding, text = "cp1251", raw.decode("cp1251", "replace")
    return TextFile(text.replace("\r\n", "\n"), encoding, b"\r\n" in raw)


def write_text(path: str, text: str, encoding: str = "utf-8", crlf: bool = False) -> None:
    body = text.replace("\r\n", "\n")
    if crlf:
        body = body.replace("\n", "\r\n")
    try:
        data = body.encode(encoding)
    except UnicodeEncodeError:
        data = body.encode("utf-8", "replace")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def make_diff(old: str, new: str, name: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), name, name, lineterm="", n=2))
    return _limit(lines)


def preview(text: str) -> str:
    return _limit(["+" + line for line in text.splitlines()])


def _limit(lines: List[str]) -> str:
    if len(lines) <= DIFF_LINE_LIMIT:
        return "\n".join(lines)
    hidden = len(lines) - DIFF_LINE_LIMIT
    return "\n".join(lines[:DIFF_LINE_LIMIT] + ["... (ещё {} строк)".format(hidden)])
