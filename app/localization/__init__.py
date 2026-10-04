"""Locale-aware browser metadata providers."""
from typing import Protocol


class Localizer(Protocol):
    def fetch(self, url: str) -> dict: ...
