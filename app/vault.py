"""Core logic for ShardVault.

Talks to MinIO in two ways:
  * the standard Amazon S3 API (through boto3, the official AWS SDK), for
    uploading, listing, downloading and deleting files;
  * the MinIO admin API, to read the health of each disk and to start a repair.

It also looks directly at the four disk folders, so the dashboard can show
which piece of which file lives on which disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config as BotoConfig
from botocore.credentials import Credentials
from botocore.exceptions import BotoCoreError, ClientError

CHUNK = 1024 * 1024


class VaultUnavailable(Exception):
    """MinIO cannot serve the request right now (too many disks lost, or not started)."""


class NotFound(Exception):
    """The requested file does not exist."""


@dataclass
class Settings:
    endpoint: str | None = "http://localhost:9000"
    access_key: str | None = "shardvault"
    secret_key: str | None = "shardvault-secret"
    region: str = "us-east-1"
    bucket: str = "vault"
    storage_dir: str | None = None
    disk_count: int = 4
    console_url: str = "http://localhost:9001"

    @classmethod
    def from_env(cls) -> Settings:
        endpoint = os.environ.get("S3_ENDPOINT", "http://localhost:9000") or None
        # With no endpoint the app talks to Amazon S3 and boto3 finds AWS
        # credentials the usual way (environment, ~/.aws, IAM role).
        default_key, default_secret = ("shardvault", "shardvault-secret") if endpoint else (None, None)
        return cls(
            endpoint=endpoint,
            access_key=os.environ.get("MINIO_ROOT_USER", default_key),
            secret_key=os.environ.get("MINIO_ROOT_PASSWORD", default_secret),
            region=os.environ.get("S3_REGION", "us-east-1"),
            bucket=os.environ.get("BUCKET", "vault"),
            storage_dir=os.environ.get("STORAGE_DIR") or None,
            disk_count=int(os.environ.get("DISK_COUNT", "4")),
            console_url=os.environ.get("MINIO_CONSOLE_URL", "http://localhost:9001"),
        )


# --------------------------------------------------------------------------
# Pure helpers (unit-tested without MinIO)
# --------------------------------------------------------------------------
def safe_key(filename: str) -> str:
    """Turn an uploaded file name into a safe, flat object key."""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^A-Za-z0-9._-]", "", name).lstrip(".")
    return name[:120] or f"file-{int(time.time())}"


def erasure_summary(total: int, parity: int, online: int) -> dict:
    """What erasure coding allows with `online` of `total` disks working.

    MinIO splits every file into (total - parity) data pieces and `parity`
    parity pieces. Any `data` pieces are enough to rebuild the file. Writes
    need one extra disk when data == parity, so two halves of a split
    cluster can never both accept writes.
    """
    data = total - parity
    read_quorum = data
    write_quorum = data + 1 if data == parity else data
    return {
        "total": total,
        "online": online,
        "data_pieces": data,
        "parity_pieces": parity,
        "can_lose": parity,
        "read_quorum": read_quorum,
        "write_quorum": write_quorum,
        "can_read": online >= read_quorum,
        "can_write": online >= write_quorum,
        "overhead": round(total / data, 2) if data else None,
    }


def disk_state(admin_drive: dict | None, has_admin: bool, missing_pieces: int, formatted: bool | None = None) -> str:
    """Combine MinIO's view of a disk with what is actually on it.

    A disk without MinIO's format file has just been wiped, so it is reported
    as failed straight away, even before MinIO itself notices.
    """
    if not has_admin:
        return "unknown"
    if formatted is False:
        return "failed"
    if admin_drive is None or admin_drive.get("state") != "ok":
        return "failed"
    if admin_drive.get("healing") or missing_pieces:
        return "rebuilding"
    return "online"


def wipe_directory(path: str) -> int:
    """Delete everything inside `path` (including hidden files) but keep the folder."""
    removed = 0
    if not os.path.isdir(path):
        return removed
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            os.remove(entry.path)
        removed += 1
    return removed


def inspect_disk(disk_path: str, bucket: str, keys: list[str]) -> dict:
    """Which file pieces are physically present on one disk, and their sizes."""
    pieces = {}
    for key in keys:
        obj_dir = os.path.join(disk_path, bucket, key)
        if not os.path.isfile(os.path.join(obj_dir, "xl.meta")):
            continue
        size = 0
        for root, _dirs, files in os.walk(obj_dir):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        pieces[key] = size
    return {
        "formatted": os.path.isfile(os.path.join(disk_path, ".minio.sys", "format.json")),
        "pieces": pieces,
        "bytes": sum(pieces.values()),
    }


# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------
@dataclass
class Vault:
    settings: Settings
    _s3: object = field(default=None, init=False, repr=False)
    _bucket_ready: bool = field(default=False, init=False, repr=False)

    # ---------------------------------------------------------------- S3 API
    @property
    def s3(self):
        if self._s3 is None:
            s = self.settings
            self._s3 = boto3.client(
                "s3",
                endpoint_url=s.endpoint,
                aws_access_key_id=s.access_key,
                aws_secret_access_key=s.secret_key,
                region_name=s.region,
                config=BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=30,
                ),
            )
        return self._s3

    def _call(self, fn, *args, **kwargs):
        """Run an S3 call and translate failures into friendly exceptions."""
        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise NotFound(str(exc)) from exc
            if code in ("NoSuchBucket",):
                self._bucket_ready = False
            raise VaultUnavailable(self._explain(code, exc)) from exc
        except BotoCoreError as exc:
            raise VaultUnavailable("Cannot reach MinIO. Is the minio container running?") from exc

    @staticmethod
    def _explain(code: str, exc: Exception) -> str:
        if code in ("SlowDownWrite", "XMinioWriteQuorum", "InsufficientWriteQuorum"):
            return "Not enough healthy disks to store new files safely. Downloads still work."
        if code in ("SlowDownRead", "XMinioReadQuorum", "InsufficientReadQuorum", "NoSuchBucket"):
            return "Too many disks are lost: the data cannot be read until disks are repaired."
        return f"Storage error ({code or type(exc).__name__})."

    def ensure_bucket(self):
        if self._bucket_ready:
            return
        try:
            self.s3.head_bucket(Bucket=self.settings.bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket", "NotFound"):
                self._call(self.s3.create_bucket, Bucket=self.settings.bucket)
            else:
                raise VaultUnavailable(self._explain(exc.response["Error"].get("Code", ""), exc)) from exc
        except BotoCoreError as exc:
            raise VaultUnavailable("Cannot reach MinIO. Is the minio container running?") from exc
        self._bucket_ready = True

    def list_files(self) -> list[dict]:
        self.ensure_bucket()
        files = []
        paginator = self.s3.get_paginator("list_objects_v2")
        pages = self._call(lambda: list(paginator.paginate(Bucket=self.settings.bucket)))
        for page in pages:
            for obj in page.get("Contents", []):
                files.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "modified": obj["LastModified"].isoformat(),
                        "etag": obj["ETag"].strip('"'),
                    }
                )
        files.sort(key=lambda f: f["modified"], reverse=True)
        return files

    def upload(self, stream, filename: str, content_type: str | None = None) -> dict:
        """Store a file. Its SHA-256 is saved as metadata to prove integrity later."""
        self.ensure_bucket()
        key = safe_key(filename)
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
        stream.seek(0)
        extra = {"Metadata": {"sha256": digest.hexdigest(), "original-name": filename[:200]}}
        if content_type:
            extra["ContentType"] = content_type
        self._call(self.s3.upload_fileobj, stream, self.settings.bucket, key, ExtraArgs=extra)
        return {"key": key, "size": size, "sha256": digest.hexdigest()}

    def open(self, key: str) -> dict:
        """Start a download. Returns the S3 response with a streaming Body."""
        self.ensure_bucket()
        return self._call(self.s3.get_object, Bucket=self.settings.bucket, Key=key)

    def verify(self, key: str) -> dict:
        """Read the whole file back and compare its SHA-256 with the one saved at upload."""
        started = time.perf_counter()
        obj = self.open(key)
        expected = obj.get("Metadata", {}).get("sha256")
        digest = hashlib.sha256()
        size = 0
        for chunk in obj["Body"].iter_chunks(CHUNK):
            digest.update(chunk)
            size += len(chunk)
        actual = digest.hexdigest()
        return {
            "key": key,
            "size": size,
            "expected": expected,
            "actual": actual,
            "intact": expected == actual if expected else None,
            "seconds": round(time.perf_counter() - started, 3),
        }

    def delete(self, key: str):
        self.ensure_bucket()
        self._call(self.s3.delete_object, Bucket=self.settings.bucket, Key=key)

    # ------------------------------------------------------------- admin API
    def _admin(self, method: str, path: str, body: bytes = b"", timeout: float = 5):
        """Call the MinIO admin API, signed with AWS Signature Version 4."""
        if not self.settings.endpoint or not self.settings.access_key:
            raise VaultUnavailable("The admin API needs S3_ENDPOINT to point at MinIO.")
        url = f"{self.settings.endpoint.rstrip('/')}/minio/admin/v3/{path}"
        request = AWSRequest(
            method=method,
            url=url,
            data=body,
            headers={"x-amz-content-sha256": hashlib.sha256(body).hexdigest()},
        )
        creds = Credentials(self.settings.access_key, self.settings.secret_key)
        SigV4Auth(creds, "s3", self.settings.region).add_auth(request)
        req = urllib.request.Request(url, data=body or None, method=method, headers=dict(request.headers.items()))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise VaultUnavailable("Cannot reach the MinIO admin API.") from exc

    def cluster_info(self) -> dict | None:
        """Disk states and erasure settings as MinIO reports them, or None if unreachable."""
        try:
            info = self._admin("GET", "info")
        except VaultUnavailable:
            return None
        drives = {}
        for server in info.get("servers", []):
            for drive in server.get("drives", []):
                match = re.search(r"(\d+)/*$", drive.get("endpoint") or drive.get("path") or "")
                if match:
                    drives[int(match.group(1))] = drive
        backend = info.get("backend", {})
        return {"drives": drives, "parity": backend.get("standardSCParity")}

    def repair(self) -> dict:
        """Ask MinIO to check every disk and rebuild missing pieces now (like `mc admin heal -r`)."""
        body = json.dumps({"recursive": True, "dryRun": False, "remove": False, "scanMode": 1}).encode()
        return self._admin("POST", "heal/?forceStart=true", body, timeout=15)

    # ----------------------------------------------------------------- disks
    def disk_path(self, number: int) -> str:
        if not self.settings.storage_dir:
            raise VaultUnavailable("STORAGE_DIR is not set, so the disks cannot be reached.")
        if not 1 <= number <= self.settings.disk_count:
            raise NotFound(f"There is no disk {number}.")
        return os.path.join(self.settings.storage_dir, f"disk{number}")

    def destroy_disk(self, number: int) -> int:
        """Permanently delete everything stored on one disk, like a drive failing."""
        return wipe_directory(self.disk_path(number))

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        cluster = self.cluster_info()
        has_admin = cluster is not None
        drives = cluster["drives"] if cluster else {}
        total = self.settings.disk_count
        parity = (cluster or {}).get("parity") or total // 2

        files, files_error = [], None
        try:
            files = self.list_files()
        except VaultUnavailable as exc:
            files_error = str(exc)
        keys = [f["key"] for f in files]

        disks = []
        for n in range(1, total + 1):
            on_disk = None
            if self.settings.storage_dir:
                on_disk = inspect_disk(os.path.join(self.settings.storage_dir, f"disk{n}"), self.settings.bucket, keys)
            missing = sum(1 for k in keys if on_disk and k not in on_disk["pieces"]) if on_disk else 0
            state = disk_state(drives.get(n), has_admin, missing, on_disk["formatted"] if on_disk else None)
            disks.append(
                {
                    "number": n,
                    "state": state,
                    "minio_state": (drives.get(n) or {}).get("state") if has_admin else None,
                    "pieces": on_disk["pieces"] if on_disk else None,
                    "bytes": on_disk["bytes"] if on_disk else None,
                    "missing": missing,
                }
            )

        for f in files:
            f["on_disks"] = [n["number"] for n in disks if n["pieces"] is not None and f["key"] in n["pieces"]]

        online = sum(1 for d in disks if d["state"] in ("online", "rebuilding"))
        summary = erasure_summary(total, parity, online if has_admin else 0)
        logical = sum(f["size"] for f in files)
        raw = sum(d["bytes"] or 0 for d in disks)
        return {
            "reachable": has_admin,
            "erasure": summary,
            "disks": disks,
            "files": files,
            "files_error": files_error,
            "usage": {"logical": logical, "raw": raw},
            "inspectable": bool(self.settings.storage_dir),
            "time": time.time(),
        }
