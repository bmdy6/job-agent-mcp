"""Unified platform output; only platform parsers interpret site vocabulary."""
import re

ACTIVITY_ORDER = ('now', 'today', 'three_days', 'week', 'unknown', 'inactive')


def activity(raw):
    text = re.sub(r'\s+', '', str(raw or ''))
    if re.fullmatch(r'(刚刚活跃|刚刚在线|当前在线|在线|正在活跃)', text):
        return 'now'
    if re.fullmatch(r'(今日活跃|今天活跃|今日在线|今天在线|\d+分钟前活跃|\d+小时前活跃)', text):
        return 'today'
    if re.fullmatch(r'([123一二三]日内活跃|[123一二三]天内活跃|[12]天前活跃|昨日活跃|昨天活跃)', text):
        return 'three_days'
    if re.fullmatch(r'(本周活跃|本周在线|[一1]周内活跃|7[日天]内活跃|[3-6]天前活跃)', text):
        return 'week'
    if re.fullmatch(r'(近一个月活跃|本月活跃|\d+周前活跃|\d+月前活跃|\d+天前活跃|[一二三四五六七八九十]+月内活跃|[一二三四五六七八九十]+周内活跃|[123]0[日天]内活跃|[一1]月内活跃|长期未活跃)', text):
        return 'inactive'
    return 'unknown'


def fields(row):
    raw = str(row.get('hr_activity_raw') or '').strip()
    return dict(hr_activity=activity(raw), hr_activity_raw=raw,
                experience=str(row.get('experience') or '').strip(),
                education=str(row.get('education') or '').strip())
