import unittest
from browser.ego import as_expression


class AsExpressionTest(unittest.TestCase):
    def test_wraps_function_literals(self):
        self.assertEqual(as_expression("() => { return 1 }"), "(() => { return 1 })()")
        self.assertEqual(as_expression("async () => 1"), "(async () => 1)()")
        self.assertEqual(as_expression("function(){return 1}"), "(function(){return 1})()")

    def test_keeps_plain_expressions(self):
        self.assertEqual(as_expression("document.title"), "document.title")
        self.assertEqual(as_expression("JSON.stringify({a:1})"), "JSON.stringify({a:1})")
        self.assertEqual(as_expression("(1+2)"), "(1+2)")
