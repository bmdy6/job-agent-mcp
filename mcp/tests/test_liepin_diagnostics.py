import unittest
from unittest.mock import Mock
from browser.base import BrowserError, FakePage, FakeSpace, PageTimeout
from platforms.liepin import pages
from platforms.liepin.replies import ReplySource

class LiepinDiagnosticsTests(unittest.TestCase):
    def page(self):
        return FakePage('p2', {'liepin.guard': {},
            'liepin.contacts': [{'id':'c','index':0,'lastMsgId':'m','unreadCount':1}],
            'liepin.select_contact': True, 'liepin.messages': {'ready':True,'items':[]}})

    def test_stages_and_privacy(self):
        for method, stage in [('new_page','open_page'),('goto','navigate'),('click','open_im'),
                              ('click_projected','click_conversation'),('close','close_page')]:
            with self.subTest(stage=stage):
                p=self.page(); space=FakeSpace('test',pages=[p]); cursor=Mock()
                setattr(space if method=='new_page' else p,method,Mock(side_effect=PageTimeout('private body https://secret/name')))
                with self.assertLogs('platforms.liepin.replies',level='INFO') as logs:
                    with self.assertRaises(PageTimeout) as caught: ReplySource().fetch(space,cursor=cursor)
                self.assertEqual(caught.exception.stage,stage)
                self.assertEqual(caught.exception.reason_code,'page_timeout')
                self.assertNotIn('private body',str(logs.output))

    def test_wait_and_read_stages(self):
        for selector,stage in [(pages.ENTRY,'wait_im_entry'),(pages.CONTACTS,'wait_contacts'),(pages.EDITOR,'wait_editor')]:
            p=self.page()
            def wait(value,**kwargs):
                if value==selector: raise PageTimeout()
            p.wait_for=wait
            with self.assertRaises(PageTimeout) as caught: ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
            self.assertEqual(caught.exception.stage,stage)
        for marker,stage in [('liepin.contacts','read_contacts'),('liepin.messages','read_messages')]:
            p=self.page(); original=p.evaluate
            def evaluate(js):
                if marker in js: raise BrowserError()
                return original(js)
            p.evaluate=evaluate
            with self.assertRaises(BrowserError) as caught: ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
            self.assertEqual(caught.exception.stage,stage)

    def test_summary_failure(self):
        p=self.page(); p.responses['liepin.contacts'][0].pop('lastMsgId')
        with self.assertRaises(pages.Unverified) as caught: ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
        self.assertEqual(caught.exception.stage,'read_summary')
        self.assertEqual(caught.exception.reason_code,'summary_field_missing:latestMsgId')

    def test_cleanup_preserves_primary_and_guard_preserves_code(self):
        p=self.page(); p.goto=Mock(side_effect=PageTimeout()); p.close=Mock(side_effect=BrowserError())
        with self.assertRaises(PageTimeout) as caught: ReplySource().fetch(FakeSpace('test',pages=[p]))
        self.assertEqual(caught.exception.stage,'navigate')
        for state,code in [({'risk':True},'risk_control'),({'login':True},'login_required')]:
            p=self.page(); p.responses['liepin.guard']=state
            with self.assertRaises(pages.Unverified) as caught: ReplySource().fetch(FakeSpace('test',pages=[p]))
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(caught.exception.stage,'guard_page')

    def test_browser_substep_and_location_failure(self):
        p=self.page()
        p.click_projected=Mock(side_effect=BrowserError('Ego unknown.target_geometry failed: browser_error'))
        with self.assertRaises(BrowserError) as caught:
            ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
        self.assertEqual(caught.exception.reason_code,'browser_target_geometry')
        p=self.page(); p.responses['liepin.contacts'][0]['id']=None
        with self.assertRaises(pages.Unverified) as caught:
            ReplySource().fetch(FakeSpace('test',pages=[p]))
        self.assertEqual(caught.exception.stage,'locate_conversation')

    def test_im_and_conversation_guards_and_bad_list(self):
        for nth,stage in [(2,'guard_im'),(3,'guard_conversation')]:
            p=self.page(); original=p.evaluate; calls=0
            def evaluate(js):
                nonlocal calls
                if 'liepin.guard' in js:
                    calls+=1
                    if calls==nth: return {'risk':True}
                return original(js)
            p.evaluate=evaluate
            with self.assertRaises(pages.RiskControl) as caught:
                ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
            self.assertEqual(caught.exception.stage,stage)
        for contacts in [None, ['private invalid row']]:
            p=self.page(); p.responses['liepin.contacts']=contacts
            with self.assertRaises(pages.Unverified) as caught:
                ReplySource().fetch(FakeSpace('test',pages=[p]),cursor=Mock())
            self.assertEqual(caught.exception.stage,'read_contacts')
            self.assertEqual(caught.exception.reason_code,'contacts_unreadable')
