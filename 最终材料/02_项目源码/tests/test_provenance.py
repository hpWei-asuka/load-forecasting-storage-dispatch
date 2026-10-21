"""Check data download and provenance entirely with in-memory archives."""

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from load_storage import provenance


def archive_bytes(*, include_dataset=True):
    raw = b"Date;Time;Global_active_power\n07/01/2010;00:00:00;1.0\n"
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        if include_dataset:
            archive.writestr("household_power_consumption.txt", raw)
        # An archive can contain other paths; only the named dataset is read.
        archive.writestr("../unexpected.txt", b"must not be extracted")
    return raw, output.getvalue()


def mock_download(monkeypatch, archive):
    calls = []

    def fake_urlopen(url, *, timeout):
        calls.append((url, timeout))
        return BytesIO(archive)

    monkeypatch.setattr(provenance, "urlopen", fake_urlopen)
    return calls


def test_explicit_download_preserves_file_and_verifiable_source_record(tmp_path, monkeypatch):
    contents, archive = archive_bytes()
    calls = mock_download(monkeypatch, archive)
    destination = tmp_path / "raw" / "household_power_consumption.txt"
    before = datetime.now(timezone.utc)
    record = provenance.download_data(destination)
    after = datetime.now(timezone.utc)

    assert calls == [(provenance.ARCHIVE_URL, 120)]
    assert destination.read_bytes() == contents
    sidecar = destination.with_suffix(".txt.source.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == record
    assert record["dataset_url"] == provenance.DATASET_URL
    assert record["download_url"] == provenance.ARCHIVE_URL
    assert record["archive_member"] == "household_power_consumption.txt"
    assert record["sha256"] == hashlib.sha256(contents).hexdigest()
    assert record["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    assert record["size_bytes"] == len(contents)
    downloaded = datetime.fromisoformat(record["downloaded_at_utc"])
    assert downloaded.tzinfo == timezone.utc
    assert before <= downloaded <= after
    assert record["power_field"] == "Global_active_power"
    assert record["power_unit"] == "kW"
    assert sorted(path.name for path in destination.parent.iterdir()) == [destination.name, sidecar.name]
    assert not (tmp_path / "unexpected.txt").exists()


@pytest.mark.parametrize("existing", ["data", "sidecar"])
def test_explicit_download_refuses_to_overwrite_existing_data_or_record(tmp_path, monkeypatch, existing):
    destination = tmp_path / "household_power_consumption.txt"
    sidecar = destination.with_suffix(".txt.source.json")
    existing_path = destination if existing == "data" else sidecar
    existing_path.write_bytes(b"preserve existing content")

    def forbidden_network(*args, **kwargs):
        pytest.fail("Existing files must be rejected before any network call")

    monkeypatch.setattr(provenance, "urlopen", forbidden_network)
    with pytest.raises(ValueError, match="不能覆盖"):
        provenance.download_data(destination)
    assert existing_path.read_bytes() == b"preserve existing content"
    assert not (sidecar if existing == "data" else destination).exists()


def test_download_exclusive_creation_preserves_file_added_concurrently(tmp_path, monkeypatch):
    destination = tmp_path / "household_power_consumption.txt"
    _, archive = archive_bytes()

    def concurrent_urlopen(*args, **kwargs):
        destination.write_bytes(b"user file created during download")
        return BytesIO(archive)

    monkeypatch.setattr(provenance, "urlopen", concurrent_urlopen)
    with pytest.raises(FileExistsError):
        provenance.download_data(destination)
    assert destination.read_bytes() == b"user file created during download"
    assert not destination.with_suffix(".txt.source.json").exists()


def test_download_rejects_archive_without_named_dataset(tmp_path, monkeypatch):
    _, archive = archive_bytes(include_dataset=False)
    mock_download(monkeypatch, archive)
    destination = tmp_path / "raw" / "household_power_consumption.txt"
    with pytest.raises(KeyError):
        provenance.download_data(destination)
    assert not destination.exists()
    assert not destination.with_suffix(".txt.source.json").exists()
    assert list(destination.parent.iterdir()) == []


def test_data_metadata_verifies_sidecar_and_preserves_declared_and_recorded_source(tmp_path, monkeypatch):
    contents, archive = archive_bytes()
    mock_download(monkeypatch, archive)
    destination = tmp_path / "household_power_consumption.txt"
    source = provenance.download_data(destination)
    declared = "https://example.org/user-declared-source"
    metadata = provenance.data_metadata(destination, declared)

    assert metadata["declared_source_url"] == declared
    assert metadata["source_record"] == source
    assert metadata["source_record"]["dataset_url"] == provenance.DATASET_URL
    assert metadata["sha256"] == hashlib.sha256(contents).hexdigest()
    assert metadata["size_bytes"] == len(contents)
    assert metadata["downloaded_at_utc"] == source["downloaded_at_utc"]
    assert metadata["provenance_note"] == "已校验下载记录"


def test_data_metadata_rejects_content_changed_since_download(tmp_path, monkeypatch):
    _, archive = archive_bytes()
    mock_download(monkeypatch, archive)
    destination = tmp_path / "household_power_consumption.txt"
    provenance.download_data(destination)
    destination.write_bytes(b"changed after download")
    with pytest.raises(ValueError, match="SHA-256.*不一致"):
        provenance.data_metadata(destination, provenance.DATASET_URL)


def test_local_data_without_sidecar_has_explicitly_unknown_download_date(tmp_path):
    destination = tmp_path / "local.txt"
    contents = b"locally supplied dataset"
    destination.write_bytes(contents)
    metadata = provenance.data_metadata(destination, provenance.DATASET_URL)
    assert metadata["source_record"] is None
    assert metadata["downloaded_at_utc"] is None
    assert "下载日期未知" in metadata["provenance_note"]
    assert metadata["sha256"] == hashlib.sha256(contents).hexdigest()
    assert metadata["power_field"] == "Global_active_power"
    assert metadata["power_unit"] == "kW"
