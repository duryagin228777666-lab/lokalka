"""Запуск команд оболочки с тайм-аутом и аккуратной расшифровкой вывода."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, NamedTuple

IS_WINDOWS = os.name == "nt"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
SECRET_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN")


class CommandResult(NamedTuple):
    output: str
    exit_code: int
    timed_out: bool


def decode_output(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # cmd.exe и старые программы пишут в кодировке консоли (в русской Windows — cp866).
        return raw.decode("oem" if IS_WINDOWS else "latin-1", "replace")


def _kill_tree(process: "subprocess.Popen[bytes]") -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    process.kill()


def _child_env() -> Dict[str, str]:
    # API-ключи из окружения запускаемым программам не передаём.
    env = {name: value for name, value in os.environ.items() if not name.upper().endswith(SECRET_SUFFIXES)}
    # Чтобы «python» в командах был тем же интерпретатором, что запустил программу (важно для флешки).
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


def run_interactive(command: str, cwd: str) -> int:
    """Команда пользователя (!команда): ввод и вывод идут напрямую в терминал."""
    return subprocess.call(command, shell=True, cwd=cwd, env=_child_env())


def run(command: str, cwd: str, stdin_text: str = "", timeout: int = DEFAULT_TIMEOUT) -> CommandResult:
    """Выполнить команду в оболочке системы; stdout и stderr возвращаются вместе."""
    limit = max(1, min(int(timeout), MAX_TIMEOUT))
    env = dict(_child_env(), PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        raw, _ = process.communicate(stdin_text.encode("utf-8", "replace"), timeout=limit)
        return CommandResult(decode_output(raw), process.returncode, False)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        raw, _ = process.communicate()
        return CommandResult(decode_output(raw), -1, True)
    except BaseException:
        _kill_tree(process)
        raise
