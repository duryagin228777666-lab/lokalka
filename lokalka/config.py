"""Настройки: список провайдеров, ключи и выбранная модель.

config.json лежит рядом с программой (то есть на флешке), а не в профиле
пользователя — на чужом компьютере после работы ничего не остаётся.
"""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_ENV = "LOKALKA_CONFIG"

# kind: "openai" — совместимый с OpenAI chat/completions, "anthropic" — Messages API.
PROVIDERS: List[Dict[str, str]] = [
    {"id": "openrouter", "name": "OpenRouter (один ключ — модели всех компаний)", "kind": "openai",
     "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY", "hint": "openai/gpt-4o-mini"},
    {"id": "openai", "name": "OpenAI (GPT)", "kind": "openai",
     "base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY", "hint": "gpt-4o-mini"},
    {"id": "anthropic", "name": "Anthropic (Claude)", "kind": "anthropic",
     "base_url": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY", "hint": "claude-sonnet-5-5"},
    {"id": "gemini", "name": "Google Gemini", "kind": "openai",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY",
     "hint": "gemini-2.5-flash"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "openai",
     "base_url": "https://api.deepseek.com", "env": "DEEPSEEK_API_KEY", "hint": "deepseek-chat"},
    {"id": "groq", "name": "Groq (быстро, есть бесплатный тариф)", "kind": "openai",
     "base_url": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY", "hint": "llama-3.3-70b-versatile"},
    {"id": "mistral", "name": "Mistral", "kind": "openai",
     "base_url": "https://api.mistral.ai/v1", "env": "MISTRAL_API_KEY", "hint": "mistral-large-latest"},
    {"id": "xai", "name": "xAI (Grok)", "kind": "openai",
     "base_url": "https://api.x.ai/v1", "env": "XAI_API_KEY", "hint": "grok-4"},
    {"id": "together", "name": "Together AI", "kind": "openai",
     "base_url": "https://api.together.xyz/v1", "env": "TOGETHER_API_KEY", "hint": ""},
    {"id": "cerebras", "name": "Cerebras", "kind": "openai",
     "base_url": "https://api.cerebras.ai/v1", "env": "CEREBRAS_API_KEY", "hint": ""},
    {"id": "ollama", "name": "Ollama (локальные модели, ключ не нужен)", "kind": "openai",
     "base_url": "http://localhost:11434/v1", "env": "", "hint": "qwen2.5-coder"},
    {"id": "custom", "name": "Свой адрес (любой OpenAI-совместимый API или прокси)", "kind": "openai",
     "base_url": "", "env": "LOKALKA_API_KEY", "hint": ""},
]

KEYLESS_PROVIDERS = ("ollama",)


class ConfigError(Exception):
    """Проблема с настройками, которую нужно показать пользователю."""


def config_path() -> str:
    return os.environ.get(CONFIG_ENV) or os.path.join(APP_DIR, "config.json")


def provider_info(provider_id: str) -> Optional[Dict[str, str]]:
    for info in PROVIDERS:
        if info["id"] == provider_id:
            return info
    return None


def load_config() -> Dict[str, Any]:
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as err:
        raise ConfigError("Не удалось прочитать {}: {}".format(path, err))
    if not isinstance(data, dict):
        raise ConfigError("Файл {} повреждён: ожидался JSON-объект.".format(path))
    return data


def save_config(cfg: Dict[str, Any]) -> None:
    path = config_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError as err:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise ConfigError("Не удалось сохранить {}: {}".format(path, err))


def provider_settings(cfg: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    stored = (cfg.get("providers") or {}).get(provider_id) or {}
    return dict(stored) if isinstance(stored, dict) else {}


def with_provider(cfg: Dict[str, Any], provider_id: str, **fields: Any) -> Dict[str, Any]:
    """Новая копия настроек, где provider_id активен и его поля обновлены."""
    merged = dict(provider_settings(cfg, provider_id), **fields)
    providers = dict(cfg.get("providers") or {})
    providers[provider_id] = {key: value for key, value in merged.items() if value not in (None, "")}
    return dict(cfg, provider=provider_id, providers=providers)


def validate_base_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("Адрес API должен начинаться с http:// или https://")
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        raise ConfigError("По http:// ключ уйдёт по сети открытым текстом. "
                          "Используй https:// (http разрешён только для своего компьютера и локальной сети).")
    return cleaned


def _is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def resolve(cfg: Dict[str, Any], provider_id: Optional[str] = None,
            model: Optional[str] = None) -> Dict[str, str]:
    """Готовые к работе параметры подключения или ConfigError с понятной причиной."""
    chosen = provider_id or cfg.get("provider")
    if not chosen:
        raise ConfigError("Провайдер не выбран.")
    info = provider_info(chosen)
    if info is None:
        raise ConfigError("Неизвестный провайдер: {}".format(chosen))
    stored = provider_settings(cfg, chosen)
    api_key = stored.get("api_key") or (os.environ.get(info["env"]) if info["env"] else "") or ""
    base_url = stored.get("base_url") or info["base_url"]
    chosen_model = model or stored.get("model") or ""
    if not base_url:
        raise ConfigError("Для провайдера «{}» не задан адрес API.".format(info["name"]))
    if not api_key and chosen not in KEYLESS_PROVIDERS:
        raise ConfigError("Для провайдера «{}» не задан API-ключ.".format(info["name"]))
    if not chosen_model:
        raise ConfigError("Для провайдера «{}» не выбрана модель.".format(info["name"]))
    return {"id": chosen, "name": info["name"], "kind": info["kind"],
            "base_url": validate_base_url(base_url), "api_key": api_key, "model": chosen_model}
