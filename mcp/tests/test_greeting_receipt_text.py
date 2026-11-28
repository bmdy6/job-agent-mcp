import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.platforms.greeting import same_text
from job_agent.platforms.boss import pages


class GreetingReceiptTextTest(unittest.TestCase):
    greeting = "您好，第一行：\n第二行；\n第三行。"

    def test_line_breaks_dropped_by_text_content_still_match(self):
        self.assertTrue(same_text("您好，第一行：第二行；第三行。", self.greeting))
        self.assertTrue(same_text("您好，第一行：\n第二行；\n第三行。 ", self.greeting))

    def test_different_text_or_empty_greeting_never_matches(self):
        self.assertFalse(same_text("您好，第一行：第二行。", self.greeting))
        self.assertFalse(same_text("", ""))
        self.assertFalse(same_text(None, self.greeting))

    def test_boss_receipt_accepts_multiline_greeting(self):
        row = dict(job_id="j", id="m1", direction="outgoing", card=False,
                   body="您好，第一行：第二行；第三行。", receipt="送达")
        self.assertTrue(pages.receipt({"rows": [row]}, self.greeting, "j", set()))
        self.assertFalse(pages.receipt({"rows": [dict(row, body="别的话")]}, self.greeting, "j", set()))


if __name__ == "__main__":
    unittest.main()
