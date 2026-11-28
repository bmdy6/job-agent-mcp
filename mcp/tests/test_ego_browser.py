import json
import subprocess
import unittest
from unittest.mock import patch
from job_agent.browser.base import BrowserError, PageTimeout, SpaceUnavailable
from job_agent.browser.ego import EgoBrowser
from job_agent.engine.spaces import TaskSpaces


def result(value=None, *, code=None):
    payload = {'ok': True, 'value': value} if code is None else {'ok': False, 'code': code}
    return subprocess.CompletedProcess([], 0, '__JOB_AGENT_RESULT__' + json.dumps(payload) + '\n', '')


class EgoBrowserTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch('job_agent.browser.ego.subprocess.run').start()
        self.addCleanup(patch.stopall)
        self.browser = EgoBrowser(executable='/custom/ego-browser', keepalive_seconds=0, auto_launch=False)

    def request(self):
        script = self.mock.call_args.kwargs['input']
        return json.loads(script.split('const request = ', 1)[1].split(';\n', 1)[0])

    def space(self, sid=32):
        self.mock.return_value = result({'space_id': sid, 'anchor': 'p1'})
        return self.browser.space('test')

    def test_resolve_every_time_and_replace_deleted_space(self):
        a = self.space(32)
        b = self.space(33)
        self.assertEqual((a.space_id, b.space_id), (32, 33))
        self.assertEqual(self.request()['op'], 'space')
        self.assertEqual(self.mock.call_args.args[0], ['/custom/ego-browser', 'nodejs'])
        self.assertFalse(self.mock.call_args.kwargs.get('shell', False))

    def test_page_protocol_and_literal_arguments(self):
        space = self.space()
        self.mock.return_value = result({'label': 'p2'})
        page = space.new_page()
        self.mock.return_value = result()
        page.goto('https://example.com/?a="hi"', wait_ms=900)
        self.assertEqual(self.request()['args'], ['https://example.com/?a="hi"', {'timeout': 900, 'waitUntil': 'domcontentloaded'}])
        text = '中文\n` ${process.exit()} " \\ ' * 100
        page.fill('#input', text, newline='insert_text')
        self.assertEqual(self.request()['args'], ['#input', text, {'newline':'insert_text'}])
        page.press('Enter')
        self.assertEqual(self.request()['method'], 'keyboard.press')
        page.wait_for_url('https://example.com/chat', timeout_ms=1200)
        self.assertEqual(self.request()['method'], 'waitForURL')
        self.assertEqual(self.request()['args'], ['https://example.com/chat', {'timeout':1200}])
        page.click('#button')
        page.wait_for('#ready', timeout_ms=200)
        self.assertEqual(self.request()['args'], ['#ready', {'timeout': 200}])
        page.screenshot('/tmp/a.png')
        self.mock.return_value = result('https://example.com')
        self.assertEqual(page.url(), 'https://example.com')
        self.mock.return_value = result({'a': [1, None, '中']})
        self.assertEqual(page.evaluate('({a: [1, null, "中"]})'), {'a': [1, None, '中']})
        self.mock.return_value = result('tree')
        self.assertEqual(page.snapshot(), 'tree')
        self.mock.return_value = result()
        page.close()
        calls = self.mock.call_count
        page.close()
        self.assertEqual(calls, self.mock.call_count)
        with self.assertRaises(BrowserError):
            page.url()

    def test_shutdown_keeps_anchor_and_is_idempotent(self):
        self.space()
        self.mock.return_value = result()
        self.browser.shutdown('test')
        self.assertEqual(self.request()['anchor'], 'p1')
        self.assertEqual(self.request()['op'], 'shutdown')
        calls = self.mock.call_count
        self.browser.shutdown('test')
        self.browser.shutdown('unknown')
        self.assertEqual(calls, self.mock.call_count)

    def test_one_shot_shutdown_explicitly_finishes_and_is_idempotent(self):
        self.space()
        self.mock.return_value=result()
        self.browser.shutdown('test',finish=True)
        self.assertTrue(self.request()['finish'])
        calls=self.mock.call_count
        self.browser.shutdown('test',finish=True)
        self.assertEqual(self.mock.call_count,calls)

    def test_error_mapping(self):
        for code, cls in [('space_unavailable', SpaceUnavailable), ('page_timeout', PageTimeout), ('browser_error', BrowserError)]:
            with self.subTest(code=code):
                self.browser = EgoBrowser('/custom/ego-browser', keepalive_seconds=0)
                self.mock.return_value = result(code=code)
                with self.assertRaises(cls):
                    self.browser.space('test')

    def test_timeout_reports_operation_without_request_arguments(self):
        space=self.space()
        self.mock.return_value=result({'label':'p2'})
        page=space.new_page()
        self.mock.return_value=result(code='page_timeout')
        with self.assertRaisesRegex(PageTimeout,'waitForURL') as error:
            page.wait_for_url('https://example.com/private-target')
        self.assertNotIn('private-target',str(error.exception))

    def test_inspected_page_cannot_act_until_new_owned_page_is_claimed(self):
        space=self.space()
        self.mock.return_value=result([{'label':'p2','url':'https://example.test/chat'}])
        page=space.inspect_page('p2')
        calls=self.mock.call_count
        with self.assertRaises(BrowserError):
            page.fill('#input','text')
        self.assertEqual(self.mock.call_count,calls)
        with self.assertRaises(SpaceUnavailable):
            space.claim_page(page,{'p2'})
        self.mock.return_value=result([{'label':'p2','url':'https://example.test/chat'}])
        space.claim_page(page,set())
        self.mock.return_value=result()
        page.fill('#input','text')
        self.assertEqual(self.request()['method'],'fill')

    def test_process_failures_never_retry(self):
        for failure in [subprocess.TimeoutExpired('ego', 1), OSError('missing')]:
            self.mock.reset_mock()
            self.mock.side_effect = failure
            with self.assertRaises(BrowserError):
                self.browser.space('test')
            self.assertEqual(self.mock.call_count, 1)

    def test_bad_response_does_not_expose_output(self):
        for response in [subprocess.CompletedProcess([], 1, '', 'private'), subprocess.CompletedProcess([], 0, 'private', ''), subprocess.CompletedProcess([], 0, '__JOB_AGENT_RESULT__broken', ''), result(['wrong shape']), result(None)]:
            self.mock.return_value = response
            with self.assertRaises(BrowserError) as ctx:
                self.browser.space('test')
            self.assertNotIn('private', str(ctx.exception))

    def test_task_spaces_bridge_preserves_name_handle(self):
        spaces = TaskSpaces(self.browser)
        self.space()
        self.assertEqual(spaces.ensure('test'), 'test')
        self.mock.return_value = result()
        spaces.shutdown('test')
        self.assertEqual(self.request()['op'], 'shutdown')

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            self.browser.space('  ')
        self.mock.assert_not_called()

    def test_cli_console_output_on_stderr(self):
        response = result({'space_id': 32, 'anchor': 'p1'})
        response.stdout, response.stderr = '', response.stdout
        self.mock.return_value = response
        self.assertEqual(self.browser.space('test').space_id, 32)

    def test_shutdown_rejection_preserves_state_for_explicit_later_action(self):
        self.space()
        self.mock.return_value = result(code='space_unavailable')
        with self.assertRaises(SpaceUnavailable):
            self.browser.shutdown('test')
        self.assertIn('test', self.browser._spaces)
        self.assertEqual(self.mock.call_count, 2)

    def test_existing_page_handles_never_resolve_by_name(self):
        space = self.space()
        self.mock.return_value = result({'label': 'p2'})
        page = space.new_page()
        self.mock.return_value = result(code='space_unavailable')
        with self.assertRaises(SpaceUnavailable):
            page.click('#send')
        request = self.request()
        self.assertNotIn('name', request)
        self.assertEqual(request['space_id'], 32)
        self.assertEqual(self.mock.call_count, 3)

    def test_unknown_outcome_close_is_not_marked_successful(self):
        space = self.space()
        self.mock.return_value = result({'label': 'p2'})
        page = space.new_page()
        self.mock.side_effect = subprocess.TimeoutExpired('ego', 30)
        with self.assertRaises(PageTimeout):
            page.close()
        self.assertFalse(page._closed)
        self.assertEqual(self.mock.call_count, 3)

    def test_keepalive_recreates_by_name_without_replaying_old_page(self):
        events = []
        self.browser.on_event = lambda *args: events.append(args)
        old = self.space(32)
        self.mock.return_value = result({'label': 'p2'})
        page = old.new_page()
        self.mock.return_value = result({'space_id': 33, 'anchor': 'p1'})
        self.browser.keepalive_once()
        self.assertEqual(self.request(), {'op': 'keepalive', 'name': 'test', 'anchor': 'p1'})
        self.assertEqual(self.browser._spaces['test'].space_id, 33)
        self.assertEqual(events, [('test', 'recreated')])
        self.mock.return_value = result({'label': 'p2'})
        fresh = old.new_page()
        self.assertEqual(self.request()['space_id'], 33)
        self.assertEqual(fresh._space.space_id, 33)
        self.mock.return_value = result(code='space_unavailable')
        with self.assertRaises(SpaceUnavailable):
            page.click('#send')
        self.assertEqual(self.request()['space_id'], 32)

    def test_keepalive_handover_stops_future_resolution(self):
        events = []
        self.browser.on_event = lambda *args: events.append(args)
        space = self.space()
        self.mock.return_value = result({'label': 'p2'})
        page = space.new_page()
        self.mock.return_value = result(code='space_unavailable')
        self.browser.keepalive_once()
        calls = self.mock.call_count
        self.browser.keepalive_once()
        with self.assertRaises(SpaceUnavailable):
            self.browser.space('test')
        with self.assertRaises(SpaceUnavailable):
            page.evaluate('1')
        with self.assertRaises(SpaceUnavailable):
            space.new_page()
        self.browser.shutdown('test')
        self.assertEqual(self.mock.call_count, calls)
        self.assertEqual(events, [('test', 'unavailable')])

    def test_keepalive_timeout_is_bounded_and_alert_failure_does_not_escape(self):
        self.space()
        events = []
        self.browser.on_event = lambda *args: events.append(args)
        self.mock.side_effect = subprocess.TimeoutExpired('ego', 30)
        self.browser.keepalive_once()
        self.browser.keepalive_once()
        self.assertEqual(events, [('test', 'keepalive_failed')])
        self.assertEqual(self.mock.call_count, 3)
        self.mock.side_effect = None
        self.mock.return_value = result({'space_id': 33, 'anchor': 'p1'})
        self.browser.on_event = lambda *args: (_ for _ in ()).throw(RuntimeError('private'))
        self.browser.keepalive_once()
        self.assertEqual(self.browser._spaces['test'].space_id, 33)

    def test_worker_starts_for_owned_space_and_stops_before_shutdown(self):
        import threading
        browser = EgoBrowser('/fake', keepalive_seconds=0.01)
        self.addCleanup(browser.stop_keepalive)
        fired = threading.Event()
        self.mock.return_value = result({'space_id': 32, 'anchor': 'p1'})
        with patch.object(browser, 'keepalive_once', side_effect=fired.set):
            browser.space('worker')
            self.assertTrue(fired.wait(1))
            browser.shutdown('worker')
            self.assertFalse(browser._thread.is_alive())
            fired.clear()
            browser.space('worker')
            self.assertTrue(fired.wait(1), 'A new space lifetime must restart its worker')
            browser.shutdown('worker')
            self.assertFalse(browser._thread.is_alive())

    def test_invalid_keepalive_interval(self):
        for value in [-1, float('inf'), float('nan'), True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EgoBrowser(keepalive_seconds=value)

    def test_foreground_and_heartbeat_commands_are_serialized(self):
        import threading
        self.space()
        entered, release = threading.Event(), threading.Event()
        active, peak = [0], [0]
        errors = []
        def execute(request, **kwargs):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            try:
                if request['op'] == 'page':
                    entered.set()
                    if not release.wait(1):
                        raise AssertionError('Foreground release timed out')
                return {'space_id': 32, 'anchor': 'p1'}
            finally:
                active[0] -= 1
        def foreground():
            try:
                self.browser._call({'op': 'page'})
            except Exception as error:
                errors.append(error)
        with patch.object(self.browser, '_execute', side_effect=execute):
            first = threading.Thread(target=foreground)
            second = threading.Thread(target=self.browser.keepalive_once)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
            finally:
                release.set()
                first.join(1)
                if second.ident is not None:
                    second.join(1)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peak[0], 1)
