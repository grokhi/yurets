from __future__ import annotations

from datetime import datetime, time

from src.settings import ScheduleSlot


class Scheduler:
    def __init__(self, slots: list[ScheduleSlot]) -> None:
        self._slots = slots

    def choose_source(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        current = now.time()

        for slot in self._slots:
            if _time_in_slot(current, slot.start, slot.end):
                return slot.source

        # fallback: first slot (predictable)
        return self._slots[0].source if self._slots else "local"


def _time_in_slot(current: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= current < end
    # wraps across midnight
    return current >= start or current < end
