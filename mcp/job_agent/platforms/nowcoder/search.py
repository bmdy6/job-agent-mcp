"""Read-only Nowcoder JobSource; all scheduling is owned by collect/."""
from job_agent.platforms.nowcoder import pages

class NowcoderJobSource:
    platform='nowcoder'

    @staticmethod
    def search_page_cost(query):
        return 2  # navigation plus keyword search

    def search(self,space,query):
        steps=self.search_steps(space,query)
        while True:
            try:next(steps)
            except StopIteration as done:return done.value
            before=getattr(self,'before_operation',None)
            if before:before()
            else:
                import time
                time.sleep(120)

    def search_steps(self,space,query):
        url=pages.search_url(query)
        page=space.new_page()
        try:
            page.goto(url,wait_ms=15000)
            pages.guard(page.evaluate(pages.GUARD_JS))
            page.wait_for('input[placeholder="请输入公司名或职位名搜索"]')
            yield
            page.fill('input[placeholder="请输入公司名或职位名搜索"]',query.keyword)
            page.press('Enter')
            # Wait for the searched value and settled list, not the old recommendation DOM.
            import json
            ready=page.evaluate(r'''async () => { /* nowcoder.search_ready */
              for(let i=0;i<80;i++){const v=document.querySelector('.school-job-box')?.__vue__;
                if(v?.searchValue===KEYWORD&&v.isLoading===false)return true;
                await new Promise(r=>setTimeout(r,100));}return false;
            }'''.replace('KEYWORD',json.dumps(query.keyword)))
            pages.guard(page.evaluate(pages.GUARD_JS))
            if ready is not True:raise pages.Unverified('牛客搜索未就绪')
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
            page.wait_for('.job-detail-word')
            return pages.parse_detail(page.evaluate(pages.DETAIL_JS),listing)
        finally:page.close()

JobSource=NowcoderJobSource
