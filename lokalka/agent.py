"""Цикл агента: вопрос -> модель -> инструменты -> модель ... -> ответ."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .net import ApiError
from .prompt import build_system_prompt
from .providers import chat, without_thinking
from .tools import TOOL_SPECS, Workspace

MAX_STEPS = 50
HISTORY_CHAR_LIMIT = 300000
KEEP_RECENT_RESULTS = 6
OMITTED_RESULT = "[old tool output removed to save space; call the tool again if you need it]"
INTERRUPTED_RESULT ="Interrupted by the user before this tool finished."

Message = Dict[str, Any]


def history_size(messages: List[Message]) -> int:
    return sum(len(json.dumps(message, ensure_ascii=False)) for message in without_thinking(messages))


def trim_history(messages: List[Message], limit: int = HISTORY_CHAR_LIMIT) -> List[Message]:
    """Если история разрослась — отбросить самые старые вопросы целиком (вместе с их шагами)."""
    if history_size(messages) <= limit:
        return messages
    starts = [index for index, message in enumerate(messages) if message["role"] == "user"]
    tail = messages
    for start in starts[1:]:
        tail = messages[start:]
        if history_size(tail) <= limit:
            break
    # После обрезки подписанные «размышления» Anthropic недействительны — убираем их.
    return _shrink_tool_results(without_thinking(tail), limit)


def _shrink_tool_results(messages: List[Message], limit: int) -> List[Message]:
    """Один длинный вопрос тоже может переполнить историю: старые результаты инструментов сворачиваем."""
    big = [index for index, message in enumerate(messages)
           if message["role"] == "tool" and len(message["content"]) > len(OMITTED_RESULT)]
    shrunk = list(messages)
    for index in big[:-KEEP_RECENT_RESULTS]:
        if history_size(shrunk) <= limit:
            break
        shrunk[index] = dict(shrunk[index], content=OMITTED_RESULT)
    return shrunk


def close_open_calls(messages: List[Message]) -> List[Message]:
    """Дописать результаты вызовам, которые не успели выполниться: API требует пару на каждый."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] == "assistant":
            answered = {later["tool_call_id"] for later in messages[index + 1:] if later["role"] == "tool"}
            missing = [call for call in message.get("tool_calls") or [] if call["id"] not in answered]
            return messages + [{"role": "tool", "tool_call_id": call["id"], "content": INTERRUPTED_RESULT}
                               for call in missing]
        if message["role"] == "user":
            break
    return messages


class Agent:
    def __init__(self, settings: Dict[str, str], workspace: Workspace, console: Any) -> None:
        self.settings = settings
        self.workspace = workspace
        self.console = console
        self.messages = []  # type: List[Message]
        self.system = build_system_prompt(workspace.root, workspace.brain.index())

    def reset(self) -> None:
        self.messages = []
        self.system = build_system_prompt(self.workspace.root, self.workspace.brain.index())

    def send(self, text: str) -> bool:
        """Обработать один запрос пользователя. False — если закончилось ошибкой или прерыванием."""
        question = {"role": "user", "content": text}
        self.messages = trim_history(self.messages + [question])
        try:
            self._loop()
            return True
        except KeyboardInterrupt:
            self.messages = close_open_calls(self.messages)
            self.console.notice("Прервано.")
        except ApiError as err:
            self.messages = close_open_calls(self.messages)
            if self.messages and self.messages[-1] == question:
                self.messages = self.messages[:-1]  # запрос не дошёл — можно просто повторить
            self.console.error(str(err))
        return False

    def _loop(self) -> None:
        for _ in range(MAX_STEPS):
            self.messages = trim_history(self.messages)
            reply = self._request()
            self.messages = self.messages + [reply]
            if reply.get("stop") == "max_tokens":
                self.console.notice("Ответ обрезан по длине.")
            elif reply.get("stop") == "refusal":
                self.console.notice("Модель отказалась выполнять этот запрос.")
            if not reply.get("tool_calls"):
                return
            for call in reply["tool_calls"]:
                self.console.tool_call(call["name"], call["arguments"])
                result = self.workspace.execute(call["name"], call["arguments"])
                self.console.tool_result(result)
                self.messages = self.messages + [{"role": "tool", "tool_call_id": call["id"], "content": result}]
        self.console.notice("Сделано {} шагов подряд — останавливаюсь. Напиши «продолжай», если нужно.".format(MAX_STEPS))

    def _request(self) -> Message:
        self.console.stream_start()
        try:
            try:
                return chat(self.settings, self.system, self.messages, TOOL_SPECS, self.console.stream_text)
            except ApiError as err:
                has_blocks = any("blocks" in message for message in self.messages)
                if not (err.status == 400 and has_blocks and "thinking" in str(err)):
                    raise
                # Сервер отверг сохранённые «размышления» — повторяем один раз без них.
                self.messages = without_thinking(self.messages)
                return chat(self.settings, self.system, self.messages, TOOL_SPECS, self.console.stream_text)
        finally:
            self.console.stream_end()
