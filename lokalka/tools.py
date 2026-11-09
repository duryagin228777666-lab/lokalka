"""Инструменты агента: файлы, поиск и команды внутри рабочей папки."""
from __future__ import annotations

import fnmatch
import math
import os
import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import shell
from .brain import Brain, brain_path
from .config import config_path
from .providers import INVALID_JSON_KEY
from .textio import ToolError, make_diff, preview, read_text, write_text

SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
                       ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "dist", "build"})
MAX_RESULT_CHARS = 16000
READ_LINE_LIMIT = 800
LIST_LIMIT = 300
FIND_LIMIT = 200
SEARCH_LIMIT = 100
SEARCH_LINE_CHARS = 300

# confirm(заголовок, подробности, спрашивать_всегда) -> (разрешено, комментарий пользователя)
Confirm = Callable[[str, str, bool], Tuple[bool, str]]


def _spec(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

TOOL_SPECS = [
    _spec("read_file", "Read a text file. Returns numbered lines. Use offset/limit for long files.",
          {"path": _STR, "offset": dict(_INT, description="First line to read, 1-based."),
           "limit": dict(_INT, description="How many lines to read (default 800).")}, ["path"]),
    _spec("write_file", "Create a file or fully overwrite an existing one with the given content. "
          "Parent folders are created automatically. For small changes prefer edit_file.",
          {"path": _STR, "content": _STR}, ["path", "content"]),
    _spec("edit_file", "Replace an exact fragment of a file. old_string must match the file text "
          "exactly (including indentation) and be unique unless replace_all is true.",
          {"path": _STR, "old_string": _STR, "new_string": _STR, "replace_all": _BOOL},
          ["path", "old_string", "new_string"]),
    _spec("list_dir", "List files and folders in a directory (default: the working folder).",
          {"path": _STR}, []),
    _spec("find_files", "Find files by glob pattern, e.g. '*.py' or 'src/**/test_*.py'.",
          {"pattern": _STR}, ["pattern"]),
    _spec("search", "Search file contents with a regular expression. Returns path:line: text.",
          {"pattern": _STR, "path": dict(_STR, description="File or folder to search (default: working folder)."),
           "glob": dict(_STR, description="Only files matching this glob, e.g. '*.py'."),
           "ignore_case": _BOOL}, ["pattern"]),
    _spec("run_command", "Run a shell command in the working folder and return its output and exit code. "
          "Use it to run and test code. The command cannot ask the user anything: "
          "pass everything the program reads from the keyboard in stdin.",
          {"command": _STR, "stdin": dict(_STR, description="Text fed to the program's standard input."),
           "timeout": dict(_INT, description="Seconds before the command is stopped (default 60).")},
          ["command"]),
    _spec("memory_save", "Save or update a note in your long-term memory graph (kept between sessions). "
          "Link it to related notes by title; missing linked notes are created as hubs.",
          {"title": _STR, "text": _STR,
           "links": {"type": "array", "items": _STR, "description": "Titles of related notes."},
           "tags": {"type": "array", "items": _STR}}, ["title", "text"]),
    _spec("memory_search", "Search your long-term memory graph by words. Returns matching notes and their links.",
          {"query": _STR}, ["query"]),
    _spec("memory_read", "Read one memory note by exact title, together with short versions of linked notes.",
          {"title": _STR}, ["title"]),
]


def _list_arg(args: Dict[str, Any], key: str) -> List[str]:
    value = args.get(key) or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ToolError("Argument '{}' must be a list of strings.".format(key))
    return [str(item) for item in value]


def _text_arg(args: Dict[str, Any], key: str, default: Optional[str] = None) -> str:
    value = args.get(key)
    if value is None:  # модели нередко присылают null вместо пропущенного аргумента
        value = default
    if value is None:
        raise ToolError("Missing required argument '{}'.".format(key))
    if not isinstance(value, str):
        raise ToolError("Argument '{}' must be a string.".format(key))
    return value


def _int_arg(args: Dict[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToolError("Argument '{}' must be a number.".format(key))
    return int(value)


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    head, tail = text[:MAX_RESULT_CHARS * 2 // 3], text[-MAX_RESULT_CHARS // 3:]
    return "{}\n... [{} characters omitted] ...\n{}".format(head, len(text) - len(head) - len(tail), tail)


class Workspace:
    """Рабочая папка и всё, что агенту разрешено в ней делать."""

    def __init__(self, root: str, confirm: Confirm, brain: Optional[Brain] = None) -> None:
        self.root = os.path.realpath(root)
        self._confirm = confirm
        self.brain = brain if brain is not None else Brain()
        self._undo = []  # type: List[Tuple[str, Optional[bytes]]]
        self._handlers = {"read_file": self._read_file, "write_file": self._write_file,
                          "edit_file": self._edit_file, "list_dir": self._list_dir,
                          "find_files": self._find_files, "search": self._search,
                          "run_command": self._run_command, "memory_save": self._memory_save,
                          "memory_search": self._memory_search, "memory_read": self._memory_read}

    # ------------------------------------------------------------ служебное

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Выполнить инструмент. Любая ошибка возвращается текстом — модель её увидит и исправится."""
        handler = self._handlers.get(name)
        try:
            if handler is None:
                raise ToolError("Unknown tool '{}'.".format(name))
            if INVALID_JSON_KEY in args:
                raise ToolError("Tool arguments were not valid JSON (the reply was probably cut off). "
                                "Retry; write big files in several smaller steps.")
            return _clip(handler(args))
        except ToolError as err:
            return "ERROR: {}".format(err)
        except OSError as err:
            return "ERROR: {}".format(err.strerror or err)
        except Exception as err:  # что бы ни прислала модель, диалог не должен падать
            return "ERROR: {}: {}".format(type(err).__name__, err)

    def resolve(self, path: str) -> str:
        return os.path.realpath(os.path.join(self.root, os.path.expanduser(path)))

    @staticmethod
    def _contains(folder: str, full_path: str) -> bool:
        base, target = os.path.normcase(folder), os.path.normcase(full_path)
        try:
            return os.path.commonpath([base, target]) == base
        except ValueError:  # разные диски
            return False

    def is_inside(self, full_path: str) -> bool:
        return self._contains(self.root, full_path)

    @staticmethod
    def is_secret(full_path: str) -> bool:
        """Файл настроек с API-ключами: модели он не нужен, даже если лежит в рабочей папке."""
        # Граф памяти тоже: правится только через memory_*, иначе модель может его испортить.
        guarded = [os.path.normcase(os.path.realpath(path)) for path in (config_path(), brain_path())]
        return os.path.normcase(full_path) in guarded + [path + ".tmp" for path in guarded]

    def relative(self, full_path: str) -> str:
        if not self.is_inside(full_path):
            return full_path
        return os.path.relpath(full_path, self.root).replace(os.sep, "/")

    def _approve(self, title: str, detail: str = "", always_ask: bool = False) -> None:
        approved, comment = self._confirm(title, detail, always_ask)
        if not approved:
            raise ToolError("The user declined this action." + (" They said: " + comment if comment else ""))

    def _reachable(self, path: str, action: str) -> str:
        """Полный путь. За пределы рабочей папки — только с явного разрешения."""
        full_path = self.resolve(path)
        if self.is_secret(full_path):
            self._approve("{} файла с API-ключами: {}".format(action, full_path), always_ask=True)
        elif not self.is_inside(full_path):
            self._approve("{} ВНЕ рабочей папки: {}".format(action, full_path), always_ask=True)
        return full_path

    def _save(self, full_path: str, text: str, encoding: str = "utf-8", crlf: bool = False) -> None:
        """Записать файл и запомнить прежнее содержимое для /undo — только если запись удалась."""
        previous = None  # type: Optional[bytes]
        if os.path.isfile(full_path):
            with open(full_path, "rb") as handle:
                previous = handle.read()
        write_text(full_path, text, encoding, crlf)
        self._undo.append((full_path, previous))

    def undo(self) -> str:
        """Откатить последнее изменение файла; возвращает описание для пользователя."""
        if not self._undo:
            return "Отменять нечего."
        full_path, previous = self._undo.pop()
        if previous is None:
            if os.path.exists(full_path):
                os.remove(full_path)
            return "Удалён созданный файл {}".format(self.relative(full_path))
        with open(full_path, "wb") as handle:
            handle.write(previous)
        return "Возвращена прежняя версия {}".format(self.relative(full_path))

    def _walk(self, start: str) -> Iterator[str]:
        """Файлы под start. Ссылки и соединения, ведущие наружу, не обходим: туда — только с разрешения."""
        if os.path.isfile(start):
            yield start
            return
        base = os.path.realpath(start)

        def allowed(path: str) -> bool:
            real = os.path.realpath(path)
            return self._contains(base, real) and not self.is_secret(real)

        for folder, dirs, files in os.walk(start):
            dirs[:] = sorted(name for name in dirs
                             if name not in SKIP_DIRS and allowed(os.path.join(folder, name)))
            for name in sorted(files):
                if allowed(os.path.join(folder, name)):
                    yield os.path.join(folder, name)

    # ------------------------------------------------------------ инструменты

    def _read_file(self, args: Dict[str, Any]) -> str:
        full_path = self._reachable(_text_arg(args, "path"), "Чтение")
        if not os.path.isfile(full_path):
            raise ToolError("File not found: {}".format(self.relative(full_path)))
        lines = read_text(full_path).text.split("\n")
        start = max(1, _int_arg(args, "offset", 1))
        count = max(1, _int_arg(args, "limit", READ_LINE_LIMIT))
        chunk = lines[start - 1:start - 1 + count]
        body = "\n".join("{}\t{}".format(start + index, line) for index, line in enumerate(chunk))
        shown_to = start + len(chunk) - 1
        if shown_to < len(lines):
            body += "\n[lines {}-{} of {}; pass offset to read further]".format(start, shown_to, len(lines))
        return body or "(empty file)"

    def _write_file(self, args: Dict[str, Any]) -> str:
        full_path = self._reachable(_text_arg(args, "path"), "Запись")
        content = _text_arg(args, "content")
        name = self.relative(full_path)
        if os.path.isdir(full_path):
            raise ToolError("{} is a folder.".format(name))
        if os.path.isfile(full_path):
            old = read_text(full_path)
            self._approve("Перезаписать файл {}".format(name), make_diff(old.text, content, name))
            self._save(full_path, content, old.encoding, old.crlf)
        else:
            self._approve("Создать файл {}".format(name), preview(content))
            self._save(full_path, content)
        return "Wrote {} ({} lines).".format(name, content.count("\n") + 1)

    def _edit_file(self, args: Dict[str, Any]) -> str:
        full_path = self._reachable(_text_arg(args, "path"), "Изменение")
        old_string, new_string = _text_arg(args, "old_string"), _text_arg(args, "new_string")
        name = self.relative(full_path)
        if not os.path.isfile(full_path):
            raise ToolError("File not found: {}. Use write_file to create it.".format(name))
        if not old_string:
            raise ToolError("old_string must not be empty.")
        current = read_text(full_path)
        old_string, new_string = old_string.replace("\r\n", "\n"), new_string.replace("\r\n", "\n")
        found = current.text.count(old_string)
        if found == 0:
            raise ToolError("old_string was not found in {}. Read the file again and copy the "
                            "fragment exactly, with its indentation.".format(name))
        replace_all = bool(args.get("replace_all"))
        if found > 1 and not replace_all:
            raise ToolError("old_string occurs {} times in {}. Add surrounding lines to make it "
                            "unique or pass replace_all=true.".format(found, name))
        updated = current.text.replace(old_string, new_string) if replace_all \
            else current.text.replace(old_string, new_string, 1)
        self._approve("Изменить файл {}".format(name), make_diff(current.text, updated, name))
        self._save(full_path, updated, current.encoding, current.crlf)
        return "Edited {} ({} replacement(s)).".format(name, found if replace_all else 1)

    def _list_dir(self, args: Dict[str, Any]) -> str:
        full_path = self._reachable(_text_arg(args, "path", "."), "Просмотр папки")
        if not os.path.isdir(full_path):
            raise ToolError("Folder not found: {}".format(self.relative(full_path)))
        entries = sorted(os.scandir(full_path), key=lambda entry: (not entry.is_dir(), entry.name.lower()))
        lines = [self._describe(entry) for entry in entries[:LIST_LIMIT]]
        if len(entries) > LIST_LIMIT:
            lines.append("... and {} more".format(len(entries) - LIST_LIMIT))
        return "\n".join(lines) or "(empty folder)"

    @staticmethod
    def _describe(entry: Any) -> str:
        if entry.is_dir():
            return "{}/".format(entry.name)
        try:
            return "{}  ({} bytes)".format(entry.name, entry.stat().st_size)
        except OSError:  # битая ссылка или занятый файл — не повод терять весь список
            return entry.name

    def _find_files(self, args: Dict[str, Any]) -> str:
        pattern = _text_arg(args, "pattern").replace("\\", "/")
        matches = []  # type: List[str]
        for full_path in self._walk(self.root):
            name = self.relative(full_path)
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(os.path.basename(name), pattern):
                matches.append(name)
                if len(matches) >= FIND_LIMIT:
                    matches.append("... (limit reached, narrow the pattern)")
                    break
        return "\n".join(matches) or "No files match."

    def _search(self, args: Dict[str, Any]) -> str:
        try:
            regex = re.compile(_text_arg(args, "pattern"), re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as err:
            raise ToolError("Bad regular expression: {}".format(err))
        start = self._reachable(_text_arg(args, "path", "."), "Поиск")
        if not os.path.exists(start):
            raise ToolError("Path not found: {}".format(self.relative(start)))
        glob = args.get("glob")
        hits = []  # type: List[str]
        for full_path in self._walk(start):
            if isinstance(glob, str) and glob and not fnmatch.fnmatch(os.path.basename(full_path), glob):
                continue
            try:
                lines = read_text(full_path).text.split("\n")
            except (ToolError, OSError):
                continue  # двоичные, огромные и недоступные файлы просто пропускаем
            for number, line in enumerate(lines, 1):
                if regex.search(line):
                    hits.append("{}:{}: {}".format(self.relative(full_path), number, line.strip()[:SEARCH_LINE_CHARS]))
                    if len(hits) >= SEARCH_LIMIT:
                        return "\n".join(hits + ["... (limit reached, narrow the search)"])
        return "\n".join(hits) or "No matches."

    def _memory_save(self, args: Dict[str, Any]) -> str:
        try:
            return self.brain.save(_text_arg(args, "title"), _text_arg(args, "text"),
                                   _list_arg(args, "links"), _list_arg(args, "tags"))
        except ValueError as err:
            raise ToolError(str(err))

    def _memory_search(self, args: Dict[str, Any]) -> str:
        return self.brain.render_search(_text_arg(args, "query"))

    def _memory_read(self, args: Dict[str, Any]) -> str:
        title = _text_arg(args, "title")
        found = self.brain.read(title)
        if found is None:
            raise ToolError("No note titled '{}'. Use memory_search.".format(title))
        return found

    def _run_command(self, args: Dict[str, Any]) -> str:
        command = _text_arg(args, "command").strip()
        if not command:
            raise ToolError("command must not be empty.")
        stdin_text = _text_arg(args, "stdin", "")
        timeout = _int_arg(args, "timeout", shell.DEFAULT_TIMEOUT)
        detail = "ввод программы:\n" + stdin_text if stdin_text else ""
        self._approve("Выполнить команду: {}".format(command), detail)
        result = shell.run(command, self.root, stdin_text, timeout)
        output = result.output.strip() or "(no output)"
        if result.timed_out:
            return "{}\n[stopped: no finish within {} s — is the program waiting for input?]".format(output, timeout)
        return "{}\n[exit code {}]".format(output, result.exit_code)
