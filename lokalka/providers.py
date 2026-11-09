"""Общение с моделями: OpenAI-совместимый chat/completions и Anthropic Messages.

Внутри программы история хранится в одном нейтральном формате:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}], ...}
  {"role": "tool", "tool_call_id": str, "content": str}
и переводится в формат конкретного провайдера прямо перед отправкой.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from .net import ApiError, get_json, stream_events

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 32000
ANTHROPIC_HOST = "api.anthropic.com"
INVALID_JSON_KEY = "_invalid_json"
EMPTY_REPLY = "(пустой ответ)"
# Провайдеры, которым нужно возвращать их же reasoning_content между вызовами инструментов.
REASONING_ECHO_PROVIDERS = ("deepseek",)

Message = Dict[str, Any]
TextCallback = Callable[[str], None]


def _parse_arguments(raw: str) -> Dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {INVALID_JSON_KEY: raw}
    return parsed if isinstance(parsed, dict) else {INVALID_JSON_KEY: raw}


# ---------------------------------------------------------------- OpenAI-совместимые

def _openai_headers(settings: Dict[str, str]) -> Dict[str, str]:
    return {"Authorization": "Bearer " + settings["api_key"]} if settings["api_key"] else {}


def _openai_call(call: Dict[str, Any]) -> Dict[str, Any]:
    item = {"id": call["id"], "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
    if call.get("extra_content") is not None:
        item["extra_content"] = call["extra_content"]
    return item


def to_openai_messages(system: str, messages: List[Message], echo_reasoning: bool = False) -> List[Message]:
    out = [{"role": "system", "content": system}]  # type: List[Message]
    for message in messages:
        role = message["role"]
        if role == "assistant":
            # Совсем пустое сообщение ассистента часть провайдеров отвергает.
            fallback = "" if message.get("tool_calls") else EMPTY_REPLY
            item = {"role": "assistant", "content": message.get("content") or fallback}  # type: Message
            if message.get("tool_calls"):
                item["tool_calls"] = [_openai_call(call) for call in message["tool_calls"]]
            if echo_reasoning and message.get("reasoning"):
                item["reasoning_content"] = message["reasoning"]
            out.append(item)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]})
        else:
            out.append({"role": "user", "content": message["content"]})
    return out


def _slot_for(slots: List[Dict[str, Any]], by_index: Dict[int, int], index: int,
              call_id: str, name: str) -> Dict[str, Any]:
    """Ячейка для кусочка вызова. Новый id или новое имя на занятом индексе — это уже другой вызов."""
    position = by_index.get(index)
    if position is not None:
        current = slots[position]
        other_id = bool(call_id and current["id"] and call_id != current["id"])
        renamed = bool(name and current["name"] and not (call_id and call_id == current["id"]))
        if not (other_id or renamed):
            return current
    slots.append({"id": "", "name": "", "raw": "", "extra_content": None})
    by_index[index] = len(slots) - 1
    return slots[-1]


def consume_openai(events: Iterable[Dict[str, Any]], on_text: TextCallback) -> Message:
    text, reasoning = [], []  # type: List[str], List[str]
    slots, by_index = [], {}  # type: List[Dict[str, Any]], Dict[int, int]
    stop = ""
    for event in events:
        if event.get("error"):
            err = event["error"]
            raise ApiError(str(err.get("message") if isinstance(err, dict) else err))
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or choice.get("message") or {}
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            text.append(piece)
            on_text(piece)
        thought = delta.get("reasoning_content")
        if isinstance(thought, str):
            reasoning.append(thought)
        for position, part in enumerate(delta.get("tool_calls") or []):
            function = part.get("function") or {}
            slot = _slot_for(slots, by_index, part.get("index", position), part.get("id") or "",
                             function.get("name") or "")
            slot["id"] = slot["id"] or part.get("id") or ""
            slot["name"] = slot["name"] or function.get("name") or ""
            arguments = function.get("arguments")
            slot["raw"] += arguments if isinstance(arguments, str) else json.dumps(arguments or {})
            if part.get("extra_content") is not None:
                slot["extra_content"] = part["extra_content"]
        stop = choice.get("finish_reason") or stop
    calls = []
    for number, slot in enumerate(slot for slot in slots if slot["name"]):
        call = {"id": slot["id"] or "call_{}".format(number), "name": slot["name"],
                "arguments": _parse_arguments(slot["raw"])}
        if slot["extra_content"] is not None:
            call["extra_content"] = slot["extra_content"]
        calls.append(call)
    return {"role": "assistant", "content": "".join(text), "tool_calls": calls,
            "reasoning": "".join(reasoning), "stop": "max_tokens" if stop == "length" else stop}


def _chat_openai(settings: Dict[str, str], system: str, messages: List[Message],
                 tools: List[Dict[str, Any]], on_text: TextCallback) -> Message:
    echo = settings["id"] in REASONING_ECHO_PROVIDERS
    payload = {"model": settings["model"], "stream": True,
               "messages": to_openai_messages(system, messages, echo_reasoning=echo),
               "tools": [{"type": "function", "function": spec} for spec in tools]}
    url = settings["base_url"] + "/chat/completions"
    return consume_openai(stream_events(url, _openai_headers(settings), payload), on_text)


# ---------------------------------------------------------------- Anthropic

def _anthropic_root(settings: Dict[str, str]) -> str:
    base = settings["base_url"]
    return base[:-3] if base.endswith("/v1") else base


def _anthropic_headers(settings: Dict[str, str]) -> Dict[str, str]:
    return {"x-api-key": settings["api_key"], "anthropic-version": ANTHROPIC_VERSION}


def _assistant_blocks(message: Message, model: str) -> List[Dict[str, Any]]:
    # Блоки той же модели возвращаем дословно: в них подписанные «размышления»,
    # которые API принимает только без изменений.
    if message.get("blocks") and message.get("model") == model:
        return message["blocks"]
    blocks = []  # type: List[Dict[str, Any]]
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]})
    return blocks or [{"type": "text", "text": EMPTY_REPLY}]


def to_anthropic_messages(messages: List[Message], model: str) -> List[Message]:
    out = []  # type: List[Message]
    for message in messages:
        role = message["role"]
        if role == "assistant":
            out.append({"role": "assistant", "content": _assistant_blocks(message, model)})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
            # Результаты одного шага обязаны лежать в одном сообщении пользователя.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1] = {"role": "user", "content": out[-1]["content"] + [block]}
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": message["content"]})
    return out


def _apply_delta(block: Dict[str, Any], delta: Dict[str, Any], on_text: TextCallback) -> None:
    kind = delta.get("type")
    if kind == "text_delta":
        block["text"] = block.get("text", "") + delta.get("text", "")
        on_text(delta.get("text", ""))
    elif kind == "input_json_delta":
        block["_raw"] = block.get("_raw", "") + delta.get("partial_json", "")
    elif kind == "thinking_delta":
        block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
    elif kind == "signature_delta":
        block["signature"] = delta.get("signature", "")


def _finish_block(block: Dict[str, Any]) -> Dict[str, Any]:
    done = {key: value for key, value in block.items() if key != "_raw"}
    if done.get("type") == "tool_use":
        raw = block.get("_raw", "")
        done["input"] = _parse_arguments(raw) if raw else (done.get("input") or {})
    return done


def consume_anthropic(events: Iterable[Dict[str, Any]], on_text: TextCallback, model: str) -> Message:
    open_blocks = {}  # type: Dict[int, Dict[str, Any]]
    stop = ""
    for event in events:
        kind = event.get("type")
        if kind == "error":
            err = event.get("error") or {}
            raise ApiError(str(err.get("message") or err))
        if kind == "message" and isinstance(event.get("content"), list):  # ответ без потока
            open_blocks = {index: dict(block) for index, block in enumerate(event["content"])}
            for block in event["content"]:
                if block.get("type") == "text":
                    on_text(block.get("text", ""))
            stop = event.get("stop_reason") or stop
        elif kind == "content_block_start":
            open_blocks[event.get("index", len(open_blocks))] = dict(event.get("content_block") or {})
        elif kind == "content_block_delta" and event.get("index") in open_blocks:
            _apply_delta(open_blocks[event["index"]], event.get("delta") or {}, on_text)
        elif kind == "message_delta":
            stop = (event.get("delta") or {}).get("stop_reason") or stop
    blocks = [_finish_block(open_blocks[index]) for index in sorted(open_blocks)]
    text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    calls = [{"id": block.get("id", ""), "name": block.get("name", ""), "arguments": block["input"]}
             for block in blocks if block.get("type") == "tool_use"]
    broken = any(INVALID_JSON_KEY in call["arguments"] for call in calls)
    reply = {"role": "assistant", "content": text, "tool_calls": calls, "stop": stop}  # type: Message
    if blocks and not broken:
        reply.update(blocks=blocks, model=model)
    return reply


def _chat_anthropic(settings: Dict[str, str], system: str, messages: List[Message],
                    tools: List[Dict[str, Any]], on_text: TextCallback) -> Message:
    model = settings["model"]
    payload = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "stream": True, "system": system,
               "messages": to_anthropic_messages(messages, model),
               "tools": [{"name": spec["name"], "description": spec["description"],
                          "input_schema": spec["parameters"]} for spec in tools]}  # type: Dict[str, Any]
    if ANTHROPIC_HOST in settings["base_url"]:
        payload["cache_control"] = {"type": "ephemeral"}  # повторяющееся начало диалога стоит дешевле
    url = _anthropic_root(settings) + "/v1/messages"
    return consume_anthropic(stream_events(url, _anthropic_headers(settings), payload), on_text, model)


# ---------------------------------------------------------------- общее

def chat(settings: Dict[str, str], system: str, messages: List[Message],
         tools: List[Dict[str, Any]], on_text: TextCallback) -> Message:
    """Один запрос к модели. Текст отдаётся в on_text по мере генерации."""
    if settings["kind"] == "anthropic":
        return _chat_anthropic(settings, system, messages, tools, on_text)
    return _chat_openai(settings, system, messages, tools, on_text)


def without_thinking(messages: List[Message]) -> List[Message]:
    """История без сохранённых блоков Anthropic (и подписанных «размышлений» в них)."""
    return [{key: value for key, value in message.items() if key not in ("blocks", "model")}
            for message in messages]


def list_models(settings: Dict[str, str]) -> List[str]:
    """Список моделей, доступных ключу. Пустой список, если провайдер его не отдаёт."""
    if settings["kind"] == "anthropic":
        data = get_json(_anthropic_root(settings) + "/v1/models?limit=1000", _anthropic_headers(settings))
    else:
        data = get_json(settings["base_url"] + "/models", _openai_headers(settings))
    items = data.get("data") if isinstance(data, dict) else data
    names = []  # type: List[str]
    for item in items if isinstance(items, list) else []:
        name = item.get("id") if isinstance(item, dict) else None
        if isinstance(name, str) and name:
            names.append(name[len("models/"):] if name.startswith("models/") else name)
    return sorted(set(names))


def find_model(models: List[str], query: str) -> Optional[str]:
    """Точное совпадение или единственная модель, в названии которой есть query."""
    if query in models:
        return query
    matches = [name for name in models if query.lower() in name.lower()]
    return matches[0] if len(matches) == 1 else None
