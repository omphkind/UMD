"""Source-independent selection across incrementally loaded collection pages."""
from collections import OrderedDict
from copy import deepcopy

from app.core.errors import ConfigurationError


class SelectionManager:
    def __init__(self, items=None):
        self._items = OrderedDict()
        self._selected = set()
        self.extend(items or [])

    @staticmethod
    def identity(item):
        if isinstance(item, str):
            return item
        if not isinstance(item, dict) or not (item.get("id") or item.get("media_id")):
            raise ConfigurationError("У объекта выбора отсутствует ID.")
        return f"{item.get('source') or 'unknown'}:{item.get('id') or item.get('media_id')}"

    def _keys(self, item):
        key = self.identity(item)
        if key in self._items:
            return [key]
        return [existing for existing, value in self._items.items()
                if str(value.get("id") or value.get("media_id")) == key]

    def extend(self, items):
        for item in items:
            key = self.identity(item)
            self._items[key] = deepcopy(item)

    add_items = extend

    def replace_items(self, items):
        selected = set(self._selected)
        self._items.clear()
        self.extend(items)
        self._selected = selected.intersection(self._items)

    set_items = replace_items

    def select(self, item):
        self._selected.update(self._keys(item))

    def deselect(self, item):
        self._selected.difference_update(self._keys(item))

    def is_selected(self, item):
        keys = self._keys(item)
        return bool(keys) and all(key in self._selected for key in keys)

    def select_all(self):
        self._selected = set(self._items)

    def deselect_all(self):
        self._selected.clear()

    def invert(self):
        self._selected = set(self._items).difference(self._selected)

    def select_range(self, start, end, selected=True):
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ConfigurationError("Границы выбора должны быть целыми индексами.")
        if start < 0 or end < start or end >= len(self._items):
            raise ConfigurationError("Диапазон выбора выходит за список объектов.")
        keys = list(self._items)[start:end + 1]
        if selected:
            self._selected.update(keys)
        else:
            self._selected.difference_update(keys)

    def filter(self, media_type=None):
        return deepcopy([item for item in self._items.values()
                         if media_type is None or item.get("media_type") == media_type])

    def selected_items(self):
        return deepcopy([item for key, item in self._items.items() if key in self._selected])

    def selected_ids(self):
        return [key for key in self._items if key in self._selected]

    def __len__(self):
        return len(self._items)
