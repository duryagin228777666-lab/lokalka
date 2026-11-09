"""Второй мозг: граф заметок, связи, поиск, сохранение между запусками."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lokalka.brain import Brain  # noqa: E402
from lokalka.tools import Workspace  # noqa: E402


class BrainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(tmp.name, "brain.json")
        self.brain = Brain(self.path)

    def test_links_go_both_ways_and_create_hub_notes(self):
        self.brain.save("Задача про факториал", "решена рекурсией", ["Рекурсия"], ["алгоритмы"])

        hub = self.brain.read("рекурсия")

        self.assertIn("Задача про факториал", hub)
        self.assertIn("Заметок: 2, связей: 1", self.brain.map())

    def test_memory_survives_restart(self):
        self.brain.save("Преподаватель", "просит комментарии на русском", [], [])

        again = Brain(self.path)

        self.assertIn("комментарии на русском", again.read("преподаватель"))

    def test_search_ranks_title_matches_first(self):
        self.brain.save("Сортировка пузырьком", "вложенные циклы", [], [])
        self.brain.save("Списки", "сортировка встроенной sorted запрещена", [], [])

        found = self.brain.search("сортировка")

        self.assertEqual(found[0]["title"], "Сортировка пузырьком")
        self.assertEqual(len(found), 2)

    def test_update_keeps_one_note_and_forget_removes_links(self):
        self.brain.save("Python", "v1", ["Курс"], [])
        self.brain.save("python", "v2", [], [])

        self.assertEqual(len(self.brain.notes), 2)
        self.assertIn("v2", self.brain.read("Python"))
        self.assertTrue(self.brain.forget("Python"))
        self.assertEqual(self.brain.notes["курс"]["links"], [])

    def test_broken_file_starts_empty_instead_of_crashing(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{oops")

        self.assertEqual(Brain(self.path).notes, {})

    def test_agent_tools_use_the_graph(self):
        ws = Workspace(self.dir, lambda *_: (True, ""), self.brain)

        saved = ws.execute("memory_save", {"title": "Курс", "text": "1 курс, Python", "links": "Python"})
        found = ws.execute("memory_search", {"query": "курс"})
        missing = ws.execute("memory_read", {"title": "нет такой"})

        self.assertIn("Saved note", saved)
        self.assertIn("1 курс, Python", found)
        self.assertTrue(missing.startswith("ERROR:"))
        self.assertTrue(ws.execute("memory_save", {"title": " ", "text": "x"}).startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
