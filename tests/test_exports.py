import json

from app.core.config import Settings
from app.core.models import MediaItem, Progress
from app.export import export_all


def test_metafin_exact_format_and_url_purity(tmp_path):
    progress = Progress("youtube", ["channel"], Settings().to_dict())
    first = MediaItem("youtube", "aaa", "https://youtube.com/watch?v=aaa", 1, status="success", title="Русское название", original_title="Original=title", overview="Первая строка\n\nВторая строка")
    second = MediaItem("youtube", "bbb", "https://youtube.com/watch?v=bbb", 3, status="success", title="Название 2", overview="Описание 2")
    failed = MediaItem("youtube", "ccc", "https://youtube.com/watch?v=ccc", 2, status="error")
    progress.items = {item.key: item for item in (second, failed, first)}
    paths = export_all(progress, tmp_path)
    assert paths["urls"].read_text(encoding="utf-8") == "https://youtube.com/watch?v=aaa\nhttps://youtube.com/watch?v=bbb\n"
    assert paths["metafin"].read_text(encoding="utf-8") == "[001]\ntitle=Русское название\noriginal title=Original=title\noverview=Первая строка Вторая строка\n\n[003]\ntitle=Название 2\noriginal title=\noverview=Описание 2\n"
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert set(data["items"]) == {"youtube:aaa", "youtube:bbb"}
    assert data["items"]["youtube:aaa"]["overview"] == "Первая строка\n\nВторая строка"


def test_empty_exports_and_replacement_remove_old_content(tmp_path):
    progress = Progress("youtube", ["channel"], Settings().to_dict())
    item = MediaItem("youtube", "aaa", "https://youtube.com/watch?v=aaa", 1, status="success")
    progress.items[item.key] = item
    export_all(progress, tmp_path)
    item.status = "skipped"
    paths = export_all(progress, tmp_path)
    assert paths["urls"].read_bytes() == b""
    assert paths["metafin"].read_bytes() == b""
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["items"] == {}
