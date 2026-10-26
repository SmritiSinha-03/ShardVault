"""End-to-end resilience test against a running ShardVault.

    docker compose up -d --build
    python scripts/integration_test.py            # default http://localhost:8000

Uploads a file, destroys disks, proves the file still reads back bit-for-bit,
checks that writes are refused when too few disks remain, and waits for
MinIO to rebuild everything.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

import requests

TIMEOUT = 90


class Check:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()

    def status(self) -> dict:
        return self.s.get(f"{self.base}/api/status", timeout=15).json()

    def wait_all_online(self, label: str) -> float:
        start = time.time()
        while time.time() - start < TIMEOUT:
            try:
                st = self.status()
                if st["reachable"] and all(d["state"] == "online" for d in st["disks"]):
                    return time.time() - start
            except requests.RequestException:
                pass
            time.sleep(1)
        fail(f"{label}: disks did not all come back online within {TIMEOUT}s")

    def upload(self, name: str, data: bytes) -> requests.Response:
        return self.s.post(f"{self.base}/api/files", files={"file": (name, data, "image/jpeg")}, timeout=60)

    def verify(self, key: str) -> dict:
        r = self.s.post(f"{self.base}/api/files/{key}/verify", timeout=60)
        r.raise_for_status()
        return r.json()

    def destroy(self, disk: int):
        self.s.post(f"{self.base}/api/disks/{disk}/destroy", timeout=15).raise_for_status()


def took_text(seconds: float) -> str:
    return "in under a second" if seconds < 1 else f"in {seconds:.0f}s"


def ok(message: str):
    print(f"  \u2713 {message}")


def fail(message: str):
    print(f"  \u2717 {message}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("SHARDVAULT_URL", "http://localhost:8000"))
    args = parser.parse_args()
    c = Check(args.url)
    print(f"Testing {c.base}\n")

    # 0. Wait for the stack
    start = time.time()
    while True:
        try:
            if c.s.get(f"{c.base}/healthz", timeout=3).ok:
                break
        except requests.RequestException:
            pass
        if time.time() - start > TIMEOUT:
            fail("app did not start")
        time.sleep(1)
    c.wait_all_online("startup")
    ok("app is up and all 4 disks are online")

    # 1. Upload
    data = os.urandom(2_500_000)
    sha = hashlib.sha256(data).hexdigest()
    r = c.upload("integration-photo.jpg", data)
    if r.status_code != 201:
        fail(f"upload failed: {r.status_code} {r.text}")
    key = r.json()["key"]
    files = {f["key"]: f for f in c.status()["files"]}
    if files.get(key, {}).get("on_disks") != [1, 2, 3, 4]:
        fail(f"expected a piece on every disk, got {files.get(key)}")
    ok("uploaded a 2.5 MB file, one piece stored on each of the 4 disks")

    # 2. One disk destroyed
    c.destroy(2)
    disk2 = c.status()["disks"][1]
    if disk2["state"] == "online":
        fail("disk 2 still reported online right after being destroyed")
    v = c.verify(key)
    if not v["intact"] or v["actual"] != sha:
        fail(f"file damaged after losing 1 disk: {v}")
    ok("destroyed disk 2: file still downloads with an identical SHA-256")
    took = c.wait_all_online("auto-heal")
    ok(f"MinIO rebuilt disk 2 by itself {took_text(took)}")

    # 3. Two disks destroyed: reads OK, writes refused
    c.destroy(1)
    c.destroy(3)
    v = c.verify(key)
    if not v["intact"]:
        fail(f"file damaged after losing 2 disks: {v}")
    ok("destroyed disks 1 and 3: file still downloads intact")
    r = c.upload("should-be-refused.txt", b"x" * 1000)
    if r.status_code != 503:
        fail(f"expected the upload to be refused with only 2 disks, got {r.status_code}")
    ok("uploads are refused while only 2 disks remain (write quorum protection)")

    # 4. Repair
    c.s.post(f"{c.base}/api/repair", timeout=30).raise_for_status()
    took = c.wait_all_online("repair")
    ok(f"repair rebuilt both disks {took_text(took)}")
    r = c.upload("after-repair.txt", b"back in business")
    if r.status_code != 201:
        fail(f"upload after repair failed: {r.status_code} {r.text}")
    ok("uploads work again")

    # 5. Clean up
    for k in (key, r.json()["key"]):
        c.s.delete(f"{c.base}/api/files/{k}", timeout=15)
    print("\nAll resilience checks passed.")


if __name__ == "__main__":
    main()
