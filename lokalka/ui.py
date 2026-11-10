"""Терминальный интерфейс: строка ввода, вывод ответа, вопросы пользователю."""
from __future__ import annotations

import getpass
import os
import shutil
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

RESET = "\033[0m"
ERASE_LINE = "\r\033[K"
# Мягкая палитра из 256 цветов: одинаково выглядит в PyCharm, cmd и обычных терминалах.
COLORS = {"dim": "\033[38;5;245m", "red": "\033[38;5;203m", "green": "\033[38;5;114m",
          "yellow": "\033[38;5;221m", "cyan": "\033[38;5;80m", "accent": "\033[38;5;141m",
          "title": "\033[1;38;5;141m", "bold": "\033[1m"}
# Значки есть в стандартных шрифтах Windows; запасной набор — для консолей без юникода.
UNICODE_GLYPHS = {"bar": "▌", "prompt": "›", "tool": "→", "gutter": "│", "answer": "●", "error": "×",
                  "spin": ("●∙∙", "∙●∙", "∙∙●", "∙●∙")}
ASCII_GLYPHS = {"bar": "|", "prompt": ">", "tool": "->", "gutter": "|", "answer": "*", "error": "x",
                "spin": ("*..", ".*.", "..*", ".*.")}
THINKING_LABEL = "думаю..."
SPIN_INTERVAL_SECONDS = 0.15
DEFAULT_WIDTH = 80
HELP_COMMAND_COLUMN = 17
ANSWER_INDENT = "  "
BOM = "﻿"
PASTE_SETTLE_SECONDS = 0.03
OUTPUT_PREVIEW_LINES = 20
SUMMARY_CHARS = 100
PASTE_END = "."
# Главный аргумент каждого инструмента — его и показываем в строке «что делает агент».
SUMMARY_KEYS = {"read_file": "path", "write_file": "path", "edit_file": "path", "list_dir": "path",
                "find_files": "pattern", "search": "pattern", "run_command": "command",
                "memory_save": "title", "memory_search": "query", "memory_read": "title"}


def clean(text: str) -> str:
    """Убрать управляющие символы: чужой текст не должен перерисовывать терминал."""
    return "".join(char if char in "\n\t" or not (ord(char) < 32 or 127 <= ord(char) < 160) else "?"
                   for char in text.replace("\r\n", "\n"))


def configure_stdio() -> None:
    """Символ, которого нет в кодировке консоли, не должен ронять программу."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _enable_windows_ansi() -> bool:
    try:
        import ctypes
        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("PYCHARM_HOSTED"):
        return True
    if not sys.stdout.isatty():
        return False
    return _enable_windows_ansi() if os.name == "nt" else True


def _input_pending() -> bool:
    """Есть ли ещё не прочитанный ввод — так распознаём вставку многострочного текста."""
    if not sys.stdin.isatty():
        return False
    time.sleep(PASTE_SETTLE_SECONDS)
    try:
        if os.name == "nt":
            import msvcrt
            return bool(msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (ImportError, OSError, ValueError):
        return False


def _pick_glyphs() -> Dict[str, Any]:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    sample = "".join("".join(value) for value in UNICODE_GLYPHS.values())
    try:
        sample.encode(encoding)
    except (UnicodeError, LookupError):
        return ASCII_GLYPHS
    return UNICODE_GLYPHS


class _Spinner:
    """Бегущий индикатор ожидания со счётчиком секунд; рисуется в отдельном потоке."""

    def __init__(self, frames: Tuple[str, ...], paint: Any) -> None:
        self._frames = frames
        self._paint = paint
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        started = time.time()
        step = 0
        while True:
            label = "{} {}с".format(THINKING_LABEL, int(time.time() - started))
            frame = self._frames[step % len(self._frames)]
            sys.stdout.write(ERASE_LINE + self._paint(frame, "accent") + " " + self._paint(label, "dim"))
            sys.stdout.flush()
            step += 1
            if self._stop.wait(SPIN_INTERVAL_SECONDS):
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        sys.stdout.write(ERASE_LINE)
        sys.stdout.flush()


class Console:
    def __init__(self, auto_approve: bool = False) -> None:
        self.auto_approve = auto_approve
        self._color = _supports_color()
        self._interactive = sys.stdout.isatty()
        self._glyphs = _pick_glyphs()
        self._waiting = False
        self._spinner = None  # type: Optional[_Spinner]
        self._answer_open = False
        self._indent_due = False
        self._last_piece = ""

    # ------------------------------------------------------------ вывод

    def out(self, text: str = "", end: str = "\n") -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()

    def paint(self, text: str, color: str) -> str:
        return COLORS[color] + text + RESET if self._color else text

    def notice(self, text: str) -> None:
        self.out(self.paint(clean(text), "yellow"))

    def error(self, text: str) -> None:
        self.out(self.paint("{} Ошибка: {}".format(self._glyphs["error"], clean(text)), "red"))

    def _gutter(self) -> str:
        return "  " + self.paint(self._glyphs["gutter"], "dim") + " "

    def banner(self, version: str, provider: str, model: str, root: str) -> None:
        """Шапка при запуске: название, подключение и рабочая папка."""
        bar = "  " + self.paint(self._glyphs["bar"], "accent") + " "
        room = max(shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns - 5, 20)
        if len(root) > room:
            root = "..." + root[-(room - 3):]
        self.out()
        self.out(bar + self.paint("Локалка", "title") + self.paint(" " + version, "dim"))
        self.out(bar + "{} · {}".format(clean(provider), clean(model))[:room])
        self.out(bar + self.paint(clean(root), "dim"))
        self.out()
        self.out("  " + self.paint("/help", "accent") + self.paint(" — команды  ·  ", "dim") +
                 self.paint("/exit", "accent") + self.paint(" — выход", "dim"))

    def help(self, text: str) -> None:
        """Справка: колонка с командами выделяется цветом."""
        for line in text.splitlines():
            if line.startswith("  "):
                line = self.paint(line[:HELP_COMMAND_COLUMN], "accent") + line[HELP_COMMAND_COLUMN:]
            self.out(line)

    def prompt(self) -> str:
        return "\n" + self.paint(self._glyphs["prompt"], "title") + " "

    def plain(self, text: str) -> None:
        """Вывести текст из внешнего источника (вывод команды и т.п.)."""
        self.out(clean(text))

    def stream_start(self) -> None:
        self._last_piece = ""
        self._answer_open = False
        self._indent_due = False
        self._waiting = self._interactive
        if self._waiting and self._color:
            self._spinner = _Spinner(self._glyphs["spin"], self.paint)
        elif self._waiting:
            self.out(THINKING_LABEL, end="")

    def _clear_waiting(self) -> None:
        if not self._waiting:
            return
        self._waiting = False
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        else:
            self.out("\r" + " " * len(THINKING_LABEL) + "\r", end="")

    def stream_text(self, piece: str) -> None:
        piece = clean(piece)
        if not piece:
            return
        self._clear_waiting()
        self._last_piece = piece
        if self._interactive:  # в терминале ответ идёт с отметкой и отступом; в файл — как есть
            if not self._answer_open:
                self._answer_open = True
                self.out(self.paint(self._glyphs["answer"], "accent") + " ", end="")
            # Отступ после последнего перевода строки откладываем до следующего куска текста.
            ends_line = piece.endswith("\n")
            body = (piece[:-1] if ends_line else piece).replace("\n", "\n" + ANSWER_INDENT)
            if self._indent_due and body:
                body = ANSWER_INDENT + body
            self._indent_due = ends_line
            piece = body + ("\n" if ends_line else "")
        self.out(piece, end="")

    def stream_end(self) -> None:
        self._clear_waiting()
        if self._last_piece and not self._last_piece.endswith("\n"):
            self.out()

    def tool_call(self, name: str, args: Dict[str, Any]) -> None:
        value = clean(str(args.get(SUMMARY_KEYS.get(name, ""), ""))).replace("\n", " ")
        if len(value) > SUMMARY_CHARS:
            value = value[:SUMMARY_CHARS] + "..."
        head = "  " + self.paint("{} {}".format(self._glyphs["tool"], clean(name)), "accent")
        self.out((head + " " + self.paint(value, "dim")) if value else head)

    def tool_result(self, result: str) -> None:
        lines = clean(result).splitlines() or [""]
        if result.startswith("ERROR:"):
            self.out(self._gutter() + self.paint(lines[0], "red"))
        elif lines[-1].startswith(("[exit code", "[stopped")):  # вывод запущенной команды
            shown = lines[:OUTPUT_PREVIEW_LINES]
            if len(lines) > OUTPUT_PREVIEW_LINES:
                shown.append("... (ещё {} строк)".format(len(lines) - OUTPUT_PREVIEW_LINES))
            for line in shown:
                self.out(self._gutter() + self.paint(line, "dim"))

    def show_diff(self, detail: str) -> None:
        for line in clean(detail).splitlines():
            color = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
            self.out(self._gutter() + self.paint(line, color))

    # ------------------------------------------------------------ ввод

    def ask(self, prompt: str) -> str:
        return input(prompt).strip()

    def ask_secret(self, prompt: str) -> str:
        if sys.stdin.isatty() and not os.environ.get("PYCHARM_HOSTED"):
            return getpass.getpass(prompt).strip()
        return input(prompt).strip()

    def read_input(self, prompt: str) -> str:
        """Одна реплика пользователя. Вставленный многострочный текст склеивается в одну."""
        # Метка кодировки (BOM) приходит при вставке и передаче текста из PowerShell — она
        # невидима, но иначе «/команда» не распознаётся и уходит модели как обычный текст.
        lines = [input(prompt).lstrip(BOM)]
        while _input_pending():
            try:
                lines.append(input())
            except EOFError:
                break
        return "\n".join(lines)

    def read_block(self) -> str:
        """Многострочный ввод до строки с одной точкой (команда /paste)."""
        self.notice("Вставь или набери текст. Закончить — строка с одной точкой «{}».".format(PASTE_END))
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == PASTE_END:
                break
            lines.append(line)
        return "\n".join(lines)

    def confirm(self, title: str, detail: str = "", always_ask: bool = False) -> Tuple[bool, str]:
        """Спросить разрешение. Возвращает (да/нет, комментарий для модели при отказе)."""
        if self.auto_approve and not always_ask:
            return True, ""
        # Действие показываем целиком, без обрезки: разрешение даётся на то, что видно.
        for line in clean(title).splitlines():
            self.out(self._gutter() + self.paint(line, "yellow"))
        if detail:
            self.show_diff(detail)
        question = "  ? Разрешить? [y — да / n — нет] " if always_ask \
            else "  ? Разрешить? [Enter — да / n — нет / a — да, и больше не спрашивать] "
        try:
            answer = input(self.paint(question, "yellow")).strip()
        except EOFError:
            return False, "no interactive terminal is available to approve actions"
        lowered = answer.lower()
        if lowered in ("y", "yes", "д", "да") or (lowered == "" and not always_ask):
            return True, ""
        if lowered in ("a", "all", "always", "в", "всегда", "а") and not always_ask:
            self.auto_approve = True
            return True, ""
        if lowered in ("n", "no", "н", "нет"):
            return False, ""
        return False, answer  # любой другой текст — отказ с пояснением, что сделать иначе
