"""Refuse before effects: public details have only App download instructions."""
from job_agent.engine.adapters import PendingP0Adapter

class MaimaiAdapter(PendingP0Adapter):
    def __init__(self,browser,settings):
        self.browser,self.settings=browser,settings

    def close(self):pass  # open_job never allocates a page.

PlatformAdapter=MaimaiAdapter
