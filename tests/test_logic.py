"""Pure logic: no MinIO, no network."""

import os

import pytest

from vault import disk_state, erasure_summary, inspect_disk, safe_key, wipe_directory


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("holiday photo.jpg", "holiday-photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        (".hidden", "hidden"),
        ("naïve résumé.pdf", "nave-rsum.pdf"),
    ],
)
def test_safe_key(raw, expected):
    assert safe_key(raw) == expected


def test_safe_key_never_empty():
    assert safe_key("???").startswith("file-")


@pytest.mark.parametrize(
    "online, can_read, can_write",
    [(4, True, True), (3, True, True), (2, True, False), (1, False, False)],
)
def test_erasure_quorums_for_four_disks(online, can_read, can_write):
    s = erasure_summary(total=4, parity=2, online=online)
    assert (s["can_read"], s["can_write"]) == (can_read, can_write)
    assert s["data_pieces"] == 2
    assert s["write_quorum"] == 3  # data == parity, so one extra disk is required
    assert s["overhead"] == 2.0


def test_erasure_quorums_for_eight_disks():
    s = erasure_summary(total=8, parity=3, online=5)
    assert s["data_pieces"] == 5
    assert s["write_quorum"] == 5
    assert s["can_read"] and s["can_write"]


def test_disk_state():
    ok = {"state": "ok"}
    assert disk_state(ok, True, 0) == "online"
    assert disk_state(ok, True, 2) == "rebuilding"
    assert disk_state({"state": "ok", "healing": True}, True, 0) == "rebuilding"
    assert disk_state({"state": "unformatted"}, True, 0) == "failed"
    assert disk_state(None, True, 0) == "failed"
    assert disk_state(ok, True, 0, formatted=False) == "failed"
    assert disk_state(ok, False, 0) == "unknown"


def test_wipe_directory_removes_everything_including_hidden(tmp_path):
    disk = tmp_path / "disk1"
    (disk / ".minio.sys").mkdir(parents=True)
    (disk / ".minio.sys" / "format.json").write_text("{}")
    (disk / "vault" / "a.jpg").mkdir(parents=True)
    (disk / "vault" / "a.jpg" / "xl.meta").write_bytes(b"x")
    (disk / "loose.txt").write_text("x")

    assert wipe_directory(str(disk)) == 3
    assert disk.exists()
    assert os.listdir(disk) == []


def test_inspect_disk_counts_pieces(tmp_path):
    disk = tmp_path / "disk1"
    (disk / ".minio.sys").mkdir(parents=True)
    (disk / ".minio.sys" / "format.json").write_text("{}")
    big = disk / "vault" / "photo.jpg"
    (big / "uuid-1").mkdir(parents=True)
    (big / "xl.meta").write_bytes(b"m" * 100)
    (big / "uuid-1" / "part.1").write_bytes(b"p" * 1000)
    small = disk / "vault" / "note.txt"
    small.mkdir(parents=True)
    (small / "xl.meta").write_bytes(b"m" * 50)  # small files live inside xl.meta

    info = inspect_disk(str(disk), "vault", ["photo.jpg", "note.txt", "missing.pdf"])
    assert info["formatted"] is True
    assert info["pieces"] == {"photo.jpg": 1100, "note.txt": 50}
    assert info["bytes"] == 1150


def test_inspect_wiped_disk(tmp_path):
    info = inspect_disk(str(tmp_path), "vault", ["photo.jpg"])
    assert info == {"formatted": False, "pieces": {}, "bytes": 0}
