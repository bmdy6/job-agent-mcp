from typing import Protocol
from .models import Platform


class Preflight(Protocol):
    # All methods read-only; unknown is False, never assume login or attachment success.
    def logged_in(self, platform: Platform, task_space: str) -> bool: ...
    def task_space_valid(self, task_space: str) -> bool: ...
    def attachment_matches(self, platform: Platform, attachment_name: str) -> bool: ...


class FakePreflight:
    def __init__(self, *, logged_in=True, space_valid=True, attachments=('resume',)):
        self.login = logged_in
        self.space_valid = space_valid
        self.attachments = list(attachments)

    def logged_in(self, platform, task_space):
        return self.login

    def task_space_valid(self, task_space):
        return self.space_valid

    def attachment_matches(self, platform, attachment_name):
        return self.attachments.count(attachment_name) == 1
