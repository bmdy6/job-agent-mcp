"""Finite synthetic source for CLI dry-run. Never opens real pages."""
from job_agent.platforms.base import JobListing


class FakeSource:
    def __init__(self, platform):
        self.platform = platform

    def search(self, space, query):
        if query.page > 1:
            return []
        return [JobListing(self.platform, f'fake:{query.keyword}:{query.city}',
                           'https://example.invalid/job', '演示公司', query.keyword,
                           query.city, '10-20K')]

    def detail(self, space, listing):
        return '离线演示 JD'
