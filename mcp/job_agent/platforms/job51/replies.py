"""Status-only fallback authorized by #37; no chat/write endpoints."""
import time
import logging
from job_agent.platforms.job51 import status


log = logging.getLogger(__name__)
# Translate only fixed internal parser errors, never serialize page/driver text.
_PARSE_REASONS = {
    '51job 申请筛选页面未就绪或筛选不匹配': 'application_filter_not_ready',
    '51job 申请列表结构不可读': 'application_rows_unreadable',
    '51job 缺少明确的无数据标志': 'application_empty_unverified',
    '51job 申请缺少岗位、公司或申请日期': 'application_field_missing',
    '51job 申请岗位身份不匹配': 'application_identity',
    '51job 申请日期不可识别': 'application_date',
    '51job 申请状态不可识别': 'application_status',
    '51job 申请简历名称不可识别': 'application_resume_name',
    '51job 申请分页不可识别或超出上限': 'application_pagination',
    '51job 申请末页仍有下一页': 'application_last_page',
    '51job 申请下一页不可识别': 'application_next_page',
    '51job 申请分页重复': 'application_page_repeated',
    '51job 申请分页在读取期间变化，下轮重新读取': 'application_pages_changed',
    '51job 申请分页不连续': 'application_page_order',
    '51job 申请岗位重复，列表可能发生变化': 'application_rows_repeated',
    '51job 状态与全部申请不匹配，下轮重新读取': 'application_filter_mismatch',
}


def step(stage, action):
    try:
        return action()
    except Exception as error:
        error.stage = stage
        error.reason_code = _PARSE_REASONS.get(str(error), getattr(error, 'code', 'unexpected_error'))
        from job_agent.monitor.diagnostics import failure
        detail = failure(error)
        log.warning('platform=51job stage=%s reason_code=%s error_type=%s',
                    detail['stage'], detail['reason_code'], type(error).__name__)
        raise


class Job51ReplySource:
    platform = '51job'

    def fetch(self, space, *, since=None, cursor=None):
        page = step('open_page', space.new_page)
        failed = False
        try:
            # Keep the existing single temporary page for monitor filters.
            snapshots = {tag: step('read_applications_' + (tag or 'all'),
                                   lambda: status.read_application_pages(page, tag=tag))
                         for tag in ('', *status.STATUS_TAGS)}
            events = step('parse_status_events', lambda: status.application_events(snapshots, observed_at=time.time(), since=since))
            if cursor is None:
                return events
            changed = set()
            for summary in status.conversation_summaries(snapshots):
                if cursor.select(summary):
                    changed.add(summary.conversation_id)
                    cursor.acknowledge(summary)
            return [e for e in events if ':'.join(e.message_id.split(':')[1:3]) in changed]
        except BaseException:
            failed = True
            raise
        finally:
            try:
                step('close_page', page.close)
            except Exception:
                if not failed:
                    raise


ReplySource = Job51ReplySource
