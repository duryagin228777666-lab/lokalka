"""Настройка: выбор провайдера, ввод ключа и выбор модели."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from .config import (KEYLESS_PROVIDERS, PROVIDERS, ConfigError, provider_info, provider_settings,
                     resolve, save_config, validate_base_url, with_provider)
from .net import ApiError
from .providers import list_models
from .ui import Console

MODELS_SHOWN = 25
PLACEHOLDER_MODEL = "-"


def choose_provider(console: Console) -> Dict[str, str]:
    console.out("Чей API-ключ у тебя есть?")
    for number, info in enumerate(PROVIDERS, 1):
        console.out("  {:>2}. {}".format(number, info["name"]))
    while True:
        answer = console.ask("Номер: ")
        if answer.isdigit() and 1 <= int(answer) <= len(PROVIDERS):
            return PROVIDERS[int(answer) - 1]
        console.notice("Введи число от 1 до {}.".format(len(PROVIDERS)))


def ask_key(console: Console, info: Dict[str, str]) -> str:
    while True:
        key = console.ask_secret("API-ключ (вставь и нажми Enter; символы могут не отображаться): ")
        if key:
            return key
        console.notice("Ключ пустой, попробуй ещё раз.")


def ask_base_url(console: Console) -> str:
    while True:
        try:
            return validate_base_url(console.ask("Адрес API (например https://example.com/v1): "))
        except ConfigError as err:
            console.notice(str(err))


def pick_model(console: Console, models: List[str], hint: str) -> str:
    """Выбор из списка: номер, часть названия для поиска или точное имя модели."""
    if not models:
        example = " (например {})".format(hint) if hint else ""
        while True:
            name = console.ask("Имя модели{}: ".format(example))
            if name:
                return name
    shown = models
    while True:
        for number, name in enumerate(shown[:MODELS_SHOWN], 1):
            console.out("  {:>2}. {}".format(number, name))
        if len(shown) > MODELS_SHOWN:
            console.out("  ... и ещё {}. Введи часть названия, чтобы сузить список.".format(len(shown) - MODELS_SHOWN))
        answer = console.ask("Модель (номер / часть названия / точное имя): ")
        if answer.isdigit() and 1 <= int(answer) <= min(len(shown), MODELS_SHOWN):
            return shown[int(answer) - 1]
        if answer in models:
            return answer
        matches = [name for name in models if answer.lower() in name.lower()] if answer else []
        if len(matches) == 1:
            return matches[0]
        if matches:
            shown = matches
        elif answer and console.ask("Такой модели в списке нет. Всё равно использовать «{}»? [y/n] ".format(
                answer)).lower() in ("y", "д", "да", "yes"):
            return answer
        else:
            shown = models


def choose_model(console: Console, cfg: Dict[str, Any], provider_id: str) -> str:
    info = provider_info(provider_id) or {}
    models = []  # type: List[str]
    try:
        console.out("Получаю список моделей...")
        models = list_models(resolve(cfg, provider_id, PLACEHOLDER_MODEL))
    except ApiError as err:
        console.error(str(err))
        console.notice("Список получить не удалось — имя модели можно ввести вручную.")
    return pick_model(console, models, info.get("hint", ""))


def set_model(console: Console, cfg: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = cfg.get("provider") or ""
    updated = with_provider(cfg, provider_id, model=choose_model(console, cfg, provider_id))
    save_config(updated)
    return updated


def set_key(console: Console, cfg: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = cfg.get("provider") or ""
    info = provider_info(provider_id)
    if info is None or provider_id in KEYLESS_PROVIDERS:
        raise ConfigError("Этому провайдеру ключ не нужен.")
    updated = with_provider(cfg, provider_id, api_key=ask_key(console, info))
    save_config(updated)
    return updated


def run_setup(console: Console, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Полная настройка с нуля. Возвращает новые сохранённые настройки."""
    info = choose_provider(console)
    provider_id = info["id"]
    fields = {}  # type: Dict[str, str]
    if not info["base_url"]:
        fields["base_url"] = ask_base_url(console)
    if provider_id not in KEYLESS_PROVIDERS:
        saved_key = provider_settings(cfg, provider_id).get("api_key")
        if info["env"] and os.environ.get(info["env"]) and not saved_key:
            console.out("Беру ключ из переменной окружения {}.".format(info["env"]))
        elif saved_key and console.ask("Ключ уже сохранён. Оставить его? [Enter — да / n — ввести новый] ").lower() \
                not in ("n", "н", "нет", "no"):
            pass
        else:
            fields["api_key"] = ask_key(console, info)
    draft = with_provider(cfg, provider_id, **fields)
    updated = with_provider(draft, provider_id, model=choose_model(console, draft, provider_id))
    save_config(updated)
    console.out("Готово. Настройки сохранены рядом с программой (config.json).")
    return updated
