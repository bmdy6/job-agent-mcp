class AdapterPreflight:
    """Read-only adapter guards run on the opened job before the effect boundary."""
    def __init__(self, browser, adapters):
        self.browser, self.adapters = browser, adapters

    def task_space_valid(self, space):
        self.browser.space(space)
        return True

    def logged_in(self, platform, space):
        # Concrete check_applied performs login/risk guards and requires known state.
        return type(self.adapters[platform].check_applied()) is bool

    def attachment_matches(self, platform, attachment_name):
        # 51job's actual unique selection must occur AFTER application by architecture.
        # Engine explicitly defers that check; all other unverified attachments block.
        return False
