"""Session skips, durable risk pause, and explicit login-guarded resume."""
import logging
from platforms.session import check_session
from engine.rate import RateDeferred
from store.automation import AutomationRepository

LABELS={'boss':'BOSS','zhilian':'智联','liepin':'猎聘','51job':'51job','nowcoder':'牛客','maimai':'脉脉'}


def telegram_alert(config,platform,reason,space_name=None):
    import notify
    if not config.notify.enabled:
        return
    if reason in {'login_required','account_type','session_unknown'}:
        text=f'请在 Ego 空间「{space_name or config.collect.task_space_name}」里登录 {LABELS[platform]}'
    else:
        text=f'{LABELS[platform]} 已停止，原因：{reason}。风控暂停需检查登录后手动解除。'
    failed=False
    for channel in config.notify.channels.values():
        if channel.enabled and channel.type=='telegram':
            failed |= not notify.send_one(channel,'job-agent 平台检查',text)['success']
    if failed:raise RuntimeError('Telegram alert failed')


class PlatformSafety:
    def __init__(self,browser,store,config,*,check=check_session,alert=None,space_name=None):
        # Each engine checks login in the Ego space it already owns; cookies are profile-wide.
        self.space_name=space_name or config.collect.task_space_name
        self.browser,self.repo,self.config,self.check=browser,AutomationRepository(store),config,check
        self.checked=False
        self.alert_failed=set()
        self.alert=alert or (lambda p,c:telegram_alert(config,p,c,self.space_name))

    def inspect(self,platform):
        self.checked=True
        expected=self.config.engine.platforms[platform].expected_account_type
        result=self.check(self.browser,platform,self.space_name, **({'expected_account_type':expected} if expected!='any' else {}))
        if result.reason in {'risk_control','unexpected_redirect'}:
            self.repo.pause(platform,result.reason)
        if not result.ok:
            try:
                self.alert(platform,result.reason)
                self.alert_failed.discard(platform)
            except Exception:
                self.alert_failed.add(platform)
                logging.getLogger(__name__).error('platform alert failed platform=%s reason=%s',platform,result.reason)
        return result

    def require(self,platform):
        if platform in self.repo.pauses():
            raise RateDeferred('risk_control')
        result=self.inspect(platform)
        if not result.ok:
            error=RateDeferred(result.reason)
            error.alert_failed=platform in self.alert_failed
            raise error
        return result

    def pause(self,platform):
        self.repo.pause(platform)
        self.alert(platform,'risk_control')

    def resume(self,platform):
        # Explicit command only. Successful periodic preflight never clears a pause.
        original=self.repo.pause_token(platform)
        result=self.inspect(platform)
        if not result.ok:
            return {'resumed':False,**result.as_dict()}
        resumed=self.repo.resume(platform,expected=original)
        return {**result.as_dict(),'resumed':resumed,**({'reason':'pause_changed'} if not resumed else {})}


def resume_platform(platform):
    from browser.ego import EgoBrowser
    from config import load_config
    from store import Store
    config=load_config()
    browser=EgoBrowser(keepalive_seconds=0,human_input=config.browser.human_input)
    try:
        with Store() as store:
            return PlatformSafety(browser,store,config).resume(platform)
    finally:
        browser.shutdown(config.collect.task_space_name)
