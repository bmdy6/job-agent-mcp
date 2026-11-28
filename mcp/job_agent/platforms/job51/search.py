from job_agent.platforms.hr import detail as with_hr, listing_hr
"""Read-only 51job source. Scheduling/rate limits belong to collect/."""
import random
import time
from job_agent.platforms.job51 import pages

class Job51JobSource:
    platform = '51job'

    def __init__(self, *, interval_seconds=(8, 15)):
        import math
        if (len(interval_seconds) != 2 or not all(math.isfinite(v) for v in interval_seconds)
                or not 8 <= interval_seconds[0] <= interval_seconds[1]):
            raise ValueError('页间隔必须有序且至少 8 秒')
        self.interval_seconds = interval_seconds

    @staticmethod
    def search_page_cost(query):
        """Upper bound of list loads, including replayed batches."""
        return 1 if query.page == 1 else 2

    def search(self, space, query):
        steps=self.search_steps(space,query)
        while True:
            try: next(steps)
            except StopIteration as done: return done.value
            before=getattr(self,'before_operation',None)
            if before: before()
            else: time.sleep(random.uniform(*getattr(self,'interval_seconds',(8,15))))

    def search_steps(self, space, query):
        url = pages.search_url(query, getattr(self,'city_codes',None))
        page = space.new_page()
        try:
            page.goto(url, wait_ms=15000)
            data = page.evaluate(pages.SEARCH_JS)
            pages.collection_guard(data)
            if query.page > 1:
                yield
                data = page.evaluate(pages.pagination_js(query.page))
            rows = pages.parse_search(data,query)
            self.skipped_rows = len(data['items']) - len(rows)
            return listing_hr(page, self.platform, rows)
        finally:
            page.close()

    def detail(self, space, listing):
        if listing.platform != self.platform:
            raise ValueError('岗位平台不匹配')
        url = pages.clean_job_url(listing.url)
        page = space.new_page()
        try:
            page.goto(url, wait_ms=15000)
            if hasattr(page,'read_detail'): page.read_detail()
            return with_hr(page, self.platform, pages.parse_detail(page.evaluate(pages.DETAIL_JS),listing))
        finally:
            page.close()

JobSource = Job51JobSource
