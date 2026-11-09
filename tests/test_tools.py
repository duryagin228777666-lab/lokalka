"""Инструменты рабочей папки: файлы, поиск, команды, разрешения, отмена."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lokalka.tools import Workspace  # noqa: E402


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.asked = []
        self.answer = (True, "")
        self.ws = Workspace(self.root, self._confirm)

    def _confirm(self, title, detail, always_ask):
        self.asked.append((title, detail, always_ask))
        return self.answer

    def path(self, name):
        return os.path.join(self.root, name)

    def put(self, name, data):
        with open(self.path(name), "wb") as handle:
            handle.write(data)

    def raw(self, name):
        with open(self.path(name), "rb") as handle:
            return handle.read()


class FileToolsTest(WorkspaceCase):
    def test_write_creates_file_and_parent_folders(self):
        result = self.ws.execute("write_file", {"path": "src/a.py", "content": "print(1)\n"})

        self.assertIn("Wrote src/a.py", result)
        self.assertEqual(self.raw("src/a.py"), b"print(1)\n")

    def test_read_returns_numbered_lines_and_respects_offset(self):
        self.put("a.txt", b"one\ntwo\nthree")

        result = self.ws.execute("read_file", {"path": "a.txt", "offset": 2, "limit": 1})

        self.assertTrue(result.startswith("2\ttwo"))
        self.assertIn("lines 2-2 of 3", result)

    def test_edit_replaces_unique_fragment(self):
        self.put("a.py", b"x = 1\ny = 2\n")

        result = self.ws.execute("edit_file", {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3"})

        self.assertIn("Edited a.py", result)
        self.assertEqual(self.raw("a.py"), b"x = 1\ny = 3\n")

    def test_edit_refuses_ambiguous_fragment_without_replace_all(self):
        self.put("a.py", b"a\na\n")

        result = self.ws.execute("edit_file", {"path": "a.py", "old_string": "a", "new_string": "b"})

        self.assertIn("occurs 2 times", result)
        self.assertEqual(self.raw("a.py"), b"a\na\n")

    def test_edit_reports_missing_fragment(self):
        self.put("a.py", b"x = 1\n")

        result = self.ws.execute("edit_file", {"path": "a.py", "old_string": "nope", "new_string": "b"})

        self.assertTrue(result.startswith("ERROR:"))

    def test_edit_keeps_windows_line_endings_and_cp1251(self):
        self.put("old.py", "# привет\r\nx = 1\r\n".encode("cp1251"))

        self.ws.execute("edit_file", {"path": "old.py", "old_string": "# привет\nx = 1", "new_string": "# мир\nx = 2"})

        self.assertEqual(self.raw("old.py"), "# мир\r\nx = 2\r\n".encode("cp1251"))

    def test_undo_restores_previous_content_then_removes_created_file(self):
        self.ws.execute("write_file", {"path": "a.txt", "content": "v1"})
        self.ws.execute("write_file", {"path": "a.txt", "content": "v2"})

        self.ws.undo()
        self.assertEqual(self.raw("a.txt"), b"v1")
        self.ws.undo()
        self.assertFalse(os.path.exists(self.path("a.txt")))
        self.assertEqual(self.ws.undo(), "Отменять нечего.")

    def test_declined_write_changes_nothing_and_passes_comment_to_model(self):
        self.answer = (False, "назови файл main.py")

        result = self.ws.execute("write_file", {"path": "a.txt", "content": "x"})

        self.assertIn("declined", result)
        self.assertIn("назови файл main.py", result)
        self.assertFalse(os.path.exists(self.path("a.txt")))

    def test_path_outside_workspace_always_asks_and_can_be_refused(self):
        self.answer = (False, "")
        outside = os.path.join(os.path.dirname(self.root), "secret.txt")

        result = self.ws.execute("read_file", {"path": outside})

        self.assertTrue(result.startswith("ERROR:"))
        self.assertTrue(self.asked[0][2], "выход за рабочую папку должен спрашиваться даже в авторежиме")

    def test_parent_traversal_counts_as_outside(self):
        self.assertFalse(self.ws.is_inside(self.ws.resolve("../x.txt")))
        self.assertTrue(self.ws.is_inside(self.ws.resolve("sub/../x.txt")))

    def test_binary_file_is_not_read_as_text(self):
        self.put("img.bin", b"\x00\x01\x02")

        self.assertIn("binary", self.ws.execute("read_file", {"path": "img.bin"}))

    def test_bad_arguments_become_errors_not_crashes(self):
        self.assertTrue(self.ws.execute("read_file", {}).startswith("ERROR:"))
        self.assertTrue(self.ws.execute("read_file", {"path": 5}).startswith("ERROR:"))
        self.assertTrue(self.ws.execute("nope", {}).startswith("ERROR:"))
        self.assertIn("not valid JSON", self.ws.execute("write_file", {"_invalid_json": '{"path": "a'}))


class SearchToolsTest(WorkspaceCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.path("src"))
        os.makedirs(self.path("node_modules"))
        self.put("src/main.py", b"def solve():\n    return 42\n")
        self.put("notes.txt", b"solve later\n")
        self.put("node_modules/lib.py", b"def solve(): pass\n")

    def test_list_dir_puts_folders_first(self):
        lines = self.ws.execute("list_dir", {}).splitlines()

        self.assertEqual(lines[0], "node_modules/")
        self.assertIn("notes.txt", lines[-1])

    def test_find_files_matches_by_name_and_skips_junk_folders(self):
        self.assertEqual(self.ws.execute("find_files", {"pattern": "*.py"}), "src/main.py")

    def test_search_reports_file_and_line(self):
        result = self.ws.execute("search", {"pattern": r"def \w+", "glob": "*.py"})

        self.assertEqual(result, "src/main.py:1: def solve():")

    def test_search_rejects_bad_regex(self):
        self.assertIn("Bad regular expression", self.ws.execute("search", {"pattern": "("}))


class CommandToolTest(WorkspaceCase):
    def test_command_gets_stdin_and_reports_exit_code(self):
        self.put("echo.py", b"print(int(input()) * 2)\n")

        result = self.ws.execute("run_command", {"command": "python echo.py", "stdin": "21\n"})

        self.assertIn("42", result)
        self.assertIn("[exit code 0]", result)

    def test_program_waiting_for_input_does_not_hang(self):
        self.put("ask.py", b"input()\n")

        result = self.ws.execute("run_command", {"command": "python ask.py"})

        self.assertIn("EOFError", result)

    def test_timeout_stops_the_command(self):
        self.put("loop.py", b"import time\ntime.sleep(30)\n")

        result = self.ws.execute("run_command", {"command": "python loop.py", "timeout": 1})

        self.assertIn("[stopped", result)

    def test_cyrillic_output_survives(self):
        self.put("ru.py", "print('привет')\n".encode("utf-8"))

        self.assertIn("привет", self.ws.execute("run_command", {"command": "python ru.py"}))


if __name__ == "__main__":
    unittest.main()
