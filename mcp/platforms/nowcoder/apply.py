"""Delivery is deliberately unavailable until recruitment receipts are verified."""
from engine.adapters import PendingP0Adapter

class NowcoderAdapter(PendingP0Adapter):
    def __init__(self,browser,settings):
        self.browser,self.settings=browser,settings

    def close(self):
        # open_job refuses before allocating a page or creating any effect.
        pass

PlatformAdapter=NowcoderAdapter
