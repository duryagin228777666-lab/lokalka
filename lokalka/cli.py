"""Точка входа: аргументы запуска, строка ввода и команды со слешем."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from . import __version__, shell, usb, wizard
from .agent import Agent
from .config import (APP_DIR, ConfigError, load_config, resolve, save_config, validate_base_url,
                     with_provider)
from .tools import Workspace
from .ui import Console, configure_stdio

DEFAULT_WORK_DIR = "workspace"
EXIT_WORDS = ("/exit", "/quit", "/q", "/выход")

HELP_TEXT = """\
Просто напиши задачу и нажми Enter. Можно вставлять текст в несколько строк.

  /model [имя]   сменить модель (без имени — выбрать из списка)
  /setup         сменить провайдера и ключ
  /key           ввести API-ключ заново
  /url адрес     свой адрес API для текущего провайдера (прокси); /url - — вернуть стандартный
  /auto          не спрашивать разрешения на правки и команды (вкл/выкл)
  /brain [слово] второй мозг: карта заметок и связей, или поиск по ним
  /forget тема   стереть заметку из второго мозга
  /undo          отменить последнюю правку файла
  /clear         начать новый диалог (забыть историю)
  /cd папка      сменить рабочую папку
  /usb           работать на флешке: найду её сам и спрошу, всё ли верно (/флешка)
  /paste         многострочный ввод (конец — строка с одной точкой)
  !команда       выполнить команду в терминале самому
  /exit          выход

Ctrl+C прерывает ответ или команду."""


def choose_root(requested: Optional[str]) -> str:
    """Рабочая папка. Запуск двойным щелчком из папки программы — работаем в её подпапке."""
    if requested:
        root = os.path.abspath(os.path.expanduser(requested))
        if not os.path.isdir(root):
            raise ConfigError("Папка не найдена: {}".format(root))
        return root
    current = os.getcwd()
    if os.path.normcase(os.path.realpath(current)) != os.path.normcase(os.path.realpath(APP_DIR)):
        return current
    root = os.path.join(APP_DIR, DEFAULT_WORK_DIR)
    os.makedirs(root, exist_ok=True)
    return root


class Session:
    """Состояние одного запуска: настройки, агент и обработчики команд."""

    def __init__(self, console: Console, cfg: Dict[str, Any], settings: Dict[str, str], root: str) -> None:
        self.console = console
        self.cfg = cfg
        self.agent = Agent(settings, Workspace(root, console.confirm), console)
        self._commands = {"/help": self._help, "/model": self._model, "/setup": self._setup,
                          "/provider": self._setup, "/key": self._key, "/url": self._url,
                          "/auto": self._auto, "/undo": self._undo, "/clear": self._clear,
                          "/new": self._clear, "/cd": self._cd, "/paste": self._paste,
                          "/brain": self._brain, "/forget": self._forget,
                          "/usb": self._usb, "/флешка": self._usb,
                          }  # type: Dict[str, Callable[[str], None]]

    def banner(self) -> None:
        settings = self.agent.settings
        self.console.banner(__version__, settings["id"], settings["model"], self.agent.workspace.root)

    def _apply(self, cfg: Dict[str, Any]) -> None:
        self.agent.settings = resolve(cfg)
        self.cfg = cfg
        self.console.out("Теперь: {} · {}".format(self.agent.settings["id"], self.agent.settings["model"]))

    # ------------------------------------------------------------ команды

    def _help(self, _: str) -> None:
        self.console.help(HELP_TEXT)

    def _model(self, argument: str) -> None:
        if argument:
            cfg = with_provider(self.cfg, self.agent.settings["id"], model=argument)
            save_config(cfg)
        else:
            cfg = wizard.set_model(self.console, self.cfg)
        self._apply(cfg)

    def _setup(self, _: str) -> None:
        self._apply(wizard.run_setup(self.console, self.cfg))

    def _key(self, _: str) -> None:
        self._apply(wizard.set_key(self.console, self.cfg))

    def _url(self, argument: str) -> None:
        if not argument:
            self.console.out("Сейчас: {}".format(self.agent.settings["base_url"]))
            return
        base_url = "" if argument == "-" else validate_base_url(argument)
        cfg = with_provider(self.cfg, self.agent.settings["id"], base_url=base_url)
        save_config(cfg)
        self._apply(cfg)

    def _auto(self, _: str) -> None:
        self.console.auto_approve = not self.console.auto_approve
        state = "включён: правки и команды выполняются без вопросов" if self.console.auto_approve \
            else "выключен: перед правками и командами будет вопрос"
        self.console.notice("Авторежим " + state + ".")

    def _brain(self, argument: str) -> None:
        brain = self.agent.workspace.brain
        self.console.plain(brain.render_search(argument) if argument else brain.map())

    def _forget(self, argument: str) -> None:
        if not argument:
            self.console.notice("Укажи название заметки: /forget тема")
            return
        removed = self.agent.workspace.brain.forget(argument)
        self.console.out("Заметка стёрта." if removed else "Такой заметки нет.")

    def _undo(self, _: str) -> None:
        self.console.out(self.agent.workspace.undo())

    def _clear(self, _: str) -> None:
        self.agent.reset()
        self.console.out("Начат новый диалог.")

    def _cd(self, argument: str) -> None:
        if not argument:
            self.console.out("Рабочая папка: {}".format(self.agent.workspace.root))
            return
        target = os.path.abspath(os.path.join(self.agent.workspace.root, os.path.expanduser(argument.strip('"'))))
        if not os.path.isdir(target):
            raise ConfigError("Папка не найдена: {}".format(target))
        self._switch(target)

    def _usb(self, _: str) -> None:
        target = usb.choose(usb.find_drives(), self.console.ask, self.console.out)
        if not target:
            self.console.out("Рабочая папка не изменилась: {}".format(self.agent.workspace.root))
            return
        os.makedirs(target, exist_ok=True)
        self._switch(target)

    def _switch(self, target: str) -> None:
        self.agent.workspace = Workspace(target, self.console.confirm, self.agent.workspace.brain)
        self.agent.reset()
        self.console.out("Рабочая папка: {} (диалог начат заново)".format(self.agent.workspace.root))

    def _paste(self, _: str) -> None:
        text = self.console.read_block().strip()
        if text:
            self.agent.send(text)

    def _shell(self, command: str) -> None:
        if command:
            shell.run_interactive(command, self.agent.workspace.root)

    # ------------------------------------------------------------ цикл

    def handle(self, text: str) -> bool:
        """Обработать одну реплику. False — пора выходить."""
        if text.lower() in EXIT_WORDS:
            return False
        try:
            if text.startswith("!"):
                self._shell(text[1:].strip())
            elif text.startswith("/"):
                name, _, argument = text.partition(" ")
                command = self._commands.get(name.lower())
                if command is None:
                    self.console.notice("Нет такой команды: {}. Список — /help".format(name))
                else:
                    command(argument.strip())
            else:
                self.agent.send(text)
        except ConfigError as err:
            self.console.error(str(err))
        except OSError as err:
            self.console.error(str(err.strerror or err))
        except (KeyboardInterrupt, EOFError):
            self.console.notice("\nОтменено.")
        return True

    def loop(self) -> None:
        while True:
            try:
                text = self.console.read_input(self.console.prompt()).strip()
            except EOFError:
                return
            except KeyboardInterrupt:
                self.console.notice("\nВыход — /exit")
                continue
            if text and not self.handle(text):
                return


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lokalka", description="Локалка — ИИ-агент для кода в терминале.")
    parser.add_argument("-p", "--prompt", help="выполнить один запрос и выйти")
    parser.add_argument("--provider", help="провайдер на этот запуск (openrouter, openai, anthropic, ...)")
    parser.add_argument("--model", help="модель на этот запуск")
    parser.add_argument("--cwd", help="рабочая папка (по умолчанию — текущая)")
    parser.add_argument("--usb", action="store_true", help="сразу предложить работать на флешке")
    parser.add_argument("--auto", action="store_true", help="не спрашивать разрешения на правки и команды")
    parser.add_argument("--version", action="version", version="lokalka " + __version__)
    return parser.parse_args(argv)


def connect(console: Console, cfg: Dict[str, Any], args: argparse.Namespace) -> "tuple[Dict[str, Any], Dict[str, str]]":
    """Параметры подключения; при первом запуске в терминале — мастер настройки."""
    try:
        return cfg, resolve(cfg, args.provider, args.model)
    except ConfigError as err:
        if args.prompt or args.provider or not sys.stdin.isatty() and not os.environ.get("PYCHARM_HOSTED"):
            raise
        console.notice("{} Давай настроим.".format(err))
    cfg = wizard.run_setup(console, cfg)
    return cfg, resolve(cfg, None, args.model)


def main(argv: Optional[List[str]] = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    console = Console(auto_approve=args.auto)
    try:
        cfg, settings = connect(console, load_config(), args)
        session = Session(console, cfg, settings, choose_root(args.cwd))
    except ConfigError as err:
        console.error(str(err))
        return 1
    except (KeyboardInterrupt, EOFError):
        console.out()
        return 1
    if args.prompt:
        return 0 if session.agent.send(args.prompt) else 1
    session.banner()
    if args.usb:
        session.handle("/usb")
    session.loop()
    return 0
