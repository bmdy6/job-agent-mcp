"""Explicit account-default sync, preview first; never participates in daily delivery."""
from job_agent.config import load_config
from job_agent.platforms.greeting import prepare_greeting

def sync_default_greeting(platform, confirm=False):
    if type(confirm) is not bool: raise ValueError('confirm must be boolean')
    if platform!='zhilian':
        return {'platform':platform,'supported':False,'changed':False,
                'reason':'未取得桌面默认招呼语编辑器与回读证据'}
    config=load_config()
    pcfg=config.engine.platforms[platform]
    if pcfg.greeting_mode!='platform_default': raise ValueError('Platform must use platform_default mode')
    text=prepare_greeting(platform,pcfg.greeting,pcfg.greeting_newline_separator, paragraph_separator=pcfg.greeting_paragraph_separator)
    from job_agent.browser.platform_lock import platform_lock
    from job_agent.browser.ego import EgoBrowser
    from job_agent.platforms.zhilian import default_greeting as site
    # Per-platform lock serializes with delivery; no config or job-database writes.
    with platform_lock(platform):
        browser=EgoBrowser(human_input=config.browser.human_input,keepalive_seconds=0)
        name='job-agent 默认招呼语同步'
        space=browser.space(name)
        page=space.new_page()
        try:
            page.goto(site.URL,wait_ms=15000)
            page.wait_for('.greeting-left-nav__item')
            page.click(site.NAV)
            page.wait_for('.greeting-list__item')
            return {'platform':platform,'supported':True,**site.synchronize(page,text,confirm)}
        finally:
            page.close()
            browser.shutdown(name)
