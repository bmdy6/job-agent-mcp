"""Read public city directory pages; keyword filtering is local and incomplete."""
from job_agent.platforms.maimai import pages

class MaimaiJobSource:
    platform='maimai'

    @staticmethod
    def search_page_cost(query):return 1

    def search(self,space,query):
        url=pages.search_url(query)
        page=space.new_page()
        try:
            page.goto(url,wait_ms=15000)
            pages.guard(page.evaluate(pages.GUARD_JS))
            page.wait_for('[data-testid="website-job-card"], [aria-label="招聘职位"]')
            pages.guard(page.evaluate(pages.GUARD_JS))
            data=page.evaluate(pages.SEARCH_JS)
            rows=pages.parse_search(data,query)
            self.skipped_rows=len(data['items'])-len(rows)
            return rows
        finally:page.close()

    def detail(self,space,listing):
        if listing.platform!=self.platform:raise ValueError('岗位平台不匹配')
        url=pages.clean_job_url(listing.url)
        page=space.new_page()
        try:
            page.goto(url,wait_ms=15000)
            pages.guard(page.evaluate(pages.GUARD_JS))
            page.wait_for('h1')
            return pages.parse_detail(page.evaluate(pages.DETAIL_JS),listing)
        finally:page.close()

JobSource=MaimaiJobSource
