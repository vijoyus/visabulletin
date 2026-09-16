import unittest

from visa_bulletin_monitor import movement


class TestMovement(unittest.TestCase):
    def test_no_change(self):
        self.assertEqual(movement("01JAN25", "01JAN25"), "NO CHANGE")

    def test_advance(self):
        self.assertEqual(
            movement("01JAN25", "01FEB25"),
            "+31 days (01JAN25 -> 01FEB25)",
        )

    def test_current_transition(self):
        self.assertEqual(movement("15JAN25", "C"), "15JAN25 -> C")

    def test_unavailable_transition(self):
        self.assertEqual(movement("15JAN25", "U"), "15JAN25 -> U")


if __name__ == "__main__":
    unittest.main()
