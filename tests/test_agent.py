"""Формат сообщений, история и полный цикл агента на поддельном сервере."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lokalka import config, net  # noqa: E402
from lokalka.agent import Agent, close_open_calls, trim_history  # noqa: E402
from lokalka.providers import (consume_openai, find_model, list_models, to_anthropic_messages,  # noqa: E402
                               to_openai_messages)
from lokalka.tools import Workspace  # noqa: E402


def sse(*events):
    return "".join("data: {}\n\n".format(json.dumps(event)) for event in events).encode("utf-8")


def openai_tool_call(path, content):
    """Вызов write_file, аргументы которого приходят двумя кусками — как в настоящем потоке."""
    arguments = json.dumps({"path": path, "content": content})
    half = len(arguments) // 2
    first = {"index": 0, "id": "call_1", "function": {"name": "write_file", "arguments": arguments[:half]}}
    second = {"index": 0, "function": {"arguments": arguments[half:]}}
    return sse({"choices": [{"delta": {"tool_calls": [first]}}]},
               {"choices": [{"delta": {"tool_calls": [second]}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]\n\n"


def anthropic_tool_call(path, content):
    arguments = json.dumps({"path": path, "content": content})
    return sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": arguments}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}})


class FakeServer:
    """Отдаёт заранее заготовленные ответы по очереди и запоминает присланные запросы."""

    def __init__(self, replies):
        self.requests = []
        queue = list(replies)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                status, payload = queue.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                payload = json.dumps({"data": [{"id": "models/b-model"}, {"id": "a-model"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeConsole:
    def __init__(self):
        self.text, self.notices, self.errors, self.calls = [], [], [], []

    def stream_start(self):
        pass

    def stream_end(self):
        pass

    def stream_text(self, piece):
        self.text.append(piece)

    def tool_call(self, name, args):
        self.calls.append(name)

    def tool_result(self, result):
        pass

    def notice(self, text):
        self.notices.append(text)

    def error(self, text):
        self.errors.append(text)


class AgentCase(unittest.TestCase):
    def start(self, kind, replies):
        self.server = FakeServer(replies)
        self.addCleanup(self.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.console = FakeConsole()
        settings = {"id": kind, "name": kind, "kind": kind, "base_url": self.server.url,
                    "api_key": "test-key", "model": "test-model"}
        return Agent(settings, Workspace(self.root, lambda *_: (True, "")), self.console)

    def read(self, name):
        with open(os.path.join(self.root, name), encoding="utf-8") as handle:
            return handle.read()


class OpenAiLoopTest(AgentCase):
    def test_tool_call_is_executed_and_result_sent_back(self):
        done = sse({"choices": [{"delta": {"content": "Гото"}}]},
                   {"choices": [{"delta": {"content": "во"}, "finish_reason": "stop"}]})
        agent = self.start("openai", [(200, openai_tool_call("a.py", "print('привет')\n")), (200, done)])

        ok = agent.send("напиши a.py")

        self.assertTrue(ok)
        self.assertEqual(self.read("a.py"), "print('привет')\n")
        self.assertEqual("".join(self.console.text), "Готово")
        second = self.server.requests[1]
        self.assertEqual(second["path"], "/chat/completions")
        self.assertEqual(second["headers"]["Authorization"], "Bearer test-key")
        roles = [message["role"] for message in second["body"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(second["body"]["messages"][3]["tool_call_id"], "call_1")
        self.assertIn("Wrote a.py", second["body"]["messages"][3]["content"])

    def test_failed_request_is_reported_and_question_can_be_repeated(self):
        error = json.dumps({"error": {"message": "bad key"}}).encode("utf-8")
        agent = self.start("openai", [(401, error)])

        ok = agent.send("привет")

        self.assertFalse(ok)
        self.assertIn("bad key", self.console.errors[0])
        self.assertIn("/key", self.console.errors[0])
        self.assertEqual(agent.messages, [])

    def test_truncated_tool_arguments_are_returned_to_model_as_error(self):
        cut = sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": '{"path": "a.py", "con'}}]},
            "finish_reason": "length"}]})
        done = sse({"choices": [{"delta": {"content": "ок"}, "finish_reason": "stop"}]})
        agent = self.start("openai", [(200, cut), (200, done)])

        agent.send("напиши")

        self.assertIn("not valid JSON", self.server.requests[1]["body"]["messages"][3]["content"])
        self.assertIn("Ответ обрезан по длине.", self.console.notices)


class AnthropicLoopTest(AgentCase):
    def test_thinking_blocks_are_replayed_verbatim_with_tool_result(self):
        done = sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Готово"}},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        agent = self.start("anthropic", [(200, anthropic_tool_call("b.py", "x = 1\n")), (200, done)])

        agent.send("напиши b.py")

        self.assertEqual(self.read("b.py"), "x = 1\n")
        second = self.server.requests[1]
        self.assertEqual(second["path"], "/v1/messages")
        headers = {name.lower(): value for name, value in second["headers"].items()}
        self.assertEqual(headers["x-api-key"], "test-key")
        assistant, results = second["body"]["messages"][1], second["body"]["messages"][2]
        self.assertEqual(assistant["content"][0], {"type": "thinking", "thinking": "", "signature": "SIG"})
        self.assertEqual(assistant["content"][1]["input"], {"path": "b.py", "content": "x = 1\n"})
        self.assertEqual(results["content"][0]["type"], "tool_result")
        self.assertEqual(results["content"][0]["tool_use_id"], "toolu_1")

    def test_rejected_thinking_is_dropped_and_request_retried_once(self):
        rejected = json.dumps({"error": {"message": "Invalid `signature` in `thinking` block."}}).encode("utf-8")
        done = sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "ок"}},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        agent = self.start("anthropic", [(200, anthropic_tool_call("c.py", "1\n")), (400, rejected), (200, done)])

        ok = agent.send("напиши c.py")

        self.assertTrue(ok)
        retried = self.server.requests[2]["body"]["messages"][1]["content"]
        self.assertEqual([block["type"] for block in retried], ["tool_use"])


class MessageFormatTest(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "read_file", "arguments": {"path": "a"}},
            {"id": "2", "name": "list_dir", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
        {"role": "tool", "tool_call_id": "2", "content": "r2"},
    ]

    def test_anthropic_groups_parallel_results_into_one_user_message(self):
        converted = to_anthropic_messages(self.HISTORY, "m")

        self.assertEqual([message["role"] for message in converted], ["user", "assistant", "user"])
        self.assertEqual([block["tool_use_id"] for block in converted[2]["content"]], ["1", "2"])

    def test_anthropic_ignores_blocks_saved_by_another_model(self):
        message = dict(self.HISTORY[1], blocks=[{"type": "thinking", "thinking": "", "signature": "S"}], model="old")

        converted = to_anthropic_messages([self.HISTORY[0], message], "new")

        self.assertEqual([block["type"] for block in converted[1]["content"]], ["tool_use", "tool_use"])

    def test_openai_serializes_arguments_as_json_string(self):
        converted = to_openai_messages("sys", self.HISTORY)

        self.assertEqual(converted[2]["tool_calls"][0]["function"]["arguments"], '{"path": "a"}')

    def test_parallel_calls_sharing_one_index_stay_separate(self):
        events = [{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "list_dir", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "b", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]}}]}]

        reply = consume_openai(events, lambda _: None)

        self.assertEqual([call["id"] for call in reply["tool_calls"]], ["a", "b"])
        self.assertEqual(reply["tool_calls"][1]["arguments"], {"path": "x"})


class HistoryTest(unittest.TestCase):
    def test_trim_drops_oldest_whole_turns_and_starts_with_user(self):
        history = [{"role": "user", "content": "a" * 500}, {"role": "assistant", "content": "b" * 500},
                   {"role": "user", "content": "c"}, {"role": "assistant", "content": "d", "blocks": [1]}]

        trimmed = trim_history(history, limit=200)

        self.assertEqual([message["content"] for message in trimmed], ["c", "d"])
        self.assertNotIn("blocks", trimmed[1])

    def test_short_history_is_untouched(self):
        history = [{"role": "user", "content": "a"}]

        self.assertIs(trim_history(history), history)

    def test_unanswered_tool_calls_get_placeholder_results(self):
        history = [{"role": "user", "content": "q"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": "1", "name": "x", "arguments": {}}, {"id": "2", "name": "y", "arguments": {}}]},
                   {"role": "tool", "tool_call_id": "1", "content": "done"}]

        closed = close_open_calls(history)

        self.assertEqual([message.get("tool_call_id") for message in closed[2:]], ["1", "2"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._old = os.environ.get(config.CONFIG_ENV)
        os.environ[config.CONFIG_ENV] = os.path.join(tmp.name, "config.json")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop(config.CONFIG_ENV, None)
        else:
            os.environ[config.CONFIG_ENV] = self._old

    def test_saved_settings_round_trip_and_resolve(self):
        cfg = config.with_provider({}, "openai", api_key="k", model="m")
        config.save_config(cfg)

        settings = config.resolve(config.load_config())

        self.assertEqual((settings["kind"], settings["api_key"], settings["model"]), ("openai", "k", "m"))
        self.assertEqual(settings["base_url"], "https://api.openai.com/v1")

    def test_with_provider_does_not_mutate_original(self):
        original = {"provider": "openai", "providers": {"openai": {"api_key": "k"}}}

        config.with_provider(original, "groq", api_key="g")

        self.assertEqual(original, {"provider": "openai", "providers": {"openai": {"api_key": "k"}}})

    def test_missing_key_and_bad_url_are_explained(self):
        with self.assertRaises(config.ConfigError):
            config.resolve(config.with_provider({}, "openai", model="m", api_key=""))
        with self.assertRaises(config.ConfigError):
            config.validate_base_url("example.com")

    def test_model_list_is_fetched_cleaned_and_searchable(self):
        server = FakeServer([])
        self.addCleanup(server.close)
        settings = {"id": "custom", "kind": "openai", "base_url": server.url, "api_key": "k", "model": "-"}

        models = list_models(settings)

        self.assertEqual(models, ["a-model", "b-model"])
        self.assertEqual(find_model(models, "b-m"), "b-model")
        self.assertIsNone(find_model(models, "model"))


class NetTest(unittest.TestCase):
    def test_unreachable_server_gives_readable_error(self):
        with self.assertRaises(net.ApiError) as caught:
            list(net.stream_events("http://127.0.0.1:1/x", {}, {}))

        self.assertIn("127.0.0.1:1", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
