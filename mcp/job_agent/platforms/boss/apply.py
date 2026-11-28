from job_agent.platforms.availability import wait_job_ready
"""Observed first-contact transitions; never follow hidden redirects or replay actions."""
from time import monotonic, sleep, time
from job_agent.browser.base import Browser, Page, PageTimeout
from job_agent.engine.models import Job
from job_agent.platforms.boss import pages
from job_agent.platforms.greeting import prepare_greeting


class BossAdapter:
    platform = 'boss'
    greeting_mode = 'send_message'

    def __init__(self, browser: Browser, settings):
        self.browser, self.settings = browser, settings
        self.delivery_kind = 'attempt'
        self.page: Page | None = None
        self.space = None
        self.job = None
        self.greeting = None
        self.before = set()
        self.target = None
        self.conversation_id = None
        self.attempted_at = None
        self.contact_receipt = False
        self.modal_submitted = False
        self.message_submitted = False
        self.attempted = False
        self.attachment_requested = False

    def bind_delivery_kind(self, kind):
        if kind not in {'attempt','followup'}:
            raise ValueError('Invalid delivery kind')
        self.delivery_kind = kind

    def _setting(self, key, default):
        return self.settings.get(key, default) if isinstance(self.settings, dict) else getattr(self.settings, key, default)

    def open_job(self, job: Job, task_space: str) -> None:
        if self.page is not None:
            raise pages.PlatformStateError('先关闭上一个岗位页面')
        if job.platform != self.platform or pages.job_id(job.url) != job.job_id:
            raise ValueError('岗位平台或 ID 与 URL 不一致')
        self.delivery_kind = 'attempt'
        self.job, self.greeting = job, None
        self.before, self.attempted, self.attachment_requested = set(), False, False
        self.target,self.conversation_id,self.attempted_at=None,None,None
        self.contact_receipt=False
        self.modal_submitted=False
        self.message_submitted=False
        self.space=self.browser.space(task_space)
        self.page = self.space.new_page()
        try:
            self.page.goto(job.url, wait_ms=15000)
            self._guard()
            wait_job_ready(self.page, pages.JOB_READY, state_js=pages.STATE_JS)
            self._guard()
            if pages.job_id(self.page.url()) != job.job_id:
                raise pages.PlatformStateError('岗位跳转后 ID 不一致')
        except Exception:
            self.close()
            raise

    def _guard(self):
        if self.page is None:
            raise pages.PlatformStateError('没有打开岗位')
        pages.guard(self.page.evaluate(pages.STATE_JS))

    def check_applied(self) -> bool:
        self._guard()
        if pages.job_id(self.page.url()) != self.job.job_id:
            raise pages.PlatformStateError('页面已不是目标岗位')
        return pages.applied(self.page.evaluate(pages.STATE_JS))

    def _editable_contact_state(self):
        self._guard()
        try:
            same_job=pages.job_id(self.page.url())==self.job.job_id
        except ValueError:
            same_job=False
        if not same_job or self.page.evaluate(pages.JOB_IDENTITY_JS)!=self.target:
            raise pages.PlatformStateError('首次沟通表单所属岗位变化；不提交')
        state=self.page.evaluate(pages.CONTACT_STATE_JS)
        if (not isinstance(state,dict) or state.get('kind')!='editable_contact'
                or state.get('input_ready') is not True):
            raise pages.PlatformStateError('首次沟通编辑框不可核验')
        return state

    def _submit_editable_contact(self, prior):
        """Ordinary visible form, one submit; never fall through to a second send."""
        if self.modal_submitted:
            raise pages.PlatformStateError('首次沟通弹窗已尝试提交')
        self._editable_contact_state()
        if any(t.label not in prior and pages.is_chat_url(t.url) for t in self.space.tabs()):
            raise pages.PlatformStateError('首次沟通同时出现新聊天页；不确认弹窗')
        if '\n' in self.greeting:
            self.page.fill(pages.CONTACT_INPUT,self.greeting,newline=pages.CONTACT_NEWLINE)
        else:
            self.page.fill(pages.CONTACT_INPUT,self.greeting)
        # Re-read the whole form and exact target after the ordinary input.
        state=self._editable_contact_state()
        selector=pages.contact_submit_selector(state,self.greeting)
        if any(t.label not in prior and pages.is_chat_url(t.url) for t in self.space.tabs()):
            raise pages.PlatformStateError('确认前出现新聊天页；不提交弹窗')
        self.modal_submitted=True
        self.page.click(selector)

    def _observe_contact_chat(self, prior):
        """Observe only normal same/new owned chat tabs after known user controls.

        An editable form can be submitted once through its verified visible
        input and button. Other dialogs and unresolved states are not replayed.
        """
        deadline=monotonic()+10
        while True:
            self._guard()
            tabs=self.space.tabs()
            if self.page.label not in {t.label for t in tabs}:
                raise pages.PlatformStateError('首次沟通原页不可核验')
            if not pages.is_chat_url(self.page.url()):
                state=self.page.evaluate(pages.CONTACT_STATE_JS)
                kind=state.get('kind') if isinstance(state,dict) else 'unknown'
                if kind=='editable_contact':
                    if not self.modal_submitted:
                        self._submit_editable_contact(prior)
                        deadline=monotonic()+10
                        continue
                    # A submit that has not navigated yet is observed, never pressed again.
                elif kind!='none':
                    if self.modal_submitted:
                        # Observe the receipt separately; do not act on a subsequent dialog.
                        return
                    if kind not in {'preset_greeting','unknown_dialog'}:kind='unknown'
                    raise pages.PlatformStateError(f'首次沟通弹窗需确认（{kind}）；不自动确认或重放')
            matches=[]
            for tab in tabs:
                if not pages.is_chat_url(tab.url) or (tab.label!=self.page.label and tab.label in prior):
                    continue
                candidate=self.page if tab.label==self.page.label else self.space.inspect_page(tab.label)
                pages.guard(candidate.evaluate(pages.STATE_JS))
                identity=candidate.evaluate(pages.CHAT_IDENTITY_JS)
                if pages.matching_chat(self.target,identity,self.job.job_id):
                    matches.append(candidate)
                elif candidate is self.page and isinstance(identity,dict) and (
                        identity.get('job_id') not in (None,'',self.job.job_id)
                        or all(identity.get(k) for k in ('job_id','company','title','header_title'))):
                    self._mismatched_chat('原页聊天目标不匹配；不发送')
            if len(matches)>1:
                raise pages.PlatformStateError('多个匹配的聊天页；不发送')
            if len(matches)==1:
                found=matches[0]
                if found is not self.page:
                    self.space.claim_page(found,prior)
                    original,self.page=self.page,found
                    original.close()
                return
            if monotonic()>=deadline:
                raise PageTimeout('目标聊天页未得到明确核验')
            sleep(.25)

    def _mismatched_chat(self, detail):
        # Never infer absence from a different conversation or a missing row.
        # No form/composer submission has occurred on this branch.
        if not self.modal_submitted and not self.message_submitted:
            from job_agent.platforms.boss.receipt_reader import read_no_outgoing
            current=self.page.evaluate(pages.CHAT_IDENTITY_JS)
            current_id=current.get('job_id') if isinstance(current,dict) else None
            ids=list(dict.fromkeys([self.job.job_id,current_id]))
            if current_id and all(read_no_outgoing(self.space,jid) for jid in ids):
                raise pages.NotStarted('只读核验目标及当前会话均无发出消息；跳过')
        raise pages.PlatformStateError(detail)

    def _chat_identity(self):
        self._guard()
        if not pages.is_chat_url(self.page.url()):
            raise pages.PlatformStateError('不是原站聊天页')
        d=self.page.evaluate(pages.CHAT_IDENTITY_JS)
        if not pages.matching_chat(self.target,d,self.job.job_id) or not d.get('conversation_id'):
            self._mismatched_chat('聊天岗位或会话不匹配；不发送')
        if self.conversation_id is not None and d['conversation_id']!=self.conversation_id:
            raise pages.PlatformStateError('聊天会话变化；不发送')
        self.conversation_id=d['conversation_id']

    def send_greeting(self, greeting: str) -> None:
        if self.delivery_kind == 'followup':
            from job_agent.platforms.boss.followup import send
            return send(self,greeting)
        greeting = prepare_greeting(self.platform, greeting,
                                    self._setting('greeting_newline_separator', '；'))
        if self.attempted:
            raise pages.PlatformStateError('同一会话禁止重复提交')
        maximum = min(1000, self._setting('greeting_max_length', 1000))
        if not greeting.strip() or pages.utf16_length(greeting) > maximum:
            raise ValueError('招呼语为空或超长；拒绝截断')
        if self._setting('greeting_mode', self.greeting_mode) != self.greeting_mode:
            raise ValueError('招呼方式与平台架构不一致')
        if self._setting('attachment_name', ''):
            raise pages.PlatformStateError('附件流程尚待授权取证；不提交申请')
        if self.check_applied():
            raise pages.PlatformStateError('已沟通，禁止重复申请')
        self.target = self.page.evaluate(pages.JOB_IDENTITY_JS)
        if not isinstance(self.target,dict) or not self.target.get('company') or not self.target.get('title'):
            raise pages.NotStarted('目标公司和岗位不可读；不开始沟通')
        prior={t.label for t in self.space.tabs()}
        if self.page.label not in prior:
            raise pages.NotStarted('原始页无法核验；不开始沟通')
        self.greeting,self.attempted,self.attempted_at=greeting,True,time()
        phase='start_contact'
        try:
            # If the click itself times out, its effect is unknown. Do not
            # enter the transition observer or execute another action.
            self.page.click(pages.START)
            phase='chat_navigation'
            self._observe_contact_chat(prior)
            if self.modal_submitted:
                # The visible form was submitted, whether or not chat opens.
                # Receipt verification below never uses the chat composer.
                return
            phase='chat_input_ready'
            self.page.wait_for(pages.INPUT)
            self._chat_identity()
            data=self.page.evaluate(pages.MESSAGES_JS)
            pages.guard(data)
            if pages.fresh_greeting_receipt(data,self.job.job_id,self.conversation_id,
                    greeting,self.attempted_at,time()):
                # First contact can submit a platform default. A fresh,
                # matching positive receipt means no additional greeting.
                self.contact_receipt=True
                return
            if not pages.first_message_ready(self.page.evaluate(pages.CHAT_READY_JS),data,
                    self.job.job_id,self.conversation_id):
                raise pages.PlatformStateError('首次沟通已有消息或聊天尚未完整就绪；不重复发送')
            self.before=pages.message_ids(data)
            phase='fill_and_read_back'
            if '\n' in greeting:
                self.page.fill(pages.INPUT, greeting, newline=pages.GREETING_NEWLINE)
            else:
                self.page.fill(pages.INPUT, greeting)
            if self.page.evaluate(pages.INPUT_JS) != greeting:
                raise pages.PlatformStateError('聊天框全文读回不匹配；不发送')
            self._chat_identity()
            latest=self.page.evaluate(pages.MESSAGES_JS)
            if pages.fresh_greeting_receipt(latest,self.job.job_id,self.conversation_id,
                    greeting,self.attempted_at,time()):
                self.contact_receipt=True
                return
            if not pages.no_outgoing_message(latest,self.job.job_id,self.conversation_id):
                raise pages.PlatformStateError('发送前出现既有或不明消息；不发送')
            phase='send_message'
            self.message_submitted=True
            self.page.press(pages.SEND)
            phase='message_ready'
            self.page.wait_for(pages.MESSAGE_READY)
        except PageTimeout as exc:
            if phase=='chat_navigation' and self.modal_submitted:
                # The form action may already have succeeded even if the page
                # remains on the job. No further submit; verify the original
                # conversation independently, unless the site indicates a block.
                self._guard()
                return
            raise PageTimeout(f'boss.{phase}: {exc.code}') from None

    def choose_attachment(self, attachment_name: str) -> bool:
        self.attachment_requested = bool(attachment_name)
        return not self.attachment_requested

    def confirm_receipt(self) -> bool:
        if not self.attempted or self.greeting is None or self.attachment_requested:
            return False
        if self.modal_submitted:
            self._guard()
            if pages.is_chat_url(self.page.url()):
                identity=self.page.evaluate(pages.CHAT_IDENTITY_JS)
                if pages.matching_chat(self.target,identity,self.job.job_id) and identity.get('conversation_id'):
                    if pages.fresh_greeting_receipt(self.page.evaluate(pages.MESSAGES_JS),
                            self.job.job_id,identity['conversation_id'],self.greeting,self.attempted_at,time()):
                        return True
            from job_agent.platforms.boss.receipt_reader import read_greeting
            evidence=read_greeting(self.space,dict(platform_job_id=self.job.job_id,
                greeting=self.greeting,attempted_at=self.attempted_at))
            return evidence.get('greeting_confirmed') is True
        self._chat_identity()
        data=self.page.evaluate(pages.MESSAGES_JS)
        pages.guard(data)
        if self.contact_receipt:
            return pages.fresh_greeting_receipt(data,self.job.job_id,self.conversation_id,
                self.greeting,self.attempted_at,time())
        if data.get('current_job_id')!=self.job.job_id or not isinstance(data.get('rows'),list):
            return False
        scoped={'rows':[r for r in data['rows'] if isinstance(r,dict)
                         and r.get('conversation_id')==self.conversation_id]}
        return pages.receipt(scoped,self.greeting,self.job.job_id,self.before)

    def close(self) -> None:
        if self.page is not None:
            p=self.page
            p.close()
            self.page=None
