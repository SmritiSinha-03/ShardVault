"""ShardVault command-line tool.

Run it inside the app container:

    docker compose exec app python cli.py status
    docker compose exec app python cli.py destroy 3
    docker compose exec app python cli.py repair
"""

from __future__ import annotations

import argparse
import sys

from vault import Settings, Vault, VaultUnavailable

LABELS = {
    "online": "online",
    "rebuilding": "rebuilding",
    "failed": "FAILED",
    "unknown": "unknown",
}


def human(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def cmd_status(vault: Vault) -> int:
    s = vault.status()
    e = s["erasure"]
    print(f"\nErasure coding: {e['data_pieces']} data + {e['parity_pieces']} parity pieces per file")
    print(f"Disks online:   {e['online']} of {e['total']}")
    print(f"Can read:       {'yes' if e['can_read'] else 'NO'}")
    print(f"Can write:      {'yes' if e['can_write'] else 'NO'}\n")
    print(f"{'Disk':<6}{'State':<12}{'Pieces':>7}{'Size':>11}")
    for d in s["disks"]:
        pieces = "-" if d["pieces"] is None else len(d["pieces"])
        print(f"{d['number']:<6}{LABELS[d['state']]:<12}{pieces!s:>7}{human(d['bytes']):>11}")
    print(
        f"\nFiles: {len(s['files'])}   stored {human(s['usage']['logical'])} using {human(s['usage']['raw'])} of disk"
    )
    if s["files_error"]:
        print(f"Warning: {s['files_error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and break ShardVault's disks.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show disk health and erasure-coding status")
    destroy = sub.add_parser("destroy", help="permanently erase one disk")
    destroy.add_argument("disk", type=int, help="disk number, 1 to 4")
    sub.add_parser("repair", help="ask MinIO to rebuild missing pieces now")
    args = parser.parse_args(argv)

    vault = Vault(Settings.from_env())
    try:
        if args.command == "status":
            return cmd_status(vault)
        if args.command == "destroy":
            removed = vault.destroy_disk(args.disk)
            print(f"Disk {args.disk} erased ({removed} top-level entries deleted).")
            print("Downloads keep working. MinIO will notice and rebuild the disk within about 20 seconds.")
            return 0
        if args.command == "repair":
            vault.repair()
            print("Repair started. Run `python cli.py status` to watch the disks come back.")
            return 0
    except VaultUnavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
