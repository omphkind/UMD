from app.tasks.selection import SelectionManager


def item(identifier, kind="photo", source="gallery"):
    return {"source": source, "id": str(identifier), "media_type": kind, "title": str(identifier)}


def test_individual_and_bulk_selection_preserve_collection_order_and_isolation():
    manager = SelectionManager([item(1), item(2, "video"), item(3)])
    assert manager.selected_items() == []
    manager.select("2")
    manager.select(item(1))
    assert manager.selected_ids() == ["gallery:1", "gallery:2"]
    manager.deselect("gallery:1")
    manager.invert()
    assert manager.selected_ids() == ["gallery:1", "gallery:3"]
    manager.select_all()
    selected = manager.selected_items()
    selected[0]["title"] = "changed"
    assert manager.selected_items()[0]["title"] == "1"
    manager.deselect_all()
    assert manager.selected_items() == []


def test_type_filter_and_range_do_not_change_other_selected_items():
    manager = SelectionManager([item(1), item(2, "video"), item(3, "audio"), item(4)])
    manager.select_range(1, 3)
    assert [value["id"] for value in manager.filter(media_type="photo")] == ["1", "4"]
    assert manager.selected_ids() == ["gallery:2", "gallery:3", "gallery:4"]
    manager.select_range(2, 2, selected=False)
    assert manager.selected_ids() == ["gallery:2", "gallery:4"]


def test_pagination_reuses_ids_and_keeps_selection_without_cross_source_collisions():
    manager = SelectionManager([item(1), item(2)])
    manager.select("gallery:1")
    manager.extend([item(2), item(3), item(1, source="other")])
    assert len(manager) == 4
    assert manager.is_selected("gallery:1") and not manager.is_selected("other:1")
    manager.replace_items([item(1), item(3)])
    assert manager.selected_ids() == ["gallery:1"]


def test_large_collection_selection_is_metadata_only_and_supports_all_ranges():
    manager = SelectionManager(item(index) for index in range(5000))
    manager.select_all()
    manager.select_range(80, 4919, selected=False)
    assert len(manager.selected_items()) == 160
    assert len(manager.filter()) == 5000
