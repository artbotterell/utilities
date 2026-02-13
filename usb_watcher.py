#!/usr/bin/env python3
"""
usb_watch_events_with_devnodes.py — USB connect/disconnect watcher for macOS
Prints only on changes. Includes /dev paths when present (USB-serial), using timing correlation.

Why timing correlation?
On macOS the IOSerialBSDClient node often doesn't carry a stable/matching locationID,
so mapping by locationID is unreliable. Instead we diff /dev and associate changes near the USB event.

Usage:
  chmod +x usb_watch_events_with_devnodes.py
  ./usb_watch_events_with_devnodes.py
  ./usb_watch_events_with_devnodes.py --interval 0.25 --settle 1.0
"""

import argparse
import glob
import re
import subprocess
import sys
import time
from datetime import datetime


TREE_LINE = re.compile(r"^(?P<indent>[\|\s]*)(?P<node>\+\-o)\s+(?P<name>.+?)(?:\s+<.*)?\s*$")
USB_BLOCK_START = re.compile(
    r"^\s*[\|\s]*\+\-o\s+(?P<name>.+?)\s+<class\s+(?P<class>[^,>]+),\s+id\s+(?P<id>0x[0-9a-fA-F]+)\b.*?>\s*$"
)
PROP_LINE = re.compile(r'^\s*"(?P<key>[^"]+)"\s*=\s*(?P<val>.+?)\s*$')


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)


def build_usb_tree_paths() -> dict[str, str]:
    txt = run(["ioreg", "-p", "IOUSB", "-w0", "-r"])
    stack: list[tuple[int, str]] = []
    out: dict[str, str] = {}

    def depth(indent: str) -> int:
        return indent.count("|")

    for line in txt.splitlines():
        m = TREE_LINE.match(line)
        if not m:
            continue
        d = depth(m.group("indent"))
        name = m.group("name").strip()
        while stack and stack[-1][0] >= d:
            stack.pop()
        stack.append((d, name))
        out[name] = "/".join(n for _, n in stack)
    return out


def snapshot_usb_devices() -> dict[str, dict]:
    """
    Key devices by IORegistry entry id (stable for lifetime of the device object).
    """
    tree_paths = build_usb_tree_paths()
    txt = run(["ioreg", "-p", "IOUSB", "-l", "-w0", "-r", "-c", "IOUSBHostDevice"])

    devices: dict[str, dict] = {}
    cur = None

    for line in txt.splitlines():
        m = USB_BLOCK_START.match(line)
        if m:
            if cur is not None:
                devices[cur["reg_id"]] = cur
            cur = {
                "node": m.group("name").strip(),
                "reg_id": m.group("id"),
                "props": {},
            }
            continue
        if cur is None:
            continue
        pm = PROP_LINE.match(line)
        if pm:
            cur["props"][pm.group("key")] = pm.group("val").strip()

    if cur is not None:
        devices[cur["reg_id"]] = cur

    # Enrich with friendly fields
    out: dict[str, dict] = {}
    for rid, d in devices.items():
        p = d["props"]
        vendor = p.get("USB Vendor Name") or p.get("kUSBVendorString") or p.get("iManufacturer")
        product = p.get("USB Product Name") or p.get("kUSBProductString") or p.get("iProduct") or d["node"]
        friendly = " / ".join([x for x in [vendor, product] if x]) or product
        out[rid] = {
            "friendly": friendly,
            "vid": p.get("idVendor"),
            "pid": p.get("idProduct"),
            "loc": p.get("locationID") or p.get("locationId") or p.get("location-id"),
            "serial": p.get("USB Serial Number") or p.get("kUSBSerialNumberString") or p.get("iSerialNumber"),
            "registry_path": tree_paths.get(d["node"]),
        }
    return out


def snapshot_serial_devnodes() -> set[str]:
    """
    Snapshot likely serial character devices.
    Many USB-UART adapters appear as /dev/cu.* (callout) and /dev/tty.* (dial-in).
    """
    devs = set(glob.glob("/dev/cu.*")) | set(glob.glob("/dev/tty.*"))

    # Optional: reduce noise a bit by keeping ones that look USB-ish
    # (comment out this filter if you want ALL cu/tty nodes)
    keep = set()
    for d in devs:
        low = d.lower()
        if any(s in low for s in ("usb", "wch", "serial", "slab", "cp210", "ftdi", "uart")):
            keep.add(d)
    return keep or devs


def fmt_usb(d: dict) -> str:
    tags = []
    if d.get("vid"): tags.append(f"VID={d['vid']}")
    if d.get("pid"): tags.append(f"PID={d['pid']}")
    if d.get("loc"): tags.append(f"LOC={d['loc']}")
    if d.get("serial"): tags.append(f"SER={d['serial']}")
    tag_str = (" [" + " ".join(tags) + "]") if tags else ""
    reg = d.get("registry_path") or "(IOReg path unavailable)"
    return f"{d.get('friendly','(unknown)')}{tag_str} | IORegPath={reg}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.25, help="poll interval seconds (default 0.25)")
    ap.add_argument("--settle", type=float, default=0.75, help="seconds to wait after USB event to catch /dev changes")
    args = ap.parse_args()

    prev_usb = snapshot_usb_devices()
    prev_dev = snapshot_serial_devnodes()

    print(f"{ts()} USB watcher started (events only). interval={args.interval}s settle={args.settle}s", flush=True)

    try:
        while True:
            time.sleep(args.interval)

            cur_usb = snapshot_usb_devices()
            cur_dev = snapshot_serial_devnodes()

            added_usb = set(cur_usb) - set(prev_usb)
            removed_usb = set(prev_usb) - set(cur_usb)

            # If there's a USB event, wait a bit and re-snapshot /dev to catch the driver creating nodes.
            if added_usb or removed_usb:
                time.sleep(args.settle)
                cur_dev2 = snapshot_serial_devnodes()
                added_dev = cur_dev2 - prev_dev
                removed_dev = prev_dev - cur_dev2
                cur_dev = cur_dev2  # use settled snapshot

                for rid in sorted(added_usb):
                    print(f"{ts()} CONNECTED    {fmt_usb(cur_usb[rid])}", flush=True)
                    if added_dev:
                        for d in sorted(added_dev):
                            print(f"{ts()}   DEV+       {d}", flush=True)

                for rid in sorted(removed_usb):
                    print(f"{ts()} DISCONNECTED {fmt_usb(prev_usb[rid])}", flush=True)
                    if removed_dev:
                        for d in sorted(removed_dev):
                            print(f"{ts()}   DEV-       {d}", flush=True)

            prev_usb = cur_usb
            prev_dev = cur_dev

    except KeyboardInterrupt:
        print(f"\n{ts()} USB watcher stopped.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
