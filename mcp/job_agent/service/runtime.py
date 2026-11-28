"""Live dependencies; constructed only after explicit config opt-in and batch binding."""
from job_agent.browser.ego import EgoBrowser
from job_agent.engine.spaces import TaskSpaces
from job_agent.platforms.registry import create_adapters

from job_agent.platforms.preflight import AdapterPreflight

def dependencies(settings, *, browser_settings=None, notify_settings=None):
    from job_agent.browser.notifications import space_alert
    browser = EgoBrowser(startup_timeout_s=browser_settings.startup_timeout_seconds if browser_settings else 60, keepalive_seconds=browser_settings.keepalive_seconds if browser_settings else 300,
                         on_event=lambda name, event: space_alert(notify_settings, name, event),
                         human_input=browser_settings.human_input if browser_settings else None)
    adapters = create_adapters(browser, settings)
    return adapters, AdapterPreflight(browser, adapters), TaskSpaces(browser)
