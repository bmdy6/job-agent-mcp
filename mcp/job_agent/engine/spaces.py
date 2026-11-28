from job_agent.browser.base import Browser
from job_agent.browser.ego import EgoBrowser


class TaskSpaces:
    """Engine bridge. Name handles stay opaque; IDs never enter configuration."""

    def __init__(self, browser: Browser | None = None):
        self.browser = browser if browser is not None else EgoBrowser()

    def ensure(self, name: str) -> str:
        self.browser.space(name)
        return name

    def shutdown(self, name: str) -> None:
        """Only the process owner calls this at exit, never once per job."""
        self.browser.shutdown(name)


class FakeTaskSpaces:
    def __init__(self):
        self.names = set()
        self.created = []

    def ensure(self, name):
        if name not in self.names:
            self.names.add(name)
            self.created.append(name)
        return name
