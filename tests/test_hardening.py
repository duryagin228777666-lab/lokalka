"""Защита от хитрого вывода модели, утечки ключа и кривых данных от провайдера."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lokalka import config  # noqa: E402
from lokalka.agent import OMITTED_RESULT, trim_history  # noqa: E402
from lokalka.providers import consume_openai, to_openai_messages  # noqa: E402
from lokalka.shell import _child_env  # noqa: E402
from lokalka.tools import Workspace  # noqa: E402
from lokalka.ui import Console, clean  # noqa: E402


def ask(console, answer, *args):
    """Вызвать confirm с заданным ответом пользователя; вернуть (результат, что было на экране)."""
    screen = io.StringIO()
    with mock.patch("builtins.input", side_effect=[answer]), mock.patch("sys.stdout", screen):
        result = console.confirm(*args)
    return result, screen.getvalue()


class ConsoleTest(unittest.TestCase):
    def test_control_characters_are_neutralised(self):
        self.assertEqual(clean("ok\x1b[8m hidden\rover\x07\x9b"), "ok?[8m hidden?over??")
        self.assertEqual(clean("a\r\nb\tc"), "a\nb\tc")

    def test_confirmation_shows_the_whole_command(self):
        command = "echo ok" + " " * 150 + "& del /q *"

        (approved, _), screen = ask(Console(), "", "Выполнить команду: " + command, "", False)

        self.assertTrue(approved)
        self.assertIn("& del /q *", screen)

    def test_outside_folder_prompt_needs_explicit_yes_and_never_enables_auto(self):
        console = Console()

        (by_enter, _), _ = ask(console, "", "Чтение ВНЕ рабочей папки", "", True)
        (by_always, _), _ = ask(console, "a", "Чтение ВНЕ рабочей папки", "", True)
        (by_yes, _), _ = ask(console, "y", "Чтение ВНЕ рабочей папки", "", True)

        self.assertEqual((by_enter, by_always, by_yes), (False, False, True))
        self.assertFalse(console.auto_approve)

    def test_auto_mode_still_asks_about_outside_paths(self):
        console = Console(auto_approve=True)

        self.assertEqual(console.confirm("Изменить файл a.py", "", False), (True, ""))
        (approved, _), _ = ask(console, "n", "Чтение ВНЕ рабочей папки", "", True)
        self.assertFalse(approved)


class SecretsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.asked = []
        self.ws = Workspace(self.root, self._confirm)

    def _confirm(self, title, detail, always_ask):
        self.asked.append(always_ask)
        return False, ""

    def test_settings_file_inside_workspace_is_guarded_and_hidden_from_search(self):
        settings = os.path.join(self.root, "config.json")
        with open(settings, "w", encoding="utf-8") as handle:
            handle.write('{"api_key": "sk-secret"}')
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: settings}):
            read = self.ws.execute("read_file", {"path": "config.json"})
            found = self.ws.execute("search", {"pattern": "sk-secret"})
            listed = self.ws.execute("find_files", {"pattern": "*.json"})

        self.assertTrue(read.startswith("ERROR:"))
        self.assertEqual(self.asked, [True])
        self.assertEqual((found, listed), ("No matches.", "No files match."))

    def test_search_does_not_follow_links_out_of_the_workspace(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        with open(os.path.join(outside.name, "s.txt"), "w", encoding="utf-8") as handle:
            handle.write("TOPSECRET")
        link = os.path.join(self.root, "j")
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside.name],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if made.returncode != 0:
                self.skipTest("не удалось создать соединение папок")
            self.addCleanup(os.rmdir, link)
        else:
            os.symlink(outside.name, link)

        self.assertEqual(self.ws.execute("search", {"pattern": "TOPSECRET"}), "No matches.")
        self.assertEqual(self.ws.execute("find_files", {"pattern": "s.txt"}), "No files match.")

    def test_api_keys_are_not_passed_to_commands(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k", "HARMLESS": "1"}):
            env = _child_env()

        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertEqual(env["HARMLESS"], "1")

    def test_plain_http_is_allowed_only_for_local_addresses(self):
        for good in ("http://localhost:11434/v1", "http://192.168.1.5:8000", "https://example.com/v1/"):
            config.validate_base_url(good)
        for bad in ("http://example.com/v1", "ftp://x", "example.com"):
            with self.assertRaises(config.ConfigError):
                config.validate_base_url(bad)


class ModelQuirksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.ws = Workspace(self.root, lambda *_: (True, ""))

    def test_null_for_optional_argument_means_default(self):
        self.assertEqual(self.ws.execute("list_dir", {"path": None}), "(empty folder)")
        self.assertIn("[exit code 0]", self.ws.execute(
            "run_command", {"command": "python -c \"print(1)\"", "stdin": None, "timeout": None}))

    def test_absurd_arguments_are_reported_not_raised(self):
        self.assertTrue(self.ws.execute("run_command", {"command": "x", "timeout": float("inf")}).startswith("ERROR:"))
        self.assertIn("Wrote", self.ws.execute("write_file", {"path": "a.txt", "content": "bad \ud800 char"}))

    def test_parallel_calls_without_ids_on_one_index_stay_separate(self):
        events = [{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "read_file", "arguments": '{"path": "a"}'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "read_file", "arguments": '{"path": "b"}'}}]}}]}]

        reply = consume_openai(events, lambda _: None)

        self.assertEqual([call["arguments"] for call in reply["tool_calls"]], [{"path": "a"}, {"path": "b"}])
        self.assertEqual(len({call["id"] for call in reply["tool_calls"]}), 2)

    def test_empty_assistant_reply_gets_placeholder_text(self):
        converted = to_openai_messages("s", [{"role": "user", "content": "q"},
                                             {"role": "assistant", "content": "", "tool_calls": []}])

        self.assertTrue(converted[2]["content"])

    def test_one_huge_turn_is_shrunk_by_dropping_old_tool_output(self):
        history = [{"role": "user", "content": "q"}]
        for number in range(10):
            call_id = str(number)
            history.append({"role": "assistant", "content": "", "blocks": [1],
                            "tool_calls": [{"id": call_id, "name": "read_file", "arguments": {}}]})
            history.append({"role": "tool", "tool_call_id": call_id, "content": "x" * 1000})

        trimmed = trim_history(history, limit=8000)

        contents = [message["content"] for message in trimmed if message["role"] == "tool"]
        self.assertEqual(len(trimmed), len(history))
        self.assertEqual(contents[0], OMITTED_RESULT)
        self.assertEqual(contents[-1], "x" * 1000)
        self.assertFalse(any("blocks" in message for message in trimmed))


if __name__ == "__main__":
    unittest.main()
