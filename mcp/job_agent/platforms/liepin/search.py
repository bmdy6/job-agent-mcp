from job_agent.platforms.hr import detail as with_hr
"""Read-only liepin source. Scheduling/rate limits belong to collect/."""
from job_agent.platforms.liepin import pages

class LiepinJobSource:
    platform = 'liepin'

    @staticmethod
    def search_page_cost(query):
        """Upper bound of list loads, including replayed batches."""
        return 1

    def search(self, space, query):
        url = pages.search_url(query, getattr(self,'city_codes',None))
        page = space.new_page()
        try:
            page.goto(url, wait_ms=15000)
            return pages.parse_search(page.evaluate(pages.SEARCH_JS),query)
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

JobSource = LiepinJobSource
