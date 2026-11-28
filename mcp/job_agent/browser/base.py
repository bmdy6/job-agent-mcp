"""浏览器驱动接口（#15 架构，见 docs/ARCHITECTURE.md）。

只有 mcp/browser/ego.py 可以调用 ego-browser。适配器和监听只依赖这里的 Protocol，
单测用 FakeBrowser + mcp/tests/fixtures/platforms/ 的离线夹具。
"""
from __future__ import annotations

from typing import Any, Protocol
from dataclasses import dataclass


class BrowserError(RuntimeError):
    """浏览器层错误的基类。code 用于映射引擎原因码。"""

    code = "browser_error"


class SpaceUnavailable(BrowserError):
    """空间被用户接管、inactive 或同名空间属于用户。必须停下，不能抢。"""

    code = "task_space"


class RiskControl(BrowserError):
    code = 'risk_control'


class PageTimeout(BrowserError):
    code = "page_timeout"


class Page(Protocol):
    """一个临时标签页。用完必须 close()。"""

    label: str

    def goto(self, url: str, *, wait_ms: int = 3000) -> None: ...
    def url(self) -> str: ...
    def wait_for_url(self, url: str, *, timeout_ms: int = 10000) -> None: ...
    def evaluate(self, js: str) -> Any:
        """在页面里执行 JS，返回 JSON 可序列化的值。读页面优先用这个。"""
    def snapshot(self) -> str:
        """无障碍树文本快照，用于取证和兜底解析。"""
    def click(self, selector: str) -> None: ...
    def hover(self, selector: str) -> None: ...
    def click_projected(self, projection: str) -> None: ...
    def fill(self, selector: str, text: str, *, newline: str = 'reject') -> None:
        """原样填入；多行必须显式声明 shift_enter / insert_text，默认拒绝。"""
    def press(self, key: str) -> None: ...
    def wait_for(self, selector: str, *, timeout_ms: int = 10000) -> None:
        """超时抛 PageTimeout。"""
    def screenshot(self, path: str) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class TabObservation:
    label: str
    url: str


class Space(Protocol):
    name: str
    space_id: int

    def new_page(self) -> Page:
        """开临时标签页。不会复用或关闭锚点页和用户页。"""
    def tabs(self) -> list[TabObservation]:
        """Read only agent-owned, non-anchor tabs in the same task space."""
    def inspect_page(self, label: str) -> Page:
        """Read-only handle to an observed agent-owned page."""
    def claim_page(self, page: Page, before: set[str]) -> None:
        """Own only a newly observed page after the adapter verifies its identity."""


class Browser(Protocol):
    def space(self, name: str) -> Space:
        """按名字取空间：存在就复用，缺失就新建同名空间并开锚点页（about:blank）。
        被用户接管/同名属于用户时抛 SpaceUnavailable。"""

    def shutdown(self, name: str, *, finish=False) -> None:
        """进程退出时停保活；默认保留锚点。一次性任务可显式 finish。"""


class FakePage:
    """离线假页面：evaluate 按注册的 (url 子串, js 片段) 返回夹具数据。"""

    def __init__(self, label: str, responses: dict[str, Any] | None = None):
        self.label = label
        self.responses = responses or {}
        self.actions: list[tuple] = []
        self._url = "about:blank"
        self.closed = False

    def goto(self, url, *, wait_ms=3000):
        self._url = url
        self.actions.append(("goto", url))

    def wait_for_url(self, url, *, timeout_ms=10000):
        self.actions.append(("wait_for_url", url))
        self._url = url

    def url(self):
        return self._url

    def evaluate(self, js):
        self.actions.append(("evaluate", js[:80]))
        for key, value in self.responses.items():
            if key in js or key in self._url:
                return value
        return None

    def snapshot(self):
        return str(self.responses.get("__snapshot__", ""))

    def click(self, selector):
        self.actions.append(("click", selector))

    def hover(self,selector):
        self.actions.append(('hover',selector))

    def click_projected(self,projection):
        self.actions.append(('click_projected',projection))
        if not self.evaluate(projection):
            raise BrowserError('Projected target unavailable')

    def fill(self, selector, text, *, newline='reject'):
        self.actions.append(("fill", selector, text) if newline=='reject'
                            else ("fill", selector, text, newline))

    def press(self, key):
        self.actions.append(("press", key))

    def wait_for(self, selector, *, timeout_ms=10000):
        self.actions.append(("wait_for", selector))

    def screenshot(self, path):
        self.actions.append(("screenshot", path))

    def close(self):
        self.closed = True


class FakeSpace:
    def __init__(self, name: str, space_id: int = 1, pages: list[FakePage] | None = None):
        self.name = name
        self.space_id = space_id
        self._pages = list(pages or [])
        self.opened: list[FakePage] = []
        self.spawned: list[FakePage] = []

    def tabs(self):
        return [TabObservation(p.label,p.url()) for p in self.opened+self.spawned if not p.closed]

    def inspect_page(self,label):
        matches=[p for p in self.opened+self.spawned if p.label==label and not p.closed]
        if len(matches)!=1:
            raise SpaceUnavailable('Observed page unavailable')
        return matches[0]

    def claim_page(self,page,before):
        if page.label in before or page not in self.spawned or page.closed:
            raise SpaceUnavailable('Cannot claim unrelated page')
        self.spawned.remove(page)
        self.opened.append(page)

    def new_page(self):
        page = self._pages.pop(0) if self._pages else FakePage(f"p{len(self.opened) + 2}")
        self.opened.append(page)
        return page


class FakeBrowser:
    def __init__(self, spaces: dict[str, FakeSpace] | None = None, unavailable: set[str] | None = None):
        self.spaces = spaces or {}
        self.unavailable = unavailable or set()
        self.shut: list[str] = []

    def space(self, name):
        if name in self.unavailable:
            raise SpaceUnavailable(name)
        if name not in self.spaces:
            self.spaces[name] = FakeSpace(name, len(self.spaces) + 1)
        return self.spaces[name]

    def shutdown(self, name, *, finish=False):
        self.shut.append(name)
