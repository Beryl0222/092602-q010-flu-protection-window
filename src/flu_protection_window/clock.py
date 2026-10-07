"""可调时钟：推演按“当前日期”而不是系统实时时钟推进。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Clock:
    """固定在某个推演日的时钟，可由命令行 ``--date`` 与服务方法设定。"""

    def __init__(self, current: str | date | None = None) -> None:
        self._today: date | None = None
        if current is not None:
            self.set(current)

    def set(self, current: str | date) -> "Clock":
        self._today = to_date(current)
        return self

    def advance(self, days: int) -> date:
        if self._today is None:
            self._today = date.today()
        self._today = self._today + timedelta(days=days)
        return self.today()

    def today(self) -> date:
        return date.today() if self._today is None else self._today

    def iso(self) -> str:
        return self.today().isoformat()

    def __repr__(self) -> str:
        return f"Clock({self.iso()!r})"
