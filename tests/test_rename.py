"""Actual filesystem tests for naming, collisions and lossless rollback."""
from pathlib import Path
import errno
import json

import pytest

from app.rename.presets import PresetStore
from app.rename.sanitizer import FilenameSanitizer, utf16_length
from app.rename.service import RenameError, RenameService
from app.rename import service as rename_module


def options(**changes):
    return {"enabled": True, "template": "{title}", "rules": [], "collision_policy": "ask",
            "start": 1, "step": 1, "padding": 0, **changes}


def local(path, **metadata):
    return {"path": str(path), "filename": path.name, "metadata": metadata}


def test_metadata_variables_padding_and_unicode(tmp_path):
    item = {"source": "Bandcamp", "id": "42", "ext": "flac", "title": "東京 / Ночь",
            "artist": "Музыкант", "album": "音楽", "track_number": 2, "upload_date": "20261005"}
    plan = RenameService().preview([item], options(template="{artist} - {album} - {track_number:02} - {title} - {date} - {source_id}"), tmp_path)
    assert plan[0]["filename"] == "Музыкант - 音楽 - 02 - 東京 _ Ночь - 2026-10-05 - 42.flac"
    assert plan[0]["status"] == "ready"
    assert not list(tmp_path.iterdir())


def test_sequential_rules_numbering_and_explicit_extension(tmp_path):
    items = [{"filename": "IMG_foo instagram.jpg"}, {"filename": "IMG_bar instagram.jpg"}]
    settings = options(template="{number:03} {filename}.{ext}", start=7, step=3,
                       rules=[{"type": "replace", "from": "IMG_", "to": ""},
                              {"type": "remove", "value": " instagram"},
                              {"type": "prefix", "value": "Foto "},
                              {"type": "suffix", "value": "_original"},
                              {"type": "remove_chars", "characters": "o"},
                              {"type": "replace_spaces", "to": "_"}])
    plan = RenameService().preview(items, settings, tmp_path)
    assert [row["filename"] for row in plan] == ["Ft_007_f_riginal.jpg", "Ft_010_bar_riginal.jpg"]
    assert [row["number"] for row in plan] == [7, 10]


@pytest.mark.parametrize("name, expected", [("CON", "_CON"), ("prn.jpg", "_prn.jpg"),
    ('a:b*c?d"e<f>g|h/i\\j', "a_b_c_d_e_f_g_h_i_j"), ("abc. ", "abc"),
    ("", "untitled"), ("..", "untitled"), ("LPT1", "_LPT1"), ("Русский 日本 中文", "Русский 日本 中文")])
def test_windows_sanitizer(name, expected):
    assert FilenameSanitizer.sanitize(name) == expected


def test_local_fallback_filename_and_folder_suffix(tmp_path):
    file = tmp_path / "Фото 001.jpg"; file.write_bytes(b"photo")
    out = tmp_path / "output"
    plan = RenameService().preview([local(file)], options(template="{title}", organization_enabled=True,
                                  folder_template="{media_type}/{source}/{filename}"), out, True)
    assert plan[0]["filename"] == "Фото 001.jpg"
    assert Path(plan[0]["target_path"]) == out / "Фото 001.jpg"
    mapping = RenameService().apply(plan)
    assert mapping[str(file)] == str(out / file.name)
    assert (out / file.name).read_bytes() == b"photo"
    assert not file.exists()


def test_pre_download_organization_uses_metadata_without_source_path(tmp_path):
    plan = RenameService().preview([{"title": "Song", "ext": "mp3", "metadata": {"artist": "Artist", "album": "Album"}}],
                                  options(organization_enabled=True, folder_template="{artist}/{album}/{filename}"), tmp_path)
    assert Path(plan[0]["target_path"]) == tmp_path / "Artist" / "Album" / "Song.mp3"
    assert plan[0]["source_path"] is None
    with pytest.raises(RenameError, match="локальные"):
        RenameService().apply(plan)


@pytest.mark.parametrize("template", ["../outside", "a/../b", "C:/outside", "/absolute", "{filename}/bad"])
def test_folder_traversal_rejected(tmp_path, template):
    plan = RenameService().preview([{"title": "photo", "ext": "jpg"}], options(organization_enabled=True, folder_template=template), tmp_path)
    assert plan[0]["status"] == "error"


@pytest.mark.parametrize("template", ["{unknown}", "{title.__class__}", "{number!r}", "{number:999}", "{title:03}"])
def test_unsupported_template_is_explained(tmp_path, template):
    plan = RenameService().preview([{"title": "photo", "ext": "jpg"}], options(template=template), tmp_path)
    assert plan[0]["status"] == "error"
    assert plan[0]["conflict"]


def test_windows_case_insensitive_duplicates_and_append_number(tmp_path):
    items = [{"title": "Photo", "ext": "jpg"}, {"title": "PHOTO", "ext": "jpg"}]
    assert RenameService().preview(items, options(), tmp_path)[1]["status"] == "conflict"
    assert RenameService().preview(items, options(collision_policy="overwrite"), tmp_path)[1]["status"] == "conflict"
    (tmp_path / "pHoTo.jpg").write_bytes(b"existing")
    (tmp_path / "Photo (1).jpg").write_bytes(b"existing1")
    plan = RenameService().preview(items, options(collision_policy="append_number"), tmp_path)
    assert [row["filename"] for row in plan] == ["Photo (2).jpg", "PHOTO (3).jpg"]


@pytest.mark.parametrize("policy, status", [("ask", "conflict"), ("skip", "skipped"), ("overwrite", "ready")])
def test_existing_collision_policies(tmp_path, policy, status):
    (tmp_path / "same.jpg").write_bytes(b"existing")
    plan = RenameService().preview([{"title": "same", "ext": "jpg"}], options(collision_policy=policy), tmp_path)
    assert plan[0]["status"] == status
    assert plan[0]["skip"] is (policy == "skip")
    assert (tmp_path / "same.jpg").read_bytes() == b"existing"


def test_reserved_queued_targets(tmp_path):
    plan = RenameService().preview([{"title": "same", "ext": "jpg"}],
        options(collision_policy="append_number", reserved_paths=[str(tmp_path / "same.jpg")]), tmp_path)
    assert plan[0]["filename"] == "same (1).jpg"


def test_safe_swaps_and_cycles(tmp_path):
    paths = [tmp_path / name for name in ("one.txt", "two.txt", "three.txt")]
    for path in paths:
        path.write_text(path.stem)
    items = [local(path, title=paths[(index + 1) % len(paths)].stem) for index, path in enumerate(paths)]
    plan = RenameService().preview(items, options(), tmp_path, True)
    assert all(row["status"] == "ready" for row in plan)
    mapping = RenameService().apply(plan)
    assert len(mapping) == 3
    assert [path.read_text() for path in paths] == ["three", "one", "two"]
    assert not list(tmp_path.glob(".umd-*.tmp"))


def test_case_only_and_unchanged_names(tmp_path):
    path = tmp_path / "Foo.txt"; path.write_text("content")
    unchanged = RenameService().preview([local(path)], options(), tmp_path, True)
    assert unchanged[0]["status"] == "unchanged"
    assert RenameService().apply(unchanged) == {}
    plan = RenameService().preview([local(path, title="foo")], options(), tmp_path, True)
    assert plan[0]["status"] == "ready"
    RenameService().apply(plan)
    assert (tmp_path / "foo.txt").read_text() == "content"


def test_overwrite_only_when_selected(tmp_path):
    file = tmp_path / "original.txt"; file.write_text("new")
    target = tmp_path / "target.txt"; target.write_text("old")
    plan = RenameService().preview([local(file, title="target")], options(collision_policy="overwrite"), tmp_path, True)
    assert target.read_text() == "old"
    RenameService().apply(plan)
    assert target.read_text() == "new"
    assert not file.exists()
    assert not list(tmp_path.glob(".umd-*.tmp"))


def test_rollback_preserves_sources_and_overwritten_targets(tmp_path, monkeypatch):
    original_a = tmp_path / "a.txt"; original_a.write_text("A")
    original_b = tmp_path / "b.txt"; original_b.write_text("B")
    target_a = tmp_path / "newA.txt"; target_a.write_text("existing")
    plan = RenameService().preview([local(original_a, title="newA"), local(original_b, title="newB")],
                                  options(collision_policy="overwrite"), tmp_path, True)
    real_install = rename_module._install
    def fail_second(source, target):
        if target.name == "newB.txt":
            raise OSError("simulated disk failure")
        real_install(source, target)
    monkeypatch.setattr(rename_module, "_install", fail_second)
    with pytest.raises(RenameError, match="Изменения отменены"):
        RenameService().apply(plan)
    assert original_a.read_text() == "A"
    assert original_b.read_text() == "B"
    assert target_a.read_text() == "existing"
    assert not (tmp_path / "newB.txt").exists()
    assert not list(tmp_path.glob(".umd-*.tmp"))


def test_collisions_after_preview_fail_without_modifications(tmp_path):
    file = tmp_path / "source.txt"; file.write_text("source")
    plan = RenameService().preview([local(file, title="target")], options(), tmp_path, True)
    target = tmp_path / "target.txt"; target.write_text("race")
    with pytest.raises(RenameError, match="конфликт"):
        RenameService().apply(plan)
    assert file.read_text() == "source"
    assert target.read_text() == "race"


def test_rollback_swapped_names_without_partial_loss(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.txt", "b.txt", "c.txt")]
    for path in paths:
        path.write_text(path.stem)
    plan = RenameService().preview([local(paths[0], title="b"), local(paths[1], title="c"), local(paths[2], title="a")], options(), tmp_path, True)
    real_install = rename_module._install
    def fail_last(source, target):
        if target.name == "a.txt":
            raise OSError("failed final move")
        real_install(source, target)
    monkeypatch.setattr(rename_module, "_install", fail_last)
    with pytest.raises(RenameError):
        RenameService().apply(plan)
    assert [path.read_text() for path in paths] == ["a", "b", "c"]


def test_long_unicode_path_is_bounded_without_stripping_unicode(tmp_path):
    plan = RenameService().preview([{"title": "東京😀" * 120, "ext": "jpg"}], options(), tmp_path)
    assert plan[0]["status"] == "ready"
    assert "東京" in plan[0]["filename"]
    assert utf16_length(plan[0]["target_path"]) <= 240
    assert plan[0]["filename"].endswith(".jpg")


def test_editable_versioned_presets_roundtrip(tmp_path):
    path = tmp_path / "presets.json"
    store = PresetStore(path)
    assert store.get("Music")["template"].startswith("{artist}")
    store.upsert("Vacation Photos", options(template="Отпуск - {number:03}"))
    assert store.get("Vacation Photos")["template"] == "Отпуск - {number:03}"
    store.upsert("Music", options(template="{title}"))
    assert store.get("Music")["template"] == "{title}"
    store.delete("Video")
    assert "Video" not in store.list()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_tampered_plan_cannot_escape_output(tmp_path):
    source = tmp_path / "source.txt"; source.write_text("source")
    plan = RenameService().preview([local(source, title="new")], options(), tmp_path, True)
    plan[0]["target_path"] = str(tmp_path.parent / "outside.txt")
    with pytest.raises(RenameError, match="пределы"):
        RenameService().apply(plan)
    assert source.exists()


def test_cross_volume_install_copy_is_exclusive_and_preserves_bytes(tmp_path, monkeypatch):
    source = tmp_path / "stage.tmp"; source.write_bytes(b"large-media-content" * 500)
    target = tmp_path / "renamed.mp4"
    def cross_device(*args):
        raise OSError(errno.EXDEV, "Cross-device link")
    monkeypatch.setattr(rename_module.os, "rename" if rename_module.os.name == "nt" else "link", cross_device)
    rename_module._install(source, target)
    assert target.read_bytes() == b"large-media-content" * 500
    assert not source.exists()
    source.write_bytes(b"different")
    with pytest.raises(FileExistsError):
        rename_module._install(source, target)
    assert source.read_bytes() == b"different"
    assert target.read_bytes() == b"large-media-content" * 500


def test_staging_failure_rolls_back_every_earlier_source(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("a.txt", "b.txt")]
    for path in paths:
        path.write_text(path.stem)
    plan = RenameService().preview([local(paths[0], title="first"), local(paths[1], title="second")], options(), tmp_path, True)
    real_install = rename_module._install
    def fail_source_b(source, target):
        if source == paths[1]:
            raise PermissionError("file is held by another process")
        real_install(source, target)
    monkeypatch.setattr(rename_module, "_install", fail_source_b)
    with pytest.raises(RenameError, match="Изменения отменены"):
        RenameService().apply(plan)
    assert [path.read_text() for path in paths] == ["a", "b"]
    assert not list(tmp_path.glob(".umd-*.tmp"))


def test_missing_metadata_variables_render_empty_instead_of_inventing_data(tmp_path):
    plan = RenameService().preview([{"filename": "photo.jpg"}], options(template="{artist}-{track_number:02}-{title}-{number}", padding=3), tmp_path)
    assert plan[0]["filename"] == "--photo-001.jpg"


def test_directory_cannot_be_overwritten_by_file(tmp_path):
    (tmp_path / "name.jpg").mkdir()
    plan = RenameService().preview([{"title": "name", "ext": "jpg"}], options(collision_policy="overwrite"), tmp_path)
    assert plan[0]["status"] == "conflict"


def test_disabled_rename_keeps_original_filename_while_organizing(tmp_path):
    plan = RenameService().preview([{"title": "Fancy title", "filename": "original.mp4", "source": "YouTube"}],
                                  options(enabled=False, organization_enabled=True, folder_template="{source}"), tmp_path)
    assert Path(plan[0]["target_path"]) == tmp_path / "YouTube" / "original.mp4"


def test_local_mixed_task_files_use_actual_extensions_and_filename_variables(tmp_path):
    media = tmp_path / "download.jpg"; media.write_bytes(b"image")
    sidecar = tmp_path / "download.jpg.umd.json"; sidecar.write_text('{"title":"Photo"}')
    items = [{"path": str(file), "ext": "jpg", "original_filename": "source.jpg", "title": "Photo"}
             for file in (media, sidecar)]
    service = RenameService()
    title_plan = service.preview(items, options(template="New {title}"), tmp_path, True)
    assert [row["filename"] for row in title_plan] == ["New Photo.jpg", "New Photo.json"]
    assert [row["original_filename"] for row in title_plan] == [media.name, sidecar.name]
    filename_plan = service.preview(items, options(template="renamed {filename}"), tmp_path, True)
    assert [row["filename"] for row in filename_plan] == ["renamed download.jpg", "renamed download.jpg.umd.json"]
    service.apply(filename_plan)
    assert (tmp_path / "renamed download.jpg").read_bytes() == b"image"
    assert json.loads((tmp_path / "renamed download.jpg.umd.json").read_text(encoding="utf-8"))["title"] == "Photo"
