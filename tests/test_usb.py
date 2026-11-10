"""Работа на флешке: выбор носителя, папки и подтверждение пользователя."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lokalka import usb  # noqa: E402
from lokalka.usb import Drive  # noqa: E402


class ChooseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stick = os.path.join(tmp.name, "stick")
        self.other = os.path.join(tmp.name, "other")
        self.app = os.path.join(self.stick, "lokalka-app")
        for path in (self.app, self.other):
            os.makedirs(path)
        self.said = []

    def run_choose(self, drives, *answers):
        replies = list(answers)
        return usb.choose(drives, lambda _: replies.pop(0), self.said.append, app_dir=self.app)

    def test_no_stick_found_explains_what_to_do(self):
        self.assertEqual(self.run_choose([]), "")
        self.assertIn("не найдена", self.said[0])

    def test_stick_with_program_uses_folder_next_to_it(self):
        target = self.run_choose([Drive(self.stick, "USB", 3.0)], "")

        self.assertEqual(target, os.path.join(self.app, "workspace"))

    def test_other_stick_uses_workspace_in_its_root(self):
        target = self.run_choose([Drive(self.other, "", 1.0)], "да")

        self.assertEqual(target, os.path.join(self.other, "workspace"))

    def test_user_can_decline(self):
        self.assertEqual(self.run_choose([Drive(self.stick, "USB", 3.0)], "n"), "")

    def test_user_can_type_own_folder_but_not_leave_the_stick(self):
        drive = Drive(self.other, "", 1.0)

        self.assertEqual(self.run_choose([drive], "задачи"), os.path.join(self.other, "задачи"))
        self.assertEqual(self.run_choose([drive], "../../escape"), "")

    def test_several_sticks_offer_the_one_with_program_first(self):
        drives = [Drive(self.other, "A", 1.0), Drive(self.stick, "B", 2.0)]

        by_default = self.run_choose(drives, "", "")
        picked = self.run_choose(drives, "1", "")
        wrong = self.run_choose(drives, "7")

        self.assertEqual(by_default, os.path.join(self.app, "workspace"))
        self.assertEqual(picked, os.path.join(self.other, "workspace"))
        self.assertEqual(wrong, "")
        self.assertTrue(any("здесь программа" in line for line in self.said))

    def test_real_detection_never_crashes(self):
        self.assertIsInstance(usb.find_drives(), list)


if __name__ == "__main__":
    unittest.main()
