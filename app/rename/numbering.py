"""Deterministic numbering, independent of network and filesystem state."""
from dataclasses import dataclass


@dataclass(frozen=True)
class NumberingEngine:
    start: int = 1
    step: int = 1
    padding: int = 0

    def __post_init__(self):
        if self.start < 0 or self.step < 1 or not 0 <= self.padding <= 12:
            raise ValueError("Нумерация: начало ≥ 0, шаг ≥ 1, разрядность от 0 до 12.")

    def value(self, index: int) -> int:
        return self.start + index * self.step
