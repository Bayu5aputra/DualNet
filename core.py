"""
DualNet core - deteksi adapter, load balancer, proxy SOCKS5/HTTP, proxy sistem Windows.
Copyright (c) 2026 Bayu Saputra. MIT License.
"""

import asyncio
import ctypes
import ipaddress
import json
import socket
import struct
import subprocess
import threading
import time

try:
    import winreg
except ImportError:
    winreg = None

DEFAULT_PORT = 8899
CONNECT_TIMEOUT = 8
BUF = 64 * 1024
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
#  Deteksi adapter jaringan
# --------------------------------------------------------------------------- #
def detect_interfaces():
    """Kembalikan daftar adapter yang punya IPv4 + default gateway."""
    ps = (
        "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null } | "
        "ForEach-Object { [PSCustomObject]@{ "
        "Alias=$_.InterfaceAlias; Desc=$_.InterfaceDescription; "
        "Gw=@($_.IPv4DefaultGateway)[0].NextHop; "
        "Addrs=@($_.IPv4Address | ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }) } } | "
        "ConvertTo-Json -Depth 3"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=NOWIN,
        ).stdout.strip()
    except Exception:
        return []
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]

    result = []
    for d in data:
        gw = d.get("Gw")
        addrs = d.get("Addrs") or []
        if isinstance(addrs, str):
            addrs = [addrs]
        chosen = None
        for a in addrs:
            try:
                iface = ipaddress.ip_interface(a)
                if gw and ipaddress.ip_address(gw) in iface.network:
                    chosen = str(iface.ip)
                    break
            except ValueError:
                pass
        if not chosen and addrs:
            chosen = addrs[0].split("/")[0]
        if chosen and not chosen.startswith("169.254."):
            result.append({"alias": d.get("Alias"), "desc": d.get("Desc"),
                           "ip": chosen, "gw": gw})
    return result


def detect_blocking_vpn():
    """Nama adapter VPN full-tunnel yang sedang Up (WARP, dll), atau None."""
    ps = ("Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and "
          "($_.Name -match 'WARP|WireGuard|OpenVPN|NordLynx|ProtonVPN' -or "
          "$_.InterfaceDescription -match 'WARP|WireGuard|TAP-Windows|Wintun') } | "
          "Select-Object -ExpandProperty Name")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=NOWIN).stdout
    except Exception:
        return None
    names = [l.strip() for l in out.splitlines() if l.strip()]
    return ", ".join(names) or None


# --------------------------------------------------------------------------- #
#  Jalur (uplink) + statistik
# --------------------------------------------------------------------------- #
class Uplink:
    def __init__(self, alias, ip, gw, desc=""):
        self.alias, self.ip, self.gw, self.desc = alias, ip, gw, desc
        self.enabled = True
        self.weight = 1
        self.active = 0
        self.total = 0
        self.fails = 0
        self.up = 0
        self.down = 0
        self._last = (time.time(), 0, 0)
        self.speed_up = 0.0
        self.speed_down = 0.0
        self.healthy = True
        self.cooldown_until = 0.0

    def tick(self):
        now = time.time()
        t, u, d = self._last
        dt = max(now - t, 1e-6)
        self.speed_up = (self.up - u) / dt
        self.speed_down = (self.down - d) / dt
        self._last = (now, self.up, self.down)


class Balancer:
    MODES = ("Round-robin (berbobot)", "Koneksi aktif paling sedikit")

    def __init__(self):
        self.uplinks: list[Uplink] = []
        self.mode = self.MODES[0]
        self._rr = 0
        self._lock = threading.Lock()

    def candidates(self):
        now = time.time()
        ups = [u for u in self.uplinks if u.enabled]
        ok = [u for u in ups if u.cooldown_until <= now]
        return ok or ups

    def order(self):
        """Urutan jalur yang dicoba untuk koneksi baru (pertama = pilihan utama)."""
        with self._lock:
            ups = self.candidates()
            if not ups:
                return []
            if self.mode == self.MODES[1]:
                ups = sorted(ups, key=lambda u: u.active / max(u.weight, 1))
                return ups
            # weighted round-robin
            slots = [u for u in ups for _ in range(max(u.weight, 1))]
            first = slots[self._rr % len(slots)]
            self._rr += 1
            return [first] + [u for u in ups if u is not first]


# --------------------------------------------------------------------------- #
#  Proxy server (SOCKS5 + HTTP/HTTPS) – asyncio
# --------------------------------------------------------------------------- #
class ProxyServer:
    def __init__(self, balancer: Balancer, port: int, log):
        self.bal = balancer
        self.port = port
        self.log = log
        self.loop = None
        self.server = None
        self.thread = None

    # ---------- lifecycle ----------
    def start(self):
        ready = threading.Event()
        err = []

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.server = self.loop.run_until_complete(
                    asyncio.start_server(self.handle, "127.0.0.1", self.port))
            except Exception as e:
                err.append(e)
                ready.set()
                return
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        if err:
            raise err[0]

    def stop(self):
        if not self.loop:
            return

        async def shutdown():
            self.server.close()
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()

        fut = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            fut.result(3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop = None

    # ---------- outbound via uplink ----------
    async def open_upstream(self, host, port):
        last_err = None
        for up in self.bal.order():
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port, family=socket.AF_INET,
                                            local_addr=(up.ip, 0)),
                    CONNECT_TIMEOUT)
                up.healthy = True
                return up, r, w
            except Exception as e:
                last_err = e
                up.fails += 1
                up.healthy = False
                up.cooldown_until = time.time() + 5
                self.log(f"[{up.alias}] gagal ke {host}:{port} ({e.__class__.__name__}), coba jalur lain")
        raise ConnectionError(f"Semua jalur gagal: {last_err}")

    async def pipe(self, reader, writer, up: Uplink, direction):
        try:
            while True:
                data = await reader.read(BUF)
                if not data:
                    break
                if direction == "up":
                    up.up += len(data)
                else:
                    up.down += len(data)
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def relay(self, cr, cw, up, ur, uw, initial=b""):
        up.active += 1
        up.total += 1
        try:
            if initial:
                uw.write(initial)
                up.up += len(initial)
                await uw.drain()
            await asyncio.gather(self.pipe(cr, uw, up, "up"),
                                 self.pipe(ur, cw, up, "down"))
        finally:
            up.active -= 1

    # ---------- client handler ----------
    async def handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
        try:
            first = await asyncio.wait_for(cr.readexactly(1), 30)
            if first == b"\x05":
                await self.handle_socks5(cr, cw)
            else:
                await self.handle_http(first, cr, cw)
        except Exception:
            pass
        finally:
            try:
                cw.close()
            except Exception:
                pass

    async def handle_socks5(self, cr, cw):
        nmethods = (await cr.readexactly(1))[0]
        await cr.readexactly(nmethods)
        cw.write(b"\x05\x00")
        await cw.drain()
        ver, cmd, _, atyp = await cr.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntoa(await cr.readexactly(4))
        elif atyp == 3:
            ln = (await cr.readexactly(1))[0]
            host = (await cr.readexactly(ln)).decode()
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await cr.readexactly(16))
        else:
            return
        port = struct.unpack("!H", await cr.readexactly(2))[0]
        if cmd != 1:  # hanya CONNECT
            cw.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await cw.drain()
            return
        try:
            up, ur, uw = await self.open_upstream(host, port)
        except Exception:
            cw.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
            await cw.drain()
            return
        cw.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await cw.drain()
        await self.relay(cr, cw, up, ur, uw)

    async def handle_http(self, first, cr, cw):
        head = first + await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)

        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            host = host.strip("[]")
            try:
                up, ur, uw = await self.open_upstream(host, int(port or 443))
            except Exception:
                cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await cw.drain()
                return
            cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await cw.drain()
            await self.relay(cr, cw, up, ur, uw)
            return

        # HTTP biasa: "GET http://host[:port]/path HTTP/1.1"
        if not target.lower().startswith("http://"):
            cw.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await cw.drain()
            return
        rest = target[7:]
        hostport, slash, path = rest.partition("/")
        path = "/" + path
        if hostport.startswith("["):
            h, _, p = hostport[1:].partition("]")
            host, port = h, int(p.lstrip(":") or 80)
        elif ":" in hostport:
            host, p = hostport.rsplit(":", 1)
            port = int(p)
        else:
            host, port = hostport, 80

        headers = [l for l in lines[1:] if l and not l.lower().startswith(
            ("proxy-connection:", "connection:", "keep-alive:"))]
        new_head = f"{method} {path} {version}\r\n" + "\r\n".join(headers) + \
                   "\r\nConnection: close\r\n\r\n"
        try:
            up, ur, uw = await self.open_upstream(host, port)
        except Exception:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await cw.drain()
            return
        await self.relay(cr, cw, up, ur, uw, new_head.encode("latin-1"))


# --------------------------------------------------------------------------- #
#  Proxy sistem Windows (HKCU\...\Internet Settings)
# --------------------------------------------------------------------------- #
INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _refresh_inet():
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


def read_system_proxy():
    if not winreg:
        return None
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY) as k:
        def get(name, default):
            try:
                return winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                return default
        return {"ProxyEnable": get("ProxyEnable", 0),
                "ProxyServer": get("ProxyServer", None),
                "ProxyOverride": get("ProxyOverride", None)}


def write_system_proxy(enable, server=None, override=None):
    if not winreg:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if server is None:
            try:
                winreg.DeleteValue(k, "ProxyServer")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        if override is None:
            try:
                winreg.DeleteValue(k, "ProxyOverride")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, override)
    _refresh_inet()
