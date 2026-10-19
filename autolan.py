"""
AutoLAN - otomatis pilih IP Static (kantor) atau DHCP (rumah) saat kabel LAN dicolok.

Logika deteksi saat kabel tersambung:
  1. Adapter diset ke DHCP, tunggu sampai dapat IP (maks. ~15 detik).
  2. Jika dapat IP normal dan BUKAN di jaringan kantor  -> tetap DHCP (rumah).
  3. Jika tidak dapat IP (169.254.x / kosong) ATAU IP yang didapat ada di
     subnet kantor -> pasang profil Static kantor, lalu ping gateway kantor
     untuk konfirmasi.
Mengubah IP butuh hak Administrator.
Copyright (c) 2026 Bayu Saputra. MIT License.
"""

import ctypes
import ipaddress
import json
import os
import subprocess
import threading
import time

import winnet

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DualNet")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(cmd, timeout=30):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
    return p.returncode, (p.stdout + p.stderr).strip()


def ps(script, timeout=30):
    return run(["powershell", "-NoProfile", "-Command", script], timeout)


# --------------------------------------------------------------------------- #
#  Konfigurasi
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "port": 8899,
    "mode": 0,
    "system_proxy": True,
    "auto_start_proxy": False,
    "weights": {},
    "disabled": [],
    "saved_proxy": None,
    "autolan_enabled": False,
    "adapter": "Ethernet",
    "office": {
        "addresses": "",   # "192.168.10.50/24, 172.20.5.10/23"
        "gateway": "",
        "dns": "8.8.8.8, 1.1.1.1",
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k != "office"})
        cfg["office"].update(data.get("office", {}))
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def split_list(s):
    return [x.strip() for x in s.replace(";", ",").split(",") if x.strip()]


# --------------------------------------------------------------------------- #
#  Query adapter
# --------------------------------------------------------------------------- #
def list_ethernet_adapters():
    code, out = ps("Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' } | "
                   "Select-Object -ExpandProperty Name")
    names = [l.strip() for l in out.splitlines() if l.strip()] if code == 0 else []
    return names or ["Ethernet"]


def link_up(adapter):
    return winnet.is_up(adapter)


def _link_up_ps(adapter):
    code, out = ps(f"(Get-NetAdapter -Name '{adapter}' -ErrorAction SilentlyContinue).Status")
    return out.strip().lower() == "up"


def current_state(adapter):
    """Kembalikan dict: dhcp(bool), addresses([ip/prefix]), gateway, dns([])."""
    script = (
        f"$a='{adapter}';"
        "$i=Get-NetIPInterface -InterfaceAlias $a -AddressFamily IPv4 -ErrorAction SilentlyContinue;"
        "$ip=@(Get-NetIPAddress -InterfaceAlias $a -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" });"
        "$gw=@(Get-NetRoute -InterfaceAlias $a -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue)[0].NextHop;"
        "$dns=@((Get-DnsClientServerAddress -InterfaceAlias $a -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses);"
        "[PSCustomObject]@{Dhcp=\"$($i.Dhcp)\";Addrs=$ip;Gw=$gw;Dns=$dns} | ConvertTo-Json -Depth 3"
    )
    code, out = ps(script)
    try:
        d = json.loads(out)
    except ValueError:
        return {"dhcp": None, "addresses": [], "gateway": None, "dns": []}
    def as_list(v):
        return [] if v is None else ([v] if isinstance(v, str) else list(v))
    return {"dhcp": d.get("Dhcp") == "Enabled", "addresses": as_list(d.get("Addrs")),
            "gateway": d.get("Gw"), "dns": as_list(d.get("Dns"))}


def ping(host, count=2):
    code, out = run(["ping", "-n", str(count), "-w", "1000", host], timeout=15)
    return code == 0 and "TTL=" in out.upper()


# --------------------------------------------------------------------------- #
#  Terapkan profil
# --------------------------------------------------------------------------- #
def apply_dhcp(adapter, log):
    log(f"[AutoLAN] {adapter}: set ke DHCP")
    run(["netsh", "interface", "ipv4", "set", "address", f"name={adapter}", "source=dhcp"])
    run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={adapter}", "source=dhcp"])
    run(["ipconfig", "/renew", adapter], timeout=40)


def apply_static(adapter, office, log):
    addrs = split_list(office.get("addresses", ""))
    if not addrs:
        log("[AutoLAN] Profil kantor belum diisi, static tidak dipasang.")
        return False
    gw = office.get("gateway", "").strip()
    dns = split_list(office.get("dns", ""))
    log(f"[AutoLAN] {adapter}: pasang static kantor {', '.join(addrs)} gw {gw or '-'}")

    first = ipaddress.ip_interface(addrs[0])
    cmd = ["netsh", "interface", "ipv4", "set", "address", f"name={adapter}", "source=static",
           f"address={first.ip}", f"mask={first.netmask}"]
    if gw:
        cmd += [f"gateway={gw}", "gwmetric=1"]
    code, out = run(cmd)
    if code != 0:
        log(f"[AutoLAN] Gagal set IP: {out}")
        return False
    for a in addrs[1:]:
        i = ipaddress.ip_interface(a)
        run(["netsh", "interface", "ipv4", "add", "address", f"name={adapter}",
             f"address={i.ip}", f"mask={i.netmask}"])
    if dns:
        run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={adapter}",
             "source=static", f"address={dns[0]}", "register=primary", "validate=no"])
        for n, d in enumerate(dns[1:], start=2):
            run(["netsh", "interface", "ipv4", "add", "dnsservers", f"name={adapter}",
                 f"address={d}", f"index={n}", "validate=no"])
    return True


def in_office_net(addresses, office):
    nets = []
    for a in split_list(office.get("addresses", "")):
        try:
            nets.append(ipaddress.ip_interface(a).network)
        except ValueError:
            pass
    for a in addresses:
        try:
            ip = ipaddress.ip_interface(a).ip
        except ValueError:
            continue
        if any(ip in n for n in nets):
            return True
    return False


def usable(addresses):
    return [a for a in addresses if not a.startswith("169.254.")]


def detect_and_apply(adapter, office, log):
    """Jalankan deteksi lengkap. Return 'kantor' / 'rumah' / 'gagal'."""
    office_gw = office.get("gateway", "").strip()

    # Langkah 0: kalau static kantor sudah terpasang & gateway kantor menjawab -> selesai
    st = current_state(adapter)
    if st["dhcp"] is False and office_gw and in_office_net(st["addresses"], office):
        if ping(office_gw):
            log("[AutoLAN] Static kantor sudah aktif & gateway kantor menjawab -> KANTOR")
            return "kantor"
    # ...atau DHCP sudah aktif dengan IP normal di luar subnet kantor -> rumah
    if st["dhcp"] and usable(st["addresses"]) and not in_office_net(st["addresses"], office):
        log(f"[AutoLAN] DHCP sudah aktif ({', '.join(usable(st['addresses']))}) -> RUMAH")
        return "rumah"

    # Langkah 1: coba DHCP
    apply_dhcp(adapter, log)
    got = []
    for _ in range(15):
        time.sleep(1)
        st = current_state(adapter)
        got = usable(st["addresses"])
        if got:
            break

    if got and not in_office_net(got, office):
        log(f"[AutoLAN] DHCP dapat {', '.join(got)} gw {st['gateway']} -> RUMAH (DHCP)")
        return "rumah"

    # Langkah 2: tidak dapat DHCP / ternyata subnet kantor -> static
    reason = "tidak ada DHCP" if not got else "DHCP ada tapi subnet kantor"
    log(f"[AutoLAN] {reason} -> coba profil KANTOR")
    if not apply_static(adapter, office, log):
        return "gagal"
    time.sleep(3)
    if office_gw and ping(office_gw, 3):
        log("[AutoLAN] Gateway kantor menjawab -> KANTOR (static) ✔")
        return "kantor"
    log("[AutoLAN] Static terpasang, tapi gateway kantor belum menjawab.")
    return "kantor"


# --------------------------------------------------------------------------- #
#  Monitor kabel
# --------------------------------------------------------------------------- #
class AutoLanMonitor:
    def __init__(self, get_cfg, log, on_done=None):
        self.get_cfg = get_cfg
        self.log = log
        self.on_done = on_done
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self.thread = None
        self.last_result = "-"

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def run_now(self, mode="auto"):
        threading.Thread(target=self._apply, args=(mode,), daemon=True).start()

    def _apply(self, mode):
        if not self._busy.acquire(blocking=False):
            self.log("[AutoLAN] Proses sebelumnya masih berjalan…")
            return
        try:
            cfg = self.get_cfg()
            ad = cfg["adapter"]
            if mode == "dhcp":
                apply_dhcp(ad, self.log)
                res = "rumah"
            elif mode == "static":
                res = "kantor" if apply_static(ad, cfg["office"], self.log) else "gagal"
            else:
                res = detect_and_apply(ad, cfg["office"], self.log)
            self.last_result = res
            if self.on_done:
                self.on_done(res)
        except Exception as e:
            self.log(f"[AutoLAN] Error: {e}")
        finally:
            self._busy.release()

    def _loop(self):
        prev = None
        while not self._stop.is_set():
            try:
                up = link_up(self.get_cfg()["adapter"])
            except Exception:
                up = False
            if up and prev is not True:
                self.log("[AutoLAN] Kabel LAN tersambung -> deteksi jaringan…")
                self._apply("auto")
            elif not up and prev is True:
                self.log("[AutoLAN] Kabel LAN dilepas.")
            prev = up
            self._stop.wait(2)


# --------------------------------------------------------------------------- #
#  Autostart saat login (Task Scheduler, hak tertinggi -> tanpa prompt UAC)
# --------------------------------------------------------------------------- #
TASK_NAME = "DualNet"


def launch_command():
    import sys
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --min'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(sys.argv[0])}" --min'


def autostart_enabled():
    code, _ = run(["schtasks", "/query", "/tn", TASK_NAME])
    return code == 0


def set_autostart(enable, log):
    if enable:
        code, out = run(["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "onlogon",
                         "/rl", "highest", "/tr", launch_command()])
    else:
        code, out = run(["schtasks", "/delete", "/f", "/tn", TASK_NAME])
    log(f"[Startup] {'aktif' if enable else 'nonaktif'}" if code == 0 else f"[Startup] gagal: {out}")
    return code == 0
