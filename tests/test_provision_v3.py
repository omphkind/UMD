import hashlib
from pathlib import Path

import pytest

from scripts import provision_tools as provision


def test_verified_tool_cache_never_downloads_again(monkeypatch, tmp_path):
    tool = tmp_path / "gallery-dl.exe"
    payload = b"verified upstream binary fixture"
    tool.write_bytes(payload)
    monkeypatch.setattr(provision, "download", lambda url: pytest.fail("Verified cache should not download"))
    assert provision.cached_tool(tool, "https://official.example/tool", hashlib.sha256(payload).hexdigest()) == tool


def test_tampered_cache_is_replaced_only_with_verified_official_payload(monkeypatch, tmp_path):
    tool = tmp_path / "gallery-dl.exe"
    tool.write_bytes(b"tampered cache")
    official = b"official tool fixture"
    urls = []
    monkeypatch.setattr(provision, "download", lambda url: urls.append(url) or official)
    provision.cached_tool(tool, "https://official.example/pinned-tool", hashlib.sha256(official).hexdigest())
    assert urls == ["https://official.example/pinned-tool"]
    assert tool.read_bytes() == official
    assert not tool.with_name("gallery-dl.exe.download").exists()


def test_wrong_download_checksum_leaves_existing_tool_untouched(monkeypatch, tmp_path):
    tool = tmp_path / "gallery-dl.exe"
    tool.write_bytes(b"old file")
    monkeypatch.setattr(provision, "download", lambda url: b"unexpected payload")
    with pytest.raises(RuntimeError, match="checksum"):
        provision.cached_tool(tool, "https://official.example/tool", hashlib.sha256(b"expected").hexdigest())
    assert tool.read_bytes() == b"old file"


def test_gallery_provision_records_pinned_source_checksum_and_redistribution_notices(monkeypatch, tmp_path):
    binary = b"gallery binary fixture"
    license_text = b"GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991\nFixture license"
    monkeypatch.setattr(provision, "GALLERY_DL_SHA256", hashlib.sha256(binary).hexdigest())
    requested = []
    def upstream(url):
        requested.append(url)
        if url == provision.GALLERY_DL_URL:
            return binary
        assert url == provision.GALLERY_DL_LICENSE_URL
        return license_text
    monkeypatch.setattr(provision, "download", upstream)
    metadata = provision.provision_gallery(tmp_path)
    assert metadata["source"].endswith("b11951527bdb6f84e844075dca8332d920376236")
    assert metadata["version"] == "1.33.0-dev:2026.10.05"
    assert (tmp_path / "gallery-dl.exe").read_bytes() == binary
    assert (tmp_path / "licenses/gallery-dl-LICENSE.txt").read_bytes() == license_text
    notices = (tmp_path / "licenses/gallery-dl-SOURCE.txt").read_text(encoding="utf-8")
    assert metadata["sha256"] in notices and metadata["dependencies_source"] in notices
    provision.provision_gallery(tmp_path)
    assert requested.count(provision.GALLERY_DL_URL) == 1


def test_wrong_upstream_license_is_rejected(monkeypatch, tmp_path):
    binary = b"binary fixture"
    (tmp_path / "gallery-dl.exe").write_bytes(binary)
    monkeypatch.setattr(provision, "GALLERY_DL_SHA256", hashlib.sha256(binary).hexdigest())
    monkeypatch.setattr(provision, "download", lambda url: b"Unexpected HTML error page")
    with pytest.raises(RuntimeError, match="license text"):
        provision.provision_gallery(tmp_path)
