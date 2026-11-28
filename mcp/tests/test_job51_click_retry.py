import unittest
from types import SimpleNamespace
from job_agent.browser.base import BrowserError, RiskControl, SpaceUnavailable
from job_agent.platforms.job51 import pages
from job_agent.platforms.job51.apply import Job51Adapter


class Page:
    def __init__(self, errors=(), prepared=True):
        self.errors, self.clicks, self.evaluations = list(errors), [], []
        self.prepared = prepared

    def evaluate(self, script):
        self.evaluations.append(script)
        if script == pages.OBSERVE_JS:
            return True
        if 'job51.prepare_apply' in script:
            if isinstance(self.prepared, Exception):
                raise self.prepared
            return self.prepared
        return {}

    def click(self, selector):
        self.clicks.append(selector)
        if self.errors:
            raise self.errors.pop(0)


def adapter(page):
    a = Job51Adapter(None, SimpleNamespace(greeting_max_length=10))
    a.page, a.job = page, SimpleNamespace(job_id='1')
    a.check_applied = lambda: False
    return a


GEOMETRY = BrowserError('Ego click.target_geometry failed: browser_error')


class Job51ClickRetryTest(unittest.TestCase):
    def test_prepare_before_click_and_never_retry_arbitrary_first_match(self):
        page = Page([GEOMETRY])
        a = adapter(page)
        a.send_greeting('')
        self.assertEqual(page.clicks, [pages.APPLY, pages.APPLY])
        self.assertEqual(page.evaluations.count(pages.PREPARE_APPLY_JS), 2)
        self.assertTrue(a._attempted)

    def test_repeated_pre_click_failure_is_not_started(self):
        a = adapter(Page([GEOMETRY, GEOMETRY]))
        with self.assertRaises(pages.NotStarted) as ctx:
            a.send_greeting('')
        self.assertEqual(ctx.exception.code, 'not_started')
        self.assertFalse(a._attempted)

    def test_unprepared_or_ambiguous_target_never_clicks(self):
        page = Page(prepared=False)
        a = adapter(page)
        with self.assertRaises(pages.NotStarted):
            a.send_greeting('')
        self.assertEqual(page.clicks, [])
        self.assertFalse(a._attempted)

    def test_read_only_preparation_error_is_not_started(self):
        a = adapter(Page(prepared=BrowserError('Ego evaluate failed: browser_error')))
        with self.assertRaises(pages.NotStarted):
            a.send_greeting('')
        self.assertFalse(a._attempted)
        self.assertEqual(a.page.clicks, [])

    def test_preparation_preserves_risk_and_space_errors(self):
        for error in (RiskControl('risk'), SpaceUnavailable('user control')):
            with self.subTest(error=type(error).__name__):
                a = adapter(Page(prepared=error))
                with self.assertRaises(type(error)):
                    a.send_greeting('')
                self.assertEqual(a.page.clicks, [])
                self.assertFalse(a._attempted)

    def test_applied_during_preparation_prevents_click(self):
        page = Page()
        a = adapter(page)
        states = iter([False, True])
        a.check_applied = lambda: next(states)
        with self.assertRaises(pages.Unverified):
            a.send_greeting('')
        self.assertEqual(page.clicks, [])
        self.assertFalse(a._attempted)

    def test_safety_error_at_geometry_is_preserved_without_retry(self):
        for cls in (RiskControl, SpaceUnavailable):
            with self.subTest(error=cls.__name__):
                a = adapter(Page([cls('Ego click.target_geometry failed: safety')]))
                with self.assertRaises(cls):
                    a.send_greeting('')
                self.assertEqual(a.page.clicks, [pages.APPLY])

    def test_failure_after_mouse_down_stays_unknown(self):
        page = Page([BrowserError('Ego click.mouse.up failed: browser_error')])
        a = adapter(page)
        with self.assertRaises(BrowserError):
            a.send_greeting('')
        self.assertEqual(len(page.clicks), 1)
        self.assertTrue(a._attempted)
        with self.assertRaises(pages.Unverified):
            a.send_greeting('')
        self.assertEqual(len(page.clicks), 1)


if __name__ == '__main__':
    unittest.main()
