"""Поиск подключённых флешек и выбор рабочей папки на них."""
from __future__ import annotations

import getpass
import os
import shutil
from typing import Callable, List, NamedTuple

from .config import APP_DIR

WORK_DIR_NAME = "workspace"
DRIVE_REMOVABLE = 2
LABEL_BUFFER = 261
POSIX_MOUNT_ROOTS = ("/media/{user}", "/run/media/{user}", "/media", "/Volumes")
NO_WORDS = ("n", "no", "н", "нет")
YES_WORDS = ("y", "yes", "д", "да")


class Drive(NamedTuple):
    root: str        # «E:\» или «/media/user/STICK»
    label: str       # имя тома, может быть пустым
    free_gb: float


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _free_gb(root: str) -> float:
    try:
        return round(shutil.disk_usage(root).free / 1024 ** 3, 1)
    except OSError:
        return 0.0


def _windows_drives() -> List[Drive]:
    import ctypes
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel.GetLogicalDrives()
    found = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = "{}:\\".format(chr(ord("A") + index))
        if kernel.GetDriveTypeW(root) != DRIVE_REMOVABLE:
            continue
        label = ctypes.create_unicode_buffer(LABEL_BUFFER)
        # Пустой кардридер без карты не отвечает — такой диск пропускаем.
        if not kernel.GetVolumeInformationW(root, label, LABEL_BUFFER, None, None, None, None, 0):
            continue
        found.append(Drive(root, label.value, _free_gb(root)))
    return found


def _posix_drives() -> List[Drive]:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = ""
    system_disk = os.stat("/").st_dev
    seen, found = set(), []
    for template in POSIX_MOUNT_ROOTS:
        base = template.format(user=user)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if not os.path.ismount(path) or path in seen:
                continue
            if os.stat(path).st_dev == system_disk:  # /Volumes/Macintosh HD и подобное
                continue
            seen.add(path)
            found.append(Drive(path, name, _free_gb(path)))
    return found


def find_drives() -> List[Drive]:
    """Подключённые съёмные носители. Пустой список, если найти не удалось."""
    try:
        return _windows_drives() if os.name == "nt" else _posix_drives()
    except (OSError, AttributeError, ValueError):
        return []


def _is_on(drive: Drive, path: str) -> bool:
    root, target = _normalized(drive.root), _normalized(path)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:  # другой диск
        return False


def program_drive(drives: List[Drive], app_dir: str = APP_DIR) -> int:
    """Номер флешки, на которой лежит сама программа, или -1."""
    for index, drive in enumerate(drives):
        if _is_on(drive, app_dir):
            return index
    return -1


def work_dir_on(drive: Drive, app_dir: str = APP_DIR) -> str:
    """Рабочая папка на флешке: рядом с программой, если она там, иначе workspace в корне."""
    if _is_on(drive, app_dir):
        return os.path.join(app_dir, WORK_DIR_NAME)
    return os.path.join(drive.root, WORK_DIR_NAME)


def describe(drive: Drive) -> str:
    name = drive.label or "без имени"
    return "{}  «{}», свободно {} ГБ".format(drive.root, name, drive.free_gb)


def _pick_drive(drives: List[Drive], ask: Callable[[str], str], say: Callable[[str], None],
                app_dir: str) -> int:
    """Номер выбранной флешки или -1. Флешка с программой предлагается первой."""
    home = program_drive(drives, app_dir)
    preferred = max(home, 0)
    if len(drives) == 1:
        return preferred
    say("Нашлось несколько флешек:")
    for number, drive in enumerate(drives, 1):
        mark = "  ← здесь программа" if number - 1 == home else ""
        say("  {}. {}{}".format(number, describe(drive), mark))
    answer = ask("Какую взять? [Enter — {}] ".format(preferred + 1)).strip()
    if not answer:
        return preferred
    if answer.isdigit() and 1 <= int(answer) <= len(drives):
        return int(answer) - 1
    say("Нет такого номера — ничего не меняю.")
    return -1


def choose(drives: List[Drive], ask: Callable[[str], str], say: Callable[[str], None],
           app_dir: str = APP_DIR) -> str:
    """Предложить флешку и папку на ней; вернуть выбранную папку или "" при отказе."""
    if not drives:
        say("Флешка не найдена. Вставь её и повтори команду или укажи папку вручную: /cd E:\\папка")
        return ""
    index = _pick_drive(drives, ask, say, app_dir)
    if index < 0:
        return ""
    drive = drives[index]
    target = work_dir_on(drive, app_dir)
    say("Флешка: {}".format(describe(drive)))
    answer = ask("Работать в папке {}? [Enter — да / n — нет / или впиши другую папку на флешке] ".format(
        target)).strip()
    if answer.lower() in NO_WORDS:
        return ""
    if answer and answer.lower() not in YES_WORDS:
        custom = os.path.abspath(os.path.join(drive.root, answer))
        if not _is_on(drive, custom):
            say("Эта папка не на флешке — ничего не меняю.")
            return ""
        target = custom
    return target
