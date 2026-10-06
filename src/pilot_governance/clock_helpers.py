"""测试与验收使用的可推进时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta


class SteppingClock:
    """以固定步长推进时间，用于构造严格有序的生效时点。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("起始时间必须包含时区")
        self._value = start.astimezone()

    def now(self) -> datetime:
        return self._value

    def tick(self, seconds: int = 1) -> None:
        self._value = self._value + timedelta(seconds=seconds)
