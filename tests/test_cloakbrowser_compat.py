import unittest

from core.cloakbrowser_driver import CloakElement


class _Locator:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def evaluate(self, expression, arg=None, timeout=None):
        self.calls.append((expression, arg, timeout))
        return self.value


class CloakElementCompatibilityTests(unittest.TestCase):
    def test_text_reads_inner_text_from_locator(self):
        locator = _Locator(" Resend code ")
        element = CloakElement(page=object(), locator=locator)

        self.assertEqual(element.text, " Resend code ")
        self.assertEqual(locator.calls[0][2], 3000)

    def test_text_reads_value_from_handle(self):
        class _Handle:
            def evaluate(self, expression, arg=None):
                return "Submit"

        element = CloakElement(page=object(), handle=_Handle())
        self.assertEqual(element.text, "Submit")


if __name__ == "__main__":
    unittest.main()
