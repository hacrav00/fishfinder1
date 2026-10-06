#!/usr/bin/env python3
"""
router_autoconnect.py — Universal Multi-Router Auto-Detection & Discovery Daemon
================================================================================
Allows the Raspberry Pi to automatically recognize ANY router it is plugged into
(COFE 192.168.150.1, D-Link 192.168.0.1, Home/Office 192.168.1.1, Direct Cable
192.168.50.1, or any future router) and immediately connect with both the
Mobile App and Laptop App without manual reconfiguration.

How it works:
1. Pre-binds permanent static aliases on eth0 for known routers:
   - 192.168.150.131/24 (COFE CF-707 WF Router - 192.168.150.1)
   - 192.168.0.131/24   (D-Link Router - 192.168.0.1)
   - 192.168.1.131/24   (Standard 192.168.1.1 Router)
   - 192.168.50.1/24    (Direct Laptop Ethernet Cable)
2. Dynamic Router Subnet Watcher:
   - Monitors DHCP leases and ARP/Gateway tables on eth0 & wlan0.
   - If connected to ANY new router (e.g., 192.168.X.1 or 10.X.Y.1), it
     automatically binds <subnet>.131/24 on eth0 so the Pi is ALWAYS at .131!
3. UDP Auto-Discovery Responder & Beacon (Port 50005):
   - Replies to Laptop/Mobile discovery broadcasts with the Pi's active IPs.
"""

import os
import re
import sys
import json
import time
import socket
import struct
import threading
import subprocess

DISCOVERY_PORT = 50005
KNOWN_STATIC_IPS = [
    "192.168.150.131/24",  # COFE CF-707 WF Router (192.168.150.1)
    "192.168.0.131/24",    # D-Link Router (192.168.0.1)
    "192.168.1.131/24",    # Standard Router (192.168.1.1)
    "192.168.50.1/24",     # Direct Laptop Ethernet Cable
]


def run_cmd(cmd):
    try:
        res = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5
        )
        return res.stdout.strip()
    except Exception:
        return ""


def get_active_ips():
    """Returns a list of all active IPv4 addresses on eth0 and wlan0."""
    out = run_cmd("ip -4 -o addr show")
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            iface = parts[1]
            if iface == "lo":
                continue
            cidr = parts[3]
            ip = cidr.split("/")[0]
            if not ip.startswith("169.254.") and not ip.startswith("127."):
                ips.append((iface, ip, cidr))
    return ips


def ensure_nmcli_persistence():
    """Configure NetworkManager rov-eth profile so settings survive reboot."""
    con_list = run_cmd("nmcli -t -f NAME,DEVICE con show")
    eth_con = "rov-eth"
    for line in con_list.splitlines():
        if ":eth0" in line:
            eth_con = line.split(":")[0]
            break

    # Enable DHCP (auto) + attach all known static router IPs
    addrs_csv = ",".join(KNOWN_STATIC_IPS)
    run_cmd(f'sudo nmcli con mod "{eth_con}" ipv4.method auto ipv4.addresses "{addrs_csv}" ipv4.may-fail yes')
    run_cmd(f'sudo nmcli con up "{eth_con}"')


def sync_router_subnets():
    """
    Checks eth0 link, DHCP leases, and neighbor gateways.
    Ensures all KNOWN_STATIC_IPS are bound on eth0, and if any new router subnet
    is detected (via DHCP or ARP), automatically binds <subnet>.131/24 on eth0.
    """
    run_cmd("sudo ip link set eth0 up")

    current = get_active_ips()
    current_cidrs = {cidr for _, _, cidr in current}
    current_ips = {ip for _, ip, _ in current}

    # 1. Ensure all known static IPs are bound on eth0 right now
    for cidr in KNOWN_STATIC_IPS:
        ip = cidr.split("/")[0]
        if ip not in current_ips:
            run_cmd(f"sudo ip addr add {cidr} dev eth0 2>/dev/null || true")

    # 2. Detect any dynamic router subnet from DHCP or ARP table on eth0
    detected_prefixes = set()
    for iface, ip, _ in current:
        if iface == "eth0":
            parts = ip.split(".")
            if len(parts) == 4:
                detected_prefixes.add(".".join(parts[:3]))

    # Also check ip route / ip neigh on eth0 for router gateway (e.g. 192.168.150.1)
    routes = run_cmd("ip -4 route show dev eth0") + "\n" + run_cmd("ip -4 neigh show dev eth0")
    for match in re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b", routes):
        if not match.startswith("169.254") and not match.startswith("224.") and not match.startswith("255."):
            detected_prefixes.add(match)

    # Automatically bind .131 on every detected router subnet!
    for prefix in detected_prefixes:
        target_ip = f"{prefix}.131"
        target_cidr = f"{target_ip}/24"
        if target_ip not in current_ips:
            print(f"[Router-AutoConnect] New router subnet {prefix}.x detected! Binding {target_cidr} on eth0...")
            run_cmd(f"sudo ip addr add {target_cidr} dev eth0 2>/dev/null || true")


def udp_discovery_responder():
    """Listens on UDP port 50005 and replies to mobile/laptop discovery requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except Exception as e:
        print(f"[Router-AutoConnect] UDP port {DISCOVERY_PORT} already bound: {e}")
        return

    hostname = socket.gethostname()
    print(f"[Router-AutoConnect] UDP Discovery Responder listening on port {DISCOVERY_PORT}...")

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            msg = data.decode("utf-8", errors="ignore").strip()
            if "FISHFINDER" in msg.upper() or "DISCOVER" in msg.upper() or "PING" in msg.upper():
                ips = [ip for _, ip, _ in get_active_ips()]
                payload = json.dumps({
                    "service": "fishfinder-rov",
                    "hostname": f"{hostname}.local",
                    "ips": ips,
                    "stream_port": 8000,
                    "control_port": 8080,
                    "status": "online"
                }).encode("utf-8")
                sock.sendto(payload, addr)
        except Exception:
            time.sleep(0.5)


def udp_beacon_broadcaster():
    """Broadcasts presence every 3 seconds on all active subnets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    hostname = socket.gethostname()

    while True:
        try:
            active = get_active_ips()
            ips = [ip for _, ip, _ in active]
            payload = json.dumps({
                "service": "fishfinder-rov",
                "hostname": f"{hostname}.local",
                "ips": ips,
                "stream_port": 8000,
                "control_port": 8080,
                "status": "online"
            }).encode("utf-8")

            # Broadcast to global and subnet broadcast addresses
            bcast_targets = {"255.255.255.255"}
            for _, ip, _ in active:
                parts = ip.split(".")
                if len(parts) == 4:
                    bcast_targets.add(f"{parts[0]}.{parts[1]}.{parts[2]}.255")

            for bcast in bcast_targets:
                try:
                    sock.sendto(payload, (bcast, DISCOVERY_PORT))
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(3.0)


def start_background_autoconnect():
    """Can be imported and called directly by arm_server.py."""
    def _worker():
        try:
            ensure_nmcli_persistence()
        except Exception:
            pass
        while True:
            try:
                sync_router_subnets()
            except Exception:
                pass
            time.sleep(5.0)

    t_sync = threading.Thread(target=_worker, daemon=True, name="router-sync")
    t_sync.start()

    t_resp = threading.Thread(target=udp_discovery_responder, daemon=True, name="udp-responder")
    t_resp.start()

    t_bcast = threading.Thread(target=udp_beacon_broadcaster, daemon=True, name="udp-beacon")
    t_bcast.start()


if __name__ == "__main__":
    print("=== FishFinder Universal Router Auto-Connect Service ===")
    ensure_nmcli_persistence()
    sync_router_subnets()
    print("Active IPs:", get_active_ips())
    start_background_autoconnect()
    while True:
        time.sleep(60)
