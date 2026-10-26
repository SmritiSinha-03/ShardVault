"""HTTP API tests.

Amazon S3 is emulated in memory by moto. Because ShardVault only speaks the
standard S3 API, the same code runs unchanged against moto, MinIO or AWS.
"""

import hashlib
import io
import json

import pytest
from moto import mock_aws

from app import create_app
from vault import Settings, Vault


@pytest.fixture
def client(tmp_path):
    for n in range(1, 5):
        (tmp_path / f"disk{n}" / ".minio.sys").mkdir(parents=True)
        (tmp_path / f"disk{n}" / ".minio.sys" / "format.json").write_text("{}")
    with mock_aws():
        vault = Vault(Settings(endpoint=None, storage_dir=str(tmp_path)))
        app = create_app(vault)
        app.config["TESTING"] = True
        yield app.test_client(), vault, tmp_path


def upload(c, name="photo.jpg", data=b"\x89PNG fake image bytes" * 1000):
    return c.post("/api/files", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_index_page(client):
    c, _, _ = client
    res = c.get("/")
    assert res.status_code == 200
    assert b"ShardVault" in res.data


def test_upload_stores_sha256_metadata(client):
    c, vault, _ = client
    data = b"hello" * 5000
    res = upload(c, "My Photo.jpg", data)
    assert res.status_code == 201
    body = res.get_json()
    assert body["key"] == "My-Photo.jpg"
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    head = vault.s3.head_object(Bucket="vault", Key="My-Photo.jpg")
    assert head["Metadata"]["sha256"] == body["sha256"]


def test_upload_without_file_is_rejected(client):
    c, _, _ = client
    assert c.post("/api/files", data={}).status_code == 400


def test_download_returns_identical_bytes(client):
    c, _, _ = client
    data = bytes(range(256)) * 400
    upload(c, "data.bin", data)
    res = c.get("/api/files/data.bin")
    assert res.status_code == 200
    assert res.data == data
    assert "attachment" in res.headers["Content-Disposition"]
    assert "inline" in c.get("/api/files/data.bin?inline=1").headers["Content-Disposition"]


def test_verify_reports_intact_file(client):
    c, _, _ = client
    upload(c, "photo.jpg")
    result = c.post("/api/files/photo.jpg/verify").get_json()
    assert result["intact"] is True
    assert result["expected"] == result["actual"]


def test_verify_detects_changed_content(client):
    c, vault, _ = client
    upload(c, "photo.jpg", b"original")
    stale = vault.s3.head_object(Bucket="vault", Key="photo.jpg")["Metadata"]
    vault.s3.put_object(Bucket="vault", Key="photo.jpg", Body=b"tampered", Metadata=stale)
    assert c.post("/api/files/photo.jpg/verify").get_json()["intact"] is False


def test_delete_and_missing_file(client):
    c, _, _ = client
    upload(c, "photo.jpg")
    assert c.delete("/api/files/photo.jpg").status_code == 200
    assert c.get("/api/files/photo.jpg").status_code == 404


def test_status_lists_files_without_minio_admin(client):
    c, _, _ = client
    upload(c, "a.jpg")
    upload(c, "b.pdf")
    status = c.get("/api/status").get_json()
    assert status["reachable"] is False  # no MinIO admin API in this test
    assert sorted(f["key"] for f in status["files"]) == ["a.jpg", "b.pdf"]
    assert [d["state"] for d in status["disks"]] == ["unknown"] * 4


def test_destroy_disk_erases_its_folder(client):
    c, _, storage = client
    assert c.post("/api/disks/2/destroy").get_json() == {"disk": 2, "removed": 1}
    assert list((storage / "disk2").iterdir()) == []
    assert (storage / "disk1" / ".minio.sys" / "format.json").exists()


def test_destroy_unknown_disk_is_404(client):
    c, _, _ = client
    assert c.post("/api/disks/9/destroy").status_code == 404


def test_repair_needs_minio(client):
    c, _, _ = client
    assert c.post("/api/repair").status_code == 503


def test_admin_requests_are_signed_and_drives_mapped(monkeypatch):
    captured = {}
    info = {
        "backend": {"standardSCParity": 2},
        "servers": [
            {"drives": [{"endpoint": f"/data{n}", "state": "ok" if n != 3 else "unformatted"} for n in (4, 1, 3, 2)]}
        ],
    }

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return FakeResponse(json.dumps(info).encode())

    monkeypatch.setattr("vault.urllib.request.urlopen", fake_urlopen)
    vault = Vault(Settings(endpoint="http://minio:9000", access_key="ak", secret_key="secretkey"))
    cluster = vault.cluster_info()

    assert captured["url"] == "http://minio:9000/minio/admin/v3/info"
    assert captured["auth"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
    assert cluster["parity"] == 2
    assert sorted(cluster["drives"]) == [1, 2, 3, 4]
    assert cluster["drives"][3]["state"] == "unformatted"
