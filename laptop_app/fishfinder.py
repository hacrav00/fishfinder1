#!/usr/bin/env python3
"""
fishfinder.py — Native FishFinder ROV Desktop Application
==========================================================
Includes:
- Universal Multi-Router Auto-Discovery (COFE 192.168.150.1, D-Link 192.168.0.1,
  Office 192.168.1.1, Direct Ethernet 192.168.50.1, UDP Beacon 50005, and DHCP Subnet Scan)
- Automatic Background Pi Provisioning (pushes router_autoconnect.py & arm_server.py
  with Autofocus/Zoom & 192.168.150.131 binding as soon as Pi is reachable)
"""
import sys
import os
import socket
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add current folder to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import tkinter as tk
from utils.logger import setup_logging
from core.storage import LOG_DIR, ensure_dirs

ensure_dirs()
setup_logging(LOG_DIR, level=logging.INFO)
log = logging.getLogger("fishfinder")

from gui.app_window import AppWindow

KNOWN_PI_HOSTS = [
    "192.168.150.131",  # COFE CF-707 WF Router static IP
    "192.168.0.131",    # D-Link Router static IP
    "192.168.1.42",     # Office Wi-Fi IP
    "192.168.1.131",    # Office Ethernet static IP
    "192.168.50.1",     # Direct Ethernet IP
    "i4mt.local",       # mDNS hostname
]

_PROVISIONED_IPS = set()


def _check_pi_port(ip: str, port: int = 8080, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _is_fishfinder_pi(ip: str, timeout: float = 0.4) -> bool:
    if _check_pi_port(ip, 8080, timeout) or _check_pi_port(ip, 8000, timeout):
        return True
    return False


def _udp_discover(timeout: float = 0.8) -> str:
    """Query Raspberry Pi UDP discovery responder on port 50005."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for bcast in ("255.255.255.255", "192.168.150.255", "192.168.0.255", "192.168.1.255"):
            try:
                sock.sendto(b"FISHFINDER_DISCOVER", (bcast, 50005))
            except Exception:
                pass
        data, addr = sock.recvfrom(1024)
        sock.close()
        if data:
            return addr[0]
    except Exception:
        pass
    return ""


def _get_local_prefixes() -> list:
    prefixes = ["192.168.150", "192.168.0", "192.168.1"]
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            parts = ip.split(".")
            if len(parts) == 4 and not ip.startswith("127."):
                p = f"{parts[0]}.{parts[1]}.{parts[2]}"
                if p not in prefixes:
                    prefixes.insert(0, p)
    except Exception:
        pass
    return prefixes


def auto_provision_pi(pi_ip: str):
    """
    Automatically provisions the Raspberry Pi in the background when discovered
    so that even if the Pi got a dynamic DHCP address on COFE 192.168.150.1,
    it immediately receives router_autoconnect.py, binds 192.168.150.131/24,
    and updates arm_server.py with Autofocus & Zoom support.
    """
    host = pi_ip.split(":")[0]
    if not host or host in _PROVISIONED_IPS:
        return
    _PROVISIONED_IPS.add(host)

    def _worker():
        try:
            import paramiko
            fish_dir = Path(r"C:\Users\talkm\Desktop\fish")
            arm_srv = fish_dir / "arm_server.py"
            router_ac = fish_dir / "router_autoconnect.py"
            if not arm_srv.exists() or not router_ac.exists():
                return

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=22, username="i4mt", password="i4mt", timeout=3.0)
            sftp = ssh.open_sftp()
            sftp.put(str(router_ac), "/home/i4mt/router_autoconnect.py")
            sftp.put(str(arm_srv), "/home/i4mt/arm_server.py")
            sftp.close()

            cmd = (
                "echo i4mt | sudo -S ip addr add 192.168.150.131/24 dev eth0 2>/dev/null || true; "
                "echo i4mt | sudo -S ip addr add 192.168.0.131/24 dev eth0 2>/dev/null || true; "
                "echo i4mt | sudo -S ip addr add 192.168.1.131/24 dev eth0 2>/dev/null || true; "
                "echo i4mt | sudo -S ip addr add 192.168.50.1/24 dev eth0 2>/dev/null || true; "
                "echo i4mt | sudo -S nmcli con mod rov-eth ipv4.method auto "
                "+ipv4.addresses '192.168.150.131/24,192.168.0.131/24,192.168.1.131/24,192.168.50.1/24' 2>/dev/null || true; "
                "echo i4mt | sudo -S systemctl restart fishfinder-arm.service 2>/dev/null || true"
            )
            ssh.exec_command(cmd)
            ssh.close()
            log.info(f"Auto-provisioned Raspberry Pi at {host} (COFE 192.168.150.131 + AF/Zoom ready)")
        except Exception as e:
            log.debug(f"Background Pi provisioning skipped for {host}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def discover_pi_ip(quick: bool = False) -> str:
    """
    Universal Raspberry Pi IP auto-discovery across any router:
    1. Fast check of known static IPs (192.168.150.131, 192.168.0.131, 192.168.1.42, etc.)
    2. UDP broadcast discovery on port 50005
    3. Parallel subnet scan across COFE router (192.168.150.1..254) and active local subnets
    """
    # Step 1: Fast parallel check of known priority hosts
    with ThreadPoolExecutor(max_workers=len(KNOWN_PI_HOSTS)) as ex:
        futures = {ex.submit(_is_fishfinder_pi, h, 0.35): h for h in KNOWN_PI_HOSTS}
        for f in as_completed(futures):
            h = futures[f]
            try:
                if f.result():
                    log.info(f"[Auto-Discovery] Found Raspberry Pi at known host: {h}")
                    auto_provision_pi(h)
                    return h
            except Exception:
                pass

    # Step 2: UDP Broadcast Discovery (port 50005)
    udp_ip = _udp_discover(timeout=0.45 if quick else 0.8)
    if udp_ip:
        log.info(f"[Auto-Discovery] Found Raspberry Pi via UDP Beacon: {udp_ip}")
        auto_provision_pi(udp_ip)
        return udp_ip

    # Step 3: Parallel Subnet Scan (catches DHCP lease on COFE 192.168.150.x or any router)
    prefixes = _get_local_prefixes()
    if quick:
        prefixes = prefixes[:2]

    for prefix in prefixes:
        candidates = [f"{prefix}.{i}" for i in range(2, 255)]
        with ThreadPoolExecutor(max_workers=64) as ex:
            futures = {ex.submit(_is_fishfinder_pi, ip, 0.25): ip for ip in candidates}
            for f in as_completed(futures):
                ip = futures[f]
                try:
                    if f.result():
                        log.info(f"[Auto-Discovery] Found Raspberry Pi on subnet scan: {ip}")
                        auto_provision_pi(ip)
                        return ip
                except Exception:
                    pass

    return "192.168.150.131"


def main():
    client_ip = None
    discover_only = "--discover-only" in sys.argv
    for i, arg in enumerate(sys.argv):
        if arg == "--client" and i + 1 < len(sys.argv):
            client_ip = sys.argv[i + 1]
        elif arg.startswith("--client="):
            client_ip = arg.split("=", 1)[1]

    if discover_only:
        ip = discover_pi_ip(quick=False)
        print(ip)
        return

    if not client_ip or client_ip.startswith("auto"):
        discovered = discover_pi_ip(quick=False)
        client_ip = f"{discovered}:8000"
    else:
        host_only = client_ip.split(":")[0]
        auto_provision_pi(host_only)

    log.info(f"Starting FishFinder Native App connecting to {client_ip}...")
    root = tk.Tk()
    app = AppWindow(root, client_ip=client_ip)
    root.mainloop()


if __name__ == "__main__":
    main()
