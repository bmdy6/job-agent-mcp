"""Pure HTML/SVG renderer; no assets, JavaScript, network or hidden private payload."""
from html import escape
from urllib.parse import urlsplit

PLATFORMS = {'boss': 'BOSS', 'zhilian': '智联招聘', 'liepin': '猎聘', '51job': '前程无忧'}
STATUS = {'sent': '成功', 'replied': '已回复', 'resume_sent': '简历已发',
          'needs_resume': '待补简历', 'follow_up_sent': '已跟进', 'pending': '待续',
          'failed': '失败', 'unverified': '回执未确认', 'skipped': '已跳过',
          'rejected': '已拒绝', 'approved': '已确认', 'recorded_applied': '已记录投递'}
MONITOR = {'ok': '正常', 'running': '运行中', 'login_paused': '未登录，已暂停',
           'login_required': '未登录', 'risk_control_paused': '风控暂停',
           'platform_paused': '平台暂停', 'unknown': '未记录', 'disabled': '未启用',
           'browser_error': '浏览器异常', 'platform_busy': '平台占用',
           'rate_deferred': '等待频率限制', 'empty': '暂无新消息'}
CATEGORIES = {'interview': '面试邀约', 'resume_request': '索要简历', 'rejection': '拒绝',
              'question': '询问', 'other': '其他', 'unclassified': '待分类'}
CSS = '''
:root{--paper:#f4f2ec;--surface:#fffefa;--ink:#23332f;--muted:#5f6c65;--line:#d7dcd3;--accent:#176d5c;--wash:#e7eee6}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1440px;margin:auto;padding:32px}header{border-top:5px solid var(--accent);padding:20px 0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}h1{font-size:30px;line-height:1.3;letter-spacing:.04em;margin:6px 0}h2{font-size:19px;margin:0 0 16px}h3{font-size:15px;margin:0 0 8px}p{margin:8px 0}.eyebrow{font-size:12px;letter-spacing:.14em;color:var(--accent);font-weight:700}.muted,small{color:var(--muted)}.stamp{text-align:right;font-size:12px}.tag{display:inline-block;border:1px solid var(--line);padding:3px 10px;border-radius:4px;background:var(--surface)}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));border-block:1px solid var(--line);margin-bottom:24px;background:var(--surface)}.metric{padding:20px 16px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric strong{display:block;font-size:32px;line-height:1.4;font-variant-numeric:tabular-nums;color:var(--accent)}.metric span{font-size:12px;color:var(--muted)}
section{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:24px;margin-bottom:24px;min-width:0}.split{display:grid;grid-template-columns:1fr 1.4fr;gap:24px}.split section{margin-bottom:0}.split{margin-bottom:24px}.note{font-size:12px;color:var(--muted)}.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap}.section-head h2{margin-bottom:12px}.funnel{display:grid;grid-template-columns:84px repeat(3,minmax(0,1fr));align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.funnel-head{font-size:12px;color:var(--muted);border:0;padding-top:0}.bar{height:5px;background:var(--wash);margin-top:4px}.bar i{display:block;height:100%;background:var(--accent)}.funnel b{font-size:20px;font-variant-numeric:tabular-nums}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.chart{min-width:0}.chart svg{width:100%;display:block}.gridline{stroke:var(--line);stroke-width:1}.axis{fill:var(--muted);font-size:10px}.applied{fill:var(--accent)}.reply-line{fill:none;stroke:var(--ink);stroke-width:2;stroke-dasharray:4 3}.reply-dot{fill:var(--surface);stroke:var(--ink);stroke-width:1.5}.legend{display:flex;gap:16px;font-size:12px;color:var(--muted)}.legend i{display:inline-block;width:14px;height:8px;background:var(--accent);margin-right:6px}.legend .dash{height:0;border-top:2px dashed var(--ink);background:none;vertical-align:middle}
.status-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px}.platform{border-left:2px solid var(--accent);padding-left:16px;min-width:0}.quota{font-size:24px;font-weight:650}.platform dl{font-size:12px;margin:8px 0 0}.platform dt{color:var(--muted);margin-top:6px}.platform dd{margin:0;overflow-wrap:anywhere}.heartbeat{border-top:1px solid var(--line);margin-top:20px;padding-top:12px;font-size:12px}.heartbeat p{margin:4px 0}
.scroll{overflow:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:12px;text-align:left}caption{text-align:left;color:var(--muted);padding-bottom:8px}th{color:var(--muted);font-weight:500;background:var(--paper);white-space:nowrap}td,th{padding:12px;border-bottom:1px solid var(--line);vertical-align:top}td{overflow-wrap:anywhere;min-width:72px}td.company,td.title{min-width:120px;max-width:220px}.nowrap{white-space:nowrap}a{color:var(--accent);text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:4px}.empty{color:var(--muted);padding:20px 0}.excerpt{max-width:340px;min-width:160px}.pill{padding:2px 6px;background:var(--wash);border-radius:3px;white-space:nowrap}details{margin-top:12px}summary{cursor:pointer;color:var(--accent);font-size:12px;min-height:24px}footer{color:var(--muted);font-size:12px;padding-bottom:16px}
@media(max-width:1024px){.split{grid-template-columns:1fr}.status-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){main{padding:16px}header{align-items:flex-start;flex-direction:column;padding:16px 0}.stamp{text-align:left}h1{font-size:26px}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.metric{padding:12px 8px;border-bottom:1px solid var(--line)}.metric strong{font-size:25px}.metric:nth-child(3n){border-right:0}.metric:nth-child(n+4){border-bottom:0}section{padding:16px;margin-bottom:16px}.split{gap:16px;margin-bottom:16px}.charts,.status-grid{grid-template-columns:1fr}.funnel{grid-template-columns:64px repeat(3,minmax(0,1fr));gap:8px;font-size:12px}.funnel b{font-size:18px}h2{font-size:17px}.platform{padding-left:12px}}
'''


def e(value):
    return escape(str(value if value is not None else '—'), quote=True)


def platform(value):
    return e(PLATFORMS.get(value, value))


def rate(value):
    return f'{value:.1%}' if value is not None else '—'


def time_text(value, zone):
    from zoneinfo import ZoneInfo
    return value.astimezone(ZoneInfo(zone)).strftime('%m-%d %H:%M') if value else '时间未知'


def link(url, label):
    try:
        parsed = urlsplit(url or '')
        safe = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        safe = False
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">{e(label)} ↗</a>' if safe else '—'


def table(headers, rows, empty):
    if not rows:
        return f'<p class="empty">{e(empty)}</p>'
    return '<div class="scroll" tabindex="0" role="region" aria-label="可横向滚动的数据表"><table><thead><tr>' + ''.join(f'<th scope="col">{e(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def trend_chart(rows, name):
    maximum = max(1, *(max(r['applied'], r['replied']) for r in rows))
    y = lambda value: 132 - value / maximum * 100
    parts = [f'<svg viewBox="0 0 350 160" role="img" aria-label="{e(name)}近14天投递与回复趋势">']
    for value in sorted({0, maximum // 2, maximum}):
        parts.append(f'<line class="gridline" x1="28" x2="340" y1="{y(value)}" y2="{y(value)}"/><text class="axis" x="2" y="{y(value)+4}">{value}</text>')
    points = []
    for i, row in enumerate(rows):
        x = 39 + i * 23
        label = f"{row['date']} · 投递 {row['applied']} · 回复 {row['replied']}"
        parts.append(f'<rect class="applied" x="{x-5}" y="{y(row["applied"])}" width="10" height="{132-y(row["applied"])}"><title>{e(label)}</title></rect>')
        points.append(f'{x},{y(row["replied"])}')
        if i in (0, 4, 9, 13):
            parts.append(f'<text class="axis" text-anchor="middle" x="{x}" y="153">{row["date"][5:].replace("-", "/")}</text>')
    parts.append(f'<polyline class="reply-line" points="{" ".join(points)}"/>')
    for i, row in enumerate(rows):
        parts.append(f'<circle class="reply-dot" cx="{39+i*23}" cy="{y(row["replied"])}" r="2.5"><title>{e(row["date"])} · 回复 {row["replied"]}</title></circle>')
    return ''.join(parts) + '</svg>'


def render(data):
    totals = data['funnel']['total']
    zone = data['timezone']
    metrics = [('applied', '累计投递岗位', totals['applied']), ('companies', '累计投递公司', data['companies']),
               ('today', '今天投递', data['today']), ('replied', 'HR 回复岗位', totals['replied']),
               ('interview_invited', '面试邀约岗位', totals['interview_invited']), ('reply_rate', '回复率', rate(totals['reply_rate']))]
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<meta name="referrer" content="no-referrer"><title>求职数据看板</title><style>', CSS, '</style></head><body><main>',
             '<header><div><div class="eyebrow">JOB AGENT / 求职记录</div><h1>求职数据看板</h1><p class="muted">投递、回应与下一次机会</p></div>',
             f'<div class="stamp"><span class="tag">本地离线快照</span><p>生成于 {e(data["now"].strftime("%Y-%m-%d %H:%M"))}<br>{e(zone)} · 重新生成以更新数据</p></div></header><div class="metrics">']
    for key, label, value in metrics:
        parts.append(f'<div class="metric"><span>{label}</span><strong data-metric="{key}">{e(value)}</strong></div>')
    parts.append('</div><div class="split"><section><h2>投递漏斗</h2><div class="funnel funnel-head"><span>平台</span><span>投递 →</span><span>回复 →</span><span>面试邀约</span></div>')
    groups = {g['key']: g for g in data['funnel']['groups']}
    for p in dict.fromkeys([*PLATFORMS, *groups]):
        group = groups.get(p, {})
        parts.append(f'<div class="funnel"><span>{platform(p)}</span>')
        for key in ('applied', 'replied', 'interview_invited'):
            n = group.get(key, 0)
            width = min(100, n / max(1, group.get('applied', 0)) * 100)
            parts.append(f'<div><b>{n}</b><div class="bar"><i style="width:{width:.2f}%"></i></div></div>')
        parts.append('</div>')
    parts.append('<p class="note">按岗位去重。BOSS 的成功是「沟通回执」，不等于简历送达；其他平台也以已有回执为准。面试邀约均待本人确认。</p></section><section><div class="section-head"><h2>近 14 天，每天的进展</h2><div class="legend"><span><i></i>投递</span><span><i class="dash"></i>回复</span></div></div><div class="charts">')
    for p in dict.fromkeys(r['platform'] for r in data['trend']):
        rows = [r for r in data['trend'] if r['platform'] == p]
        parts.append(f'<div class="chart"><h3>{platform(p)}</h3>{trend_chart(rows, PLATFORMS.get(p,p))}</div>')
    undated = data['undated']
    parts.append(f'</div><p class="note">按首次已知投递 / 首次已知回复日期去重，使用 {e(zone)}。缺少日期：投递 {undated["applied"]} 个、回复 {undated["replied"]} 个；不计入每日趋势和今天数字。</p>')
    parts.append('<details><summary>查看每日精确数值</summary>' + table(['日期', '平台', '投递', '回复'], [[e(r['date']),platform(r['platform']),str(r['applied']),str(r['replied'])] for r in data['trend']], '') + '</details></section></div>')
    parts.append('<section><div class="section-head"><h2>各平台状态</h2><span class="note">状态文件的最后记录，非实时账号检测</span></div><div class="status-grid">')
    for row in data['platforms']:
        pause = {'risk_control':'风控暂停', 'login_required':'未登录，已暂停'}.get(row['pause'], '已暂停：'+row['pause'] if row['pause'] else '无暂停记录')
        parts.append(f'<div class="platform"><h3>{platform(row["platform"])} <small>{"已启用" if row["enabled"] else "未启用投递"}</small></h3><div class="quota">{row["used"]} / {row["limit"] if row["limit"] is not None else "不限"}</div><small>今日额度已使用（含尝试 / 预留）</small><dl>')
        for label, value in [('暂停',pause),('最近监听',MONITOR.get(row['monitor_status'],row['monitor_status'])),('阶段 / 原因码',row['stage']+' / '+row['reason_code']),('最近轮询',time_text(row['last_poll'],zone)),('最近成功',time_text(row['last_success'],zone))]:
            parts.append(f'<dt>{label}</dt><dd>{e(value)}</dd>')
        parts.append('</dl></div>')
    parts.append('</div><div class="heartbeat"><b>心跳状态</b>')
    if data['heartbeat']:
        for code, message in data['heartbeat'].items():
            parts.append(f'<p>{e(code)} · {e(message)}</p>')
    else:
        parts.append('<p>正常 · 最近轮询在心跳阈值内</p>')
    parts.append('<p class="note">复用现有心跳判断；本页面只读取状态，不发送告警。</p></div></section>')
    rows = []
    for r in data['deliveries']:
        j = r['job']
        label = '沟通回执' if j['platform'] == 'boss' and r['status'] == 'sent' else STATUS.get(r['status'], '未知结果')
        rows.append([e(time_text(r['time'],zone)),platform(j['platform']),e(j['company']),e(j['title']),e(j['salary']),e(j['city']),f'<span class="pill">{e(label)}</span>',link(j['url'],'岗位')])
    parts.append('<section><div class="section-head"><h2>最近投递</h2><span class="note">每个岗位最近一条结果 · 最多 50 条</span></div>' + table(['时间','平台','公司','岗位','薪资','城市','结果','链接'],rows,'暂无投递记录') + '</section>')
    rows = []
    for r in data['replies']:
        j = r['job']
        rows.append([e(time_text(r['time'],zone)),platform(j['platform']),e(j['company']),e(j['title']),e(CATEGORIES.get(r['category'],'待分类')),f'<div class="excerpt">{e(r["excerpt"])}{" <small>（摘要）</small>" if r["is_excerpt"] else ""}</div>',link(r['chat_url'],'聊天')])
    parts.append('<section><div class="section-head"><h2>最近 HR 回复</h2><span class="note">最多 30 条 · 正文仅保留前 40 字</span></div>' + table(['时间','平台','公司','岗位','回复分类','正文摘要','链接'],rows,'暂无 HR 回复') + '</section>')
    usage_rows = [[e('当天' if window=='today' else '最近7天'),
        e({'scoring':'评分','greetings':'招呼语','reply_classification':'回复分类','draft_reply':'回复建议'}[purpose]),
        str(row['calls']),str(row['prompt_tokens']),str(row['cached_input_tokens']),str(row['completion_tokens']),e(row['cost_text'])]
        for window,values in data.get('llm_usage',{}).items() for purpose,row in values.items()]
    parts.append('<section><h2>模型调用与费用</h2>'+table(['范围','用途','调用次数','输入 token','缓存输入 token','输出 token','费用'],usage_rows,'暂无模型调用')+'</section>')
    greetings = data['greetings']
    def interval(value):
        return '—' if value is None else f'{rate(value[0])}–{rate(value[1])}'
    rows = [[platform(g['platform']),e(g['greeting_version']),str(g['applied']),str(g['replied']),str(g['interview_invited']),rate(g['reply_rate']),interval(g['reply_ci95']),e(g['assessment'])] for g in greetings['platform_groups']]
    parts.append('<section><h2>招呼语版本效果</h2><p class="note">'+e(greetings['advice'])+'</p>' + table(['平台','版本','投递样本','回复','面试邀约','回复率','回复率 95% 区间','判断'],rows,'样本不足 · 暂无成功投递的招呼语样本') + f'<p class="note">最低样本数 {greetings["min_samples"]}；{e(greetings["semantics"])}</p></section>')
    parts.append('<footer>累计数字复用 funnel_stats；HR 回复数表示回复过的岗位数，不是消息条数。公司名按现有规则归一化去重，缺失名称不计。趋势只分配已有日期，不使用采集或导入时间。<br>个人求职数据 · 保存在本地 · 页面不自动联网；点击岗位或聊天链接后会打开对应网站。</footer></main></body></html>')
    return ''.join(parts)
