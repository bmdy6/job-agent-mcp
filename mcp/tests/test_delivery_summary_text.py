import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_agent.engine.models import Job, Record, Status, Reason
from job_agent.service.deliver import summary, DETAIL_MARK


class DeliverySummaryTest(unittest.TestCase):
    def test_lists_platform_company_title_result_and_link(self):
        ok = Record(Job('boss', 'j1', 'https://www.zhipin.com/job_detail/j1.html'), Status.SENT)
        bad = Record(Job('boss', 'j2', 'https://www.zhipin.com/job_detail/j2.html'), Status.UNVERIFIED, Reason.EFFECT_ERROR)
        text = summary([ok, bad], dry_run=False, details={('boss', 'j1'): dict(company='甲公司', title='Agent 开发', salary='10-15K', city='合肥')})
        self.assertIn('BOSS：成功 1、不明 1', text)
        self.assertIn('✅ 甲公司｜Agent 开发｜10-15K｜合肥 — 成功', text)
        self.assertNotIn('https://', text)
        self.assertIn('⚠️ 未提供｜未提供｜未提供｜未提供 — 不明：执行中出错，结果未知', text)
        self.assertIn('成功 1', text)
        self.assertIn(DETAIL_MARK, text)


if __name__ == '__main__':
    unittest.main()
