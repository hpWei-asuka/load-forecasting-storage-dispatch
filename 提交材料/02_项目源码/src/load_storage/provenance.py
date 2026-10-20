"""Explicit data download and small, auditable provenance records."""

from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile

DATASET_URL = "https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption"
ARCHIVE_URL = "https://archive.ics.uci.edu/static/public/235/individual+household+electric+power+consumption.zip"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def download_data(destination: Path) -> dict:
    """Called only by the explicit download-data command; never by run."""
    destination = destination.resolve()
    sidecar = destination.with_suffix(destination.suffix + ".source.json")
    if destination.exists() or sidecar.exists():
        raise ValueError(f"目标已存在，不能覆盖：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="load-storage-", dir=destination.parent) as temporary:
        archive = Path(temporary) / "source.zip"
        with urlopen(ARCHIVE_URL, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with ZipFile(archive) as zipped:
            member = "household_power_consumption.txt"
            extracted = Path(temporary) / member
            # Read this one named member; do not extract arbitrary archive paths.
            with zipped.open(member) as source, extracted.open("wb") as output:
                shutil.copyfileobj(source, output)
        record = {
            "dataset_url": DATASET_URL, "download_url": ARCHIVE_URL,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": sha256_file(extracted), "archive_sha256": sha256_file(archive),
            "archive_member": member, "size_bytes": extracted.stat().st_size,
            "power_field": "Global_active_power", "power_unit": "kW",
        }
        # Exclusive creation preserves a file added concurrently by the user.
        with extracted.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        write_json(sidecar, record)
    return record


def runtime_metadata(root: Path) -> dict:
    git = {"commit": None, "dirty": None, "status": None}
    try:
        prefix = ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root)]
        commit = subprocess.run(prefix + ["rev-parse", "HEAD"], check=True,
                                capture_output=True, text=True, timeout=10)
        status = subprocess.run(prefix + ["status", "--porcelain"], check=True,
                                capture_output=True, text=True, timeout=10)
        git.update(commit=commit.stdout.strip(), dirty=bool(status.stdout.strip()), status=status.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        git["unavailable_reason"] = str(exc)
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "libraries": {name: version(name) for name in ["load-storage", "pandas", "numpy", "PuLP", "matplotlib"]},
        "git": git,
    }


def data_metadata(path: Path, declared_source_url: str) -> dict:
    digest = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".source.json")
    source = None
    if sidecar.exists():
        source = json.loads(sidecar.read_text(encoding="utf-8"))
        if source.get("sha256") != digest:
            raise ValueError(f"数据 SHA-256 与来源记录不一致：{sidecar}")
    return {
        "path": str(path), "sha256": digest, "size_bytes": path.stat().st_size,
        "declared_source_url": declared_source_url, "source_record": source,
        "downloaded_at_utc": source.get("downloaded_at_utc") if source else None,
        "provenance_note": "已校验下载记录" if source else "本地提供的文件；下载日期未知",
        "power_field": "Global_active_power", "power_unit": "kW",
        "time_basis": "数据记录的当地钟表时间，无时区转换",
    }
