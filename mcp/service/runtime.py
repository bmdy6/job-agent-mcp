"""Live dependencies; constructed only after explicit config opt-in and batch binding."""
from browser.ego import EgoBrowser
from engine.spaces import TaskSpaces
from platforms.registry import create_adapters

from platforms.preflight import AdapterPreflight

def dependencies(settings, *, browser_settings=None, notify_settings=None):
    from browser.notifications import space_alert
    browser = EgoBrowser(keepalive_seconds=browser_settings.keepalive_seconds if browser_settings else 300,
                         on_event=lambda name, event: space_alert(notify_settings, name, event),
                         human_input=browser_settings.human_input if browser_settings else None)
    adapters = create_adapters(browser, settings)
    return adapters, AdapterPreflight(browser, adapters), TaskSpaces(browser)
