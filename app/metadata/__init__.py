"""Universal metadata processors and normalizers."""
from typing import Protocol


class MetadataProcessor(Protocol):
    def process(self, item) -> dict: ...
