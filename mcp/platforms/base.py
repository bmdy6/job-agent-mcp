"""平台层公共接口（#15 架构，见 docs/ARCHITECTURE.md）。

每个平台一个目录 mcp/platforms/<平台>/：
  pages.py   选择器与解析（纯函数，夹具单测）
  apply.py   投递适配器，实现 engine.adapters.PlatformAdapter，构造参数 (browser: browser.base.Browser, settings)
  replies.py 回复抓取，实现下面的 ReplySource
平台值：boss / zhilian / liepin / 51job（目录名 job51）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from browser.base import Space

Platform = Literal["boss", "zhilian", "liepin", "51job"]
Direction = Literal["incoming", "outgoing", "system"]


@dataclass(frozen=True)
class ReplyEvent:
    platform: Platform
    message_id: str            # 原站稳定消息 ID；拿不到就不产出事件
    platform_job_id: str       # 原站岗位 ID；对不上已投岗位的由监听层丢弃
    company: str
    job_title: str
    direction: Direction       # 只有 incoming 会推 TG
    body: str                  # 原文，不截断
    occurred_at: float         # UTC epoch 秒
    chat_url: str = ""
    extra: dict = field(default_factory=dict)  # 不放 HR 姓名、手机号


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    last_message_id: str       # Stable platform ID, or explicitly prefixed summary fingerprint
    unread_count: int
    last_time: float | None = None


class ReplySource(Protocol):
    platform: Platform

    def fetch(self, space: Space, *, since: float | None = None, cursor=None) -> list[ReplyEvent]:
        """只读：打开聊天列表，返回 since 之后的消息（含 outgoing/system，由监听层过滤）。
        自己开临时页、用完关掉；不点「聊一聊」、不发消息、不标记已读以外的副作用。"""


# ---- 采集（#15 P3）----

@dataclass(frozen=True)
class JobListing:
    """搜索结果列表里的一条岗位。字段名对齐 store.upsert_job。"""
    platform: Platform
    platform_job_id: str       # 原站稳定岗位 ID，去重键 (platform, platform_job_id)
    url: str
    company: str
    title: str
    city: str = ""
    salary: str = ""           # 原文，例如 "15-24K·14薪"
    extra: dict = field(default_factory=dict)  # 经验、学历、公司规模等
    hr_name: str = ""
    hr_title: str = ""
    hr_id: str = ""
    hr_activity: str = "unknown"
    hr_activity_raw: str = ""
    experience: str = ""
    education: str = ""


@dataclass(frozen=True)
class SearchQuery:
    keyword: str
    city: str                  # 城市名（中文），平台城市编码由各平台 pages.py 映射
    page: int = 1
    salary_min_k: int | None = None


class JobSource(Protocol):
    platform: Platform

    def search_page_cost(self, query: SearchQuery) -> int:
        """列表访问预算上界，包含翻页前重新加载的批次；在访问前预扣。"""

    def search(self, space: Space, query: SearchQuery) -> list[JobListing]:
        """只读：打开搜索页，返回这一页的岗位。没有更多时返回 []。
        遇到验证码/风控/登录失效抛 browser.base.BrowserError 子类，不重试、不绕过。"""

    def detail(self, space: Space, listing: JobListing) -> str:
        """只读：打开岗位详情页，返回 JD 全文。不点「沟通」「投递」等按钮。"""
