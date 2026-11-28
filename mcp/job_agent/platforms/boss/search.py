from job_agent.platforms.hr import detail as with_hr, listing_hr
"""只读采集编排；所有页面知识在 pages.py。"""
import random
import time
from job_agent.browser.base import PageTimeout
from job_agent.platforms.boss import pages


class BossJobSource:
    platform = 'boss'

    def _read(self, page, projection, ready):
        # DOM hydration can lag navigation. Poll only reads, never retry navigation.
        deadline = time.monotonic() + 10
        while True:
            data = page.evaluate(projection)
            pages.collection_guard(data)
            if ready(data):
                return data
            if time.monotonic() >= deadline:
                raise PageTimeout('采集页面尚未加载')
            time.sleep(0.5)

    @staticmethod
    def search_page_cost(query):
        """Upper bound of list loads, including replayed batches."""
        return query.page

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
            data = self._read(page, pages.SEARCH_JS, lambda d: d.get('ready') is True)
            # Current sites use infinite scroll; page URL parameters are ignored.
            for _ in range(1, query.page):
                if data.get('has_more') is False:
                    return []
                previous = len(data.get('rows', []))
                yield
                page.evaluate(pages.SCROLL_JS)
                data = self._read(page, pages.SEARCH_JS,
                    lambda d: d.get('ready') is True and (
                        len(d.get('rows', [])) > previous or d.get('has_more') is False))
            rows = pages.search_rows(data)
            start = (query.page - 1) * pages.PAGE_SIZE
            return listing_hr(page, self.platform, rows[start:start + pages.PAGE_SIZE])
        finally:
            page.close()

    def detail(self, space, listing):
        if listing.platform != self.platform or pages.job_id(listing.url) != listing.platform_job_id:
            raise ValueError('岗位平台或 ID 不匹配')
        page = space.new_page()
        try:
            page.goto(listing.url, wait_ms=15000)
            if hasattr(page,'read_detail'): page.read_detail()
            data = self._read(page, pages.DETAIL_JS, lambda d: bool(d.get('jd')))
            return with_hr(page, self.platform, pages.detail_text(data, listing.platform_job_id))
        finally:
            page.close()


JobSource = BossJobSource
