"""
DualNet - Gabungkan internet Wi-Fi + LAN (Ethernet) sekaligus di Windows,
plus LAN Otomatis: IP static di kantor, DHCP di rumah, tinggal colok.

Copyright (c) 2026 Bayu Saputra. MIT License.
"""

import ctypes
import ipaddress
import os
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox

import autolan
import core
import winnet

APP_NAME = "DualNet"
VERSION = "1.0.0"
AUTHOR = "Bayu Saputra"

# --------------------------------------------------------------------------- #
#  Tema
# --------------------------------------------------------------------------- #
C = {
    "bg": "#0B0E13", "panel": "#12161D", "panel2": "#181D26", "line": "#232A35",
    "text": "#E8ECF1", "muted": "#7D8796", "dim": "#4A5361",
    "accent": "#C6F432", "accent_ink": "#0B0E13",
    "wifi": "#46C8FF", "lan": "#FFB23F", "other": "#B78CFF",
    "bad": "#FF5D5D", "ok": "#42E39A",
}
F_UI = "Chakra Petch"
F_UI_MED = "Chakra Petch Medium"
F_UI_BOLD = "Chakra Petch SemiBold"
F_MONO = "IBM Plex Mono"
F_MONO_MED = "IBM Plex Mono Medium"


def resource(*p):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *p)


def load_fonts():
    """Muat font bundled secara privat (tidak diinstal ke sistem)."""
    global F_UI, F_UI_MED, F_UI_BOLD, F_MONO, F_MONO_MED
    folder = resource("fonts")
    ok = 0
    try:
        for f in os.listdir(folder):
            if f.lower().endswith(".ttf"):
                ok += ctypes.windll.gdi32.AddFontResourceExW(os.path.join(folder, f), 0x10, 0) > 0
    except Exception:
        pass
    if not ok:  # fallback font bawaan Windows
        F_UI = F_UI_MED = F_UI_BOLD = "Segoe UI"
        F_MONO = F_MONO_MED = "Consolas"


def human(n, per_s=False):
    suf = "/s" if per_s else ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return (f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}") + suf
        n /= 1024


def color_for(alias):
    a = alias.lower()
    if "wi" in a or "wlan" in a or "wireless" in a:
        return C["wifi"]
    if "eth" in a or "lan" in a:
        return C["lan"]
    return C["other"]


# --------------------------------------------------------------------------- #
#  Widget kustom
# --------------------------------------------------------------------------- #
class Btn(tk.Label):
    def __init__(self, master, text, command, kind="ghost", font=None, padx=16, pady=8, **kw):
        self.kind = kind
        self.command = command
        super().__init__(master, text=text, cursor="hand2", padx=padx, pady=pady,
                         font=font or (F_UI_BOLD, 10), **kw)
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self._paint(False)

    def set_kind(self, kind):
        self.kind = kind
        self._paint(False)

    def _paint(self, hover):
        if self.kind == "primary":
            bg = "#D7FF5C" if hover else C["accent"]
            fg = C["accent_ink"]
        elif self.kind == "danger":
            bg = "#FF7A7A" if hover else C["bad"]
            fg = C["accent_ink"]
        else:
            bg = C["line"] if hover else C["panel2"]
            fg = C["text"]
        self.configure(bg=bg, fg=fg)


class Toggle(tk.Canvas):
    W, H = 42, 22

    def __init__(self, master, value=False, command=None, bg=None):
        super().__init__(master, width=self.W, height=self.H, bg=bg or C["panel"],
                         highlightthickness=0, cursor="hand2")
        self.value = value
        self.command = command
        self.bind("<Button-1>", self._click)
        self.draw()

    def _click(self, _):
        self.set(not self.value)
        if self.command:
            self.command(self.value)

    def set(self, v):
        self.value = bool(v)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2
        fill = C["accent"] if self.value else C["line"]
        self.create_oval(0, 0, h, h, fill=fill, outline=fill)
        self.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
        self.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        x = w - h + 3 if self.value else 3
        knob = C["accent_ink"] if self.value else C["muted"]
        self.create_oval(x, 3, x + h - 6, h - 3, fill=knob, outline=knob)


class Spark(tk.Canvas):
    """Grafik garis mini; menggambar ulang hanya saat ada data baru."""

    def __init__(self, master, color, height=56, points=60, bg=None):
        super().__init__(master, height=height, bg=bg or C["panel"], highlightthickness=0)
        self.color = color
        self.data = deque([0.0] * points, maxlen=points)
        self.bind("<Configure>", lambda e: self.draw())

    def push(self, v):
        self.data.append(v)
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        for gy in (0.33, 0.66):
            self.create_line(0, h * gy, w, h * gy, fill=C["line"], dash=(2, 4))
        mx = max(max(self.data), 1.0)
        n = len(self.data)
        pts = []
        for i, v in enumerate(self.data):
            pts += [i * w / (n - 1), h - 3 - (v / mx) * (h - 8)]
        self.create_polygon(0, h, *pts, w, h, fill=self._shade(), outline="")
        self.create_line(*pts, fill=self.color, width=2, smooth=True)

    def _shade(self):
        r, g, b = (int(self.color[i:i + 2], 16) for i in (1, 3, 5))
        br, bgc, bb = (int(C["panel"][i:i + 2], 16) for i in (1, 3, 5))
        a = 0.18
        return "#%02x%02x%02x" % (int(br + (r - br) * a), int(bgc + (g - bgc) * a), int(bb + (b - bb) * a))


def label(master, text="", size=10, color="text", font=None, bg="panel", **kw):
    return tk.Label(master, text=text, fg=C[color], bg=C[bg], font=(font or F_UI, size), **kw)


def entry(master, var, width=20, mono=False):
    e = tk.Entry(master, textvariable=var, width=width, bg=C["panel2"], fg=C["text"],
                 insertbackground=C["accent"], relief="flat", highlightthickness=1,
                 highlightbackground=C["line"], highlightcolor=C["accent"],
                 font=((F_MONO, 10) if mono else (F_UI, 10)))
    return e


# --------------------------------------------------------------------------- #
#  Kartu jalur internet
# --------------------------------------------------------------------------- #
class UplinkCard(tk.Frame):
    def __init__(self, master, app, up: core.Uplink):
        super().__init__(master, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        self.app, self.up = app, up
        col = color_for(up.alias)
        tk.Frame(self, bg=col, height=3).pack(fill="x")
        body = tk.Frame(self, bg=C["panel"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg=C["panel"])
        top.pack(fill="x")
        label(top, up.alias.upper(), 13, font=F_UI_BOLD).pack(side="left")
        self.state_lbl = label(top, "", 9, "ok", font=F_MONO_MED)
        self.state_lbl.pack(side="left", padx=10)
        self.tg = Toggle(top, up.enabled, self.on_toggle)
        self.tg.pack(side="right")

        label(body, f"{up.ip}  →  gw {up.gw}", 9, "muted", font=F_MONO).pack(anchor="w", pady=(2, 0))
        self.link_lbl = label(body, "", 9, "dim", font=F_MONO)
        self.link_lbl.pack(anchor="w")

        self.spark = Spark(body, col, height=46)
        self.spark.pack(fill="x", pady=10)

        stats = tk.Frame(body, bg=C["panel"])
        stats.pack(fill="x")
        self.down_lbl = label(stats, "↓ 0 B/s", 15, font=F_MONO_MED)
        self.down_lbl.pack(side="left")
        self.up_lbl = label(stats, "↑ 0 B/s", 10, "muted", font=F_MONO)
        self.up_lbl.pack(side="left", padx=(12, 0), pady=(4, 0))

        foot = tk.Frame(body, bg=C["panel"])
        foot.pack(fill="x", pady=(10, 0))
        self.conn_lbl = label(foot, "0 koneksi", 9, "muted", font=F_MONO)
        self.conn_lbl.pack(side="left")
        wf = tk.Frame(foot, bg=C["panel"])
        wf.pack(side="right")
        label(wf, "BOBOT", 8, "dim", font=F_UI_BOLD).pack(side="left", padx=(0, 6))
        Btn(wf, "–", lambda: self.weight(-1), padx=8, pady=0, font=(F_MONO_MED, 11)).pack(side="left")
        self.w_lbl = label(wf, str(up.weight), 11, font=F_MONO_MED, width=2)
        self.w_lbl.pack(side="left")
        Btn(wf, "+", lambda: self.weight(1), padx=8, pady=0, font=(F_MONO_MED, 11)).pack(side="left")

    def on_toggle(self, v):
        self.up.enabled = v
        self.app.log(f"{self.up.alias} {'diaktifkan' if v else 'dimatikan'}")
        self.app.persist_uplinks()

    def weight(self, d):
        self.up.weight = min(9, max(1, self.up.weight + d))
        self.w_lbl.config(text=str(self.up.weight))
        self.app.persist_uplinks()

    def refresh(self):
        u = self.up
        self.spark.push(u.speed_down)
        self.down_lbl.config(text="↓ " + human(u.speed_down, True))
        self.up_lbl.config(text="↑ " + human(u.speed_up, True))
        self.conn_lbl.config(text=f"{u.active} aktif · {u.total} total · {human(u.down)}")
        if not u.enabled:
            self.state_lbl.config(text="OFF", fg=C["dim"])
        elif u.cooldown_until > time.time():
            self.state_lbl.config(text="GAGAL", fg=C["bad"])
        else:
            self.state_lbl.config(text="SIAP", fg=C["ok"])

    def refresh_link(self):
        sp = winnet.link_speed(self.up.alias)
        self.link_lbl.config(text=f"link {sp // 1_000_000} Mbps" if sp else "link down")


# --------------------------------------------------------------------------- #
#  Aplikasi
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, start_min=False):
        self.root = root
        self.cfg = autolan.load_config()
        self.bal = core.Balancer()
        self.bal.mode = core.Balancer.MODES[self.cfg.get("mode", 0)]
        self.proxy = None
        self.cards = []
        self.page = None
        self.link_sig = None
        self.pending_logs = []

        root.title(f"{APP_NAME} — Wi-Fi + LAN")
        root.configure(bg=C["bg"])
        root.geometry("1000x760")
        root.minsize(900, 720)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.iconbitmap(resource("assets", "dualnet.ico"))
        except Exception:
            pass
        self._ttk_style()

        self.build_header()
        self.build_nav()
        self.pages = tk.Frame(root, bg=C["bg"])
        self.pages.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.page_frames = {
            "gabung": self.build_page_gabung(),
            "lan": self.build_page_lan(),
            "log": self.build_page_log(),
        }
        self.build_footer()
        self.show("gabung")

        self.monitor = autolan.AutoLanMonitor(self.collect_lan_cfg, self.log, self.on_lan_done)
        self.cleanup_stale_proxy()
        self.refresh_ifaces(then_autostart=True)
        if self.cfg["autolan_enabled"]:
            self.monitor.start()
        if start_min:
            root.iconify()
        root.after(1000, self.tick)

    # ---------- style ----------
    def _ttk_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("D.TCombobox", fieldbackground=C["panel2"], background=C["panel2"],
                    foreground=C["text"], arrowcolor=C["accent"], bordercolor=C["line"],
                    lightcolor=C["panel2"], darkcolor=C["panel2"], selectbackground=C["panel2"],
                    selectforeground=C["text"], padding=4)
        s.map("D.TCombobox", fieldbackground=[("readonly", C["panel2"])],
              foreground=[("readonly", C["text"])])
        self.root.option_add("*TCombobox*Listbox.background", C["panel2"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", C["accent_ink"])
        self.root.option_add("*TCombobox*Listbox.font", (F_UI, 10))

    # ---------- header / nav ----------
    def build_header(self):
        h = tk.Frame(self.root, bg=C["bg"], padx=24, pady=14)
        h.pack(fill="x")
        brand = tk.Frame(h, bg=C["bg"])
        brand.pack(side="left")
        wm = tk.Frame(brand, bg=C["bg"])
        wm.pack(anchor="w")
        label(wm, "DUAL", 24, font=F_UI_BOLD, bg="bg", bd=0, padx=0).pack(side="left")
        label(wm, "NET", 24, "accent", font=F_UI_BOLD, bg="bg", bd=0, padx=0).pack(side="left")
        label(wm, f" v{VERSION}", 9, "dim", font=F_MONO, bg="bg").pack(side="left", anchor="s", pady=(0, 8))
        label(brand, "Wi-Fi + LAN aktif bersamaan · satu internet gabungan", 10, "muted",
              bg="bg").pack(anchor="w")

        right = tk.Frame(h, bg=C["bg"])
        right.pack(side="right")
        self.pill = label(right, "● BERHENTI", 9, "muted", font=F_MONO_MED, bg="bg")
        self.pill.pack(side="left", padx=(0, 16))
        self.start_btn = Btn(right, "MULAI GABUNG", self.toggle_proxy, "primary",
                             font=(F_UI_BOLD, 11), padx=22, pady=10)
        self.start_btn.pack(side="left")

    def build_nav(self):
        n = tk.Frame(self.root, bg=C["bg"], padx=24)
        n.pack(fill="x", pady=(0, 14))
        self.nav = {}
        for key, txt in (("gabung", "GABUNG INTERNET"), ("lan", "LAN OTOMATIS"), ("log", "LOG")):
            f = tk.Frame(n, bg=C["bg"], cursor="hand2")
            f.pack(side="left", padx=(0, 26))
            l = label(f, txt, 10, "muted", font=F_UI_BOLD, bg="bg", cursor="hand2")
            l.pack()
            bar = tk.Frame(f, height=2, bg=C["bg"])
            bar.pack(fill="x", pady=(5, 0))
            for w in (f, l):
                w.bind("<Button-1>", lambda e, k=key: self.show(k))
            self.nav[key] = (l, bar)
        tk.Frame(self.root, bg=C["line"], height=1).pack(fill="x", padx=24, pady=(0, 16))

    def show(self, key):
        for k, fr in self.page_frames.items():
            fr.pack_forget()
            l, bar = self.nav[k]
            l.config(fg=C["text"] if k == key else C["muted"])
            bar.config(bg=C["accent"] if k == key else C["bg"])
        self.page_frames[key].pack(fill="both", expand=True)
        self.page = key

    def build_footer(self):
        f = tk.Frame(self.root, bg=C["bg"], padx=24, pady=8)
        f.pack(fill="x", side="bottom")
        label(f, f"© 2026 {AUTHOR}", 8, "dim", font=F_MONO, bg="bg").pack(side="left")
        self.foot_lbl = label(f, "", 8, "dim", font=F_MONO, bg="bg")
        self.foot_lbl.pack(side="right")

    # ---------- halaman: gabung ----------
    def build_page_gabung(self):
        pg = tk.Frame(self.pages, bg=C["bg"])

        hero = tk.Frame(pg, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"],
                        padx=20, pady=16)
        hero.pack(fill="x")
        left = tk.Frame(hero, bg=C["panel"])
        left.pack(side="left", fill="y")
        label(left, "TOTAL GABUNGAN", 9, "muted", font=F_UI_BOLD).pack(anchor="w")
        self.tot_down = label(left, "↓ 0 B/s", 30, font=F_MONO_MED)
        self.tot_down.pack(anchor="w")
        self.tot_up = label(left, "↑ 0 B/s", 11, "muted", font=F_MONO)
        self.tot_up.pack(anchor="w")
        self.tot_sess = label(left, "sesi: 0 B", 9, "dim", font=F_MONO)
        self.tot_sess.pack(anchor="w", pady=(6, 0))
        self.tot_spark = Spark(hero, C["accent"], height=90, points=90)
        self.tot_spark.pack(side="left", fill="both", expand=True, padx=(28, 0))

        self.cards_box = tk.Frame(pg, bg=C["bg"])
        self.cards_box.pack(fill="both", expand=True, pady=14)
        self.empty_lbl = label(self.cards_box, "Mendeteksi adapter…", 11, "muted", bg="bg")

        bar = tk.Frame(pg, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"],
                       padx=16, pady=10)
        bar.pack(fill="x")
        label(bar, "PORT", 8, "dim", font=F_UI_BOLD).pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", core.DEFAULT_PORT)))
        entry(bar, self.port_var, 6, mono=True).pack(side="left", padx=(6, 20), ipady=3)

        label(bar, "MODE", 8, "dim", font=F_UI_BOLD).pack(side="left")
        self.mode_btns = []
        for i, t in enumerate(("ROUND-ROBIN", "PALING SEPI")):
            b = Btn(bar, t, lambda i=i: self.set_mode(i), padx=10, pady=4, font=(F_UI_BOLD, 9))
            b.pack(side="left", padx=(6 if i == 0 else 1, 0))
            self.mode_btns.append(b)
        self.set_mode(self.cfg.get("mode", 0), save=False)

        for txt, key in (("AUTO MULAI", "auto_start_proxy"), ("PROXY SISTEM", "system_proxy")):
            tg = Toggle(bar, self.cfg.get(key, False),
                        lambda v, k=key: self.set_cfg(k, v))
            tg.pack(side="right")
            label(bar, txt, 8, "dim", font=F_UI_BOLD).pack(side="right", padx=(18, 6))
        Btn(bar, "DETEKSI ULANG", self.refresh_ifaces, padx=10, pady=4,
            font=(F_UI_BOLD, 9)).pack(side="right", padx=(0, 4))
        return pg

    def set_mode(self, i, save=True):
        self.bal.mode = core.Balancer.MODES[i]
        for j, b in enumerate(self.mode_btns):
            b.set_kind("primary" if j == i else "ghost")
        if save:
            self.set_cfg("mode", i)

    def set_cfg(self, k, v):
        self.cfg[k] = v
        autolan.save_config(self.cfg)

    def rebuild_cards(self):
        for c in self.cards:
            c.destroy()
        self.cards = []
        self.empty_lbl.place_forget()
        if not self.bal.uplinks:
            self.empty_lbl.config(text="Tidak ada adapter dengan internet. Sambungkan Wi-Fi dan LAN.")
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor="center")
            return
        n = len(self.bal.uplinks)
        for i, u in enumerate(self.bal.uplinks):
            card = UplinkCard(self.cards_box, self, u)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 0 if i == n - 1 else 7))
            self.cards_box.columnconfigure(i, weight=1, uniform="c")
            card.refresh_link()
            self.cards.append(card)
        for i in range(n, 6):
            self.cards_box.columnconfigure(i, weight=0, uniform="")
        self.cards_box.rowconfigure(0, weight=1)

    def persist_uplinks(self):
        self.cfg["weights"] = {u.alias: u.weight for u in self.bal.uplinks}
        self.cfg["disabled"] = [u.alias for u in self.bal.uplinks if not u.enabled]
        autolan.save_config(self.cfg)

    # ---------- halaman: LAN otomatis ----------
    def build_page_lan(self):
        pg = tk.Frame(self.pages, bg=C["bg"])
        top = tk.Frame(pg, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"],
                       padx=20, pady=16)
        top.pack(fill="x")
        l = tk.Frame(top, bg=C["panel"])
        l.pack(side="left")
        label(l, "LOKASI SAAT INI", 9, "muted", font=F_UI_BOLD).pack(anchor="w")
        self.loc_lbl = label(l, "—", 26, font=F_UI_BOLD)
        self.loc_lbl.pack(anchor="w")
        self.loc_sub = label(l, "colok kabel LAN, DualNet memilih Static / DHCP otomatis", 9, "dim",
                             font=F_MONO)
        self.loc_sub.pack(anchor="w")
        r = tk.Frame(top, bg=C["panel"])
        r.pack(side="right")
        label(r, "OTOMATIS", 8, "dim", font=F_UI_BOLD).pack(side="left", padx=(0, 6))
        self.lan_tg = Toggle(r, self.cfg["autolan_enabled"], self.toggle_autolan)
        self.lan_tg.pack(side="left")
        label(r, "STARTUP WINDOWS", 8, "dim", font=F_UI_BOLD).pack(side="left", padx=(20, 6))
        self.boot_tg = Toggle(r, False, lambda v: threading.Thread(
            target=autolan.set_autostart, args=(v, self.log), daemon=True).start())
        self.boot_tg.pack(side="left")
        threading.Thread(target=lambda: self.ui(self.boot_tg.set, autolan.autostart_enabled()),
                         daemon=True).start()

        form = tk.Frame(pg, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"],
                        padx=20, pady=16)
        form.pack(fill="x", pady=14)
        label(form, "PROFIL KANTOR  ·  IP STATIC", 10, font=F_UI_BOLD).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        label(form, "ADAPTER", 8, "dim", font=F_UI_BOLD).grid(row=1, column=0, sticky="w", pady=5)
        self.ad_var = tk.StringVar(value=self.cfg["adapter"])
        self.ad_combo = ttk.Combobox(form, textvariable=self.ad_var, style="D.TCombobox",
                                     values=[self.cfg["adapter"]], width=28, font=(F_UI, 10))
        self.ad_combo.grid(row=1, column=1, sticky="w", padx=12)
        threading.Thread(target=lambda: self.ui(
            self.ad_combo.config, {"values": autolan.list_ethernet_adapters()}), daemon=True).start()

        self.off_vars = {}
        rows = (("addresses", "IP / PREFIX", "192.168.10.50/24, 172.20.5.10/23"),
                ("gateway", "GATEWAY", "192.168.10.1"),
                ("dns", "DNS", "8.8.8.8, 1.1.1.1"))
        for i, (k, t, ex) in enumerate(rows, start=2):
            label(form, t, 8, "dim", font=F_UI_BOLD).grid(row=i, column=0, sticky="w", pady=5)
            v = tk.StringVar(value=self.cfg["office"].get(k, ""))
            entry(form, v, 44, mono=True).grid(row=i, column=1, sticky="we", padx=12, ipady=4)
            label(form, "mis. " + ex, 8, "dim", font=F_MONO).grid(row=i, column=2, sticky="w")
            self.off_vars[k] = v
        form.columnconfigure(1, weight=1)

        btns = tk.Frame(form, bg=C["panel"])
        btns.grid(row=5, column=1, columnspan=2, sticky="w", padx=12, pady=(12, 0))
        Btn(btns, "AMBIL DARI LAN SEKARANG", self.capture_office, "primary",
            font=(F_UI_BOLD, 9)).pack(side="left")
        Btn(btns, "SIMPAN", self.save_lan, font=(F_UI_BOLD, 9)).pack(side="left", padx=6)

        act = tk.Frame(pg, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"],
                       padx=20, pady=14)
        act.pack(fill="x")
        label(act, "TERAPKAN MANUAL", 8, "dim", font=F_UI_BOLD).pack(side="left", padx=(0, 12))
        Btn(act, "DETEKSI & TERAPKAN", lambda: self.lan_now("auto"), font=(F_UI_BOLD, 9)).pack(side="left")
        Btn(act, "PAKSA KANTOR", lambda: self.lan_now("static"), font=(F_UI_BOLD, 9)).pack(side="left", padx=6)
        Btn(act, "PAKSA RUMAH (DHCP)", lambda: self.lan_now("dhcp"), font=(F_UI_BOLD, 9)).pack(side="left")

        note = ("Cara kerja: saat kabel dicolok, DualNet mencoba DHCP. Dapat IP di luar subnet kantor "
                "→ RUMAH. Tidak dapat IP / subnet kantor → pasang IP static kantor.")
        if not autolan.is_admin():
            note += "  ⚠ Tidak berjalan sebagai Administrator: ubah IP akan gagal."
        label(pg, note, 9, "muted", bg="bg", wraplength=900, justify="left").pack(anchor="w", pady=12)
        return pg

    def collect_lan_cfg(self):
        self.cfg["adapter"] = self.ad_var.get().strip() or "Ethernet"
        self.cfg["office"] = {k: v.get().strip() for k, v in self.off_vars.items()}
        self.cfg["autolan_enabled"] = self.lan_tg.value
        return self.cfg

    def save_lan(self, quiet=False):
        autolan.save_config(self.collect_lan_cfg())
        if not quiet:
            self.log("[AutoLAN] Pengaturan disimpan.")

    def capture_office(self):
        st = autolan.current_state(self.ad_var.get().strip())
        if st["dhcp"]:
            messagebox.showinfo(APP_NAME, "LAN sekarang memakai DHCP.\nGunakan tombol ini saat di kantor "
                                          "(IP static sedang aktif).")
            return
        addrs = autolan.usable(st["addresses"])
        if not addrs:
            messagebox.showwarning(APP_NAME, "Tidak ada IP pada adapter ini.")
            return
        gw = st["gateway"]
        if gw:
            addrs.sort(key=lambda a: ipaddress.ip_address(gw) not in ipaddress.ip_interface(a).network)
        dns = [d for d in st["dns"] if not d.startswith("127.")]
        self.off_vars["addresses"].set(", ".join(addrs))
        self.off_vars["gateway"].set(gw or "")
        if dns:
            self.off_vars["dns"].set(", ".join(dns))
        self.save_lan(quiet=True)
        self.log(f"[AutoLAN] Profil kantor diambil: {', '.join(addrs)} gw {gw}"
                 + ("" if dns else " — DNS asli tidak terbaca, cek kolom DNS"))

    def toggle_autolan(self, v):
        self.save_lan(quiet=True)
        if v:
            if not self.off_vars["addresses"].get().strip():
                self.log("[AutoLAN] Isi profil kantor dulu (klik 'Ambil dari LAN sekarang' saat di kantor).")
            self.monitor.start()
            self.log("[AutoLAN] Otomatis AKTIF — memantau kabel LAN.")
        else:
            self.monitor.stop()
            self.log("[AutoLAN] Otomatis dimatikan.")

    def lan_now(self, mode):
        self.save_lan(quiet=True)
        self.loc_lbl.config(text="MENDETEKSI…", fg=C["muted"])
        self.monitor.run_now(mode)

    def on_lan_done(self, res):
        txt, col, sub = {
            "kantor": ("KANTOR", C["lan"], "IP static kantor terpasang"),
            "rumah": ("RUMAH", C["wifi"], "DHCP — IP otomatis dari router"),
        }.get(res, ("GAGAL", C["bad"], "lihat tab LOG"))
        self.ui(self.loc_lbl.config, {"text": txt, "fg": col})
        self.ui(self.loc_sub.config, {"text": sub})
        self.root.after(2500, self.refresh_ifaces)

    # ---------- halaman: log ----------
    def build_page_log(self):
        pg = tk.Frame(self.pages, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        self.log_txt = tk.Text(pg, bg=C["panel"], fg=C["muted"], relief="flat", padx=14, pady=12,
                               font=(F_MONO, 9), state="disabled", insertbackground=C["accent"],
                               selectbackground=C["line"], wrap="word")
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_configure("t", foreground=C["dim"])
        self.log_txt.tag_configure("w", foreground=C["lan"])
        return pg

    # ---------- util ----------
    def ui(self, fn, *args):
        """Jalankan fungsi di thread GUI."""
        if len(args) == 1 and isinstance(args[0], dict):
            self.root.after(0, lambda: fn(**args[0]))
        else:
            self.root.after(0, fn, *args)

    def log(self, msg):
        self.ui(self._append_log, time.strftime("%H:%M:%S"), msg)

    def _append_log(self, t, msg):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", t + "  ", "t")
        self.log_txt.insert("end", msg + "\n", "w" if ("⚠" in msg or "gagal" in msg.lower()) else "")
        if int(self.log_txt.index("end-1c").split(".")[0]) > 600:
            self.log_txt.delete("1.0", "200.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")
        self.foot_lbl.config(text=msg[:110])

    # ---------- deteksi adapter (di thread agar UI tidak macet) ----------
    def refresh_ifaces(self, then_autostart=False):
        def work():
            found = core.detect_interfaces()
            vpn = core.detect_blocking_vpn()
            self.ui(self._apply_ifaces, found, vpn, then_autostart)
        threading.Thread(target=work, daemon=True).start()

    def _apply_ifaces(self, found, vpn, then_autostart):
        old = {u.ip: u for u in self.bal.uplinks}
        ups = []
        for f in found:
            u = old.get(f["ip"]) or core.Uplink(f["alias"], f["ip"], f["gw"], f["desc"])
            u.weight = self.cfg.get("weights", {}).get(u.alias, u.weight)
            u.enabled = u.alias not in self.cfg.get("disabled", [])
            ups.append(u)
        self.bal.uplinks = ups
        self.link_sig = self._link_signature()
        self.rebuild_cards()
        if ups:
            self.log("Jalur terdeteksi: " + ", ".join(f"{u.alias} ({u.ip})" for u in ups))
            if len(ups) < 2:
                self.log("⚠ Hanya 1 jalur internet. Sambungkan Wi-Fi dan LAN untuk menggabungkan.")
        if vpn:
            self.log(f"⚠ VPN aktif ({vpn}) memblokir koneksi langsung lewat Wi-Fi/LAN. "
                     "Disconnect VPN agar DualNet bekerja.")
        if then_autostart and self.cfg.get("auto_start_proxy") and not self.proxy and ups:
            self.start_proxy()

    def _link_signature(self):
        names = {u.alias for u in self.bal.uplinks} | {"Wi-Fi", self.cfg.get("adapter", "Ethernet")}
        return tuple(sorted((n, winnet.is_up(n)) for n in names))

    # ---------- proxy ----------
    def toggle_proxy(self):
        self.stop_proxy() if self.proxy else self.start_proxy()

    def start_proxy(self):
        if not self.bal.uplinks:
            messagebox.showwarning(APP_NAME, "Tidak ada adapter internet yang terdeteksi.")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Port tidak valid.")
            return
        self.set_cfg("port", port)
        self.proxy = core.ProxyServer(self.bal, port, self.log)
        try:
            self.proxy.start()
        except Exception as e:
            self.proxy = None
            messagebox.showerror(APP_NAME, f"Gagal membuka port {port}:\n{e}")
            return
        msg = f"Proxy aktif 127.0.0.1:{port} (SOCKS5 + HTTP)"
        if self.cfg.get("system_proxy", True):
            try:
                if not self.cfg.get("saved_proxy"):
                    self.cfg["saved_proxy"] = core.read_system_proxy()
                    autolan.save_config(self.cfg)
                core.write_system_proxy(True, f"127.0.0.1:{port}",
                                        "localhost;127.*;10.*;172.16.*;192.168.*;<local>")
                msg += " — proxy sistem Windows terpasang"
            except Exception as e:
                self.log(f"Gagal set proxy sistem: {e}")
        self.log(msg)
        self.pill.config(text=f"● AKTIF :{port}", fg=C["accent"])
        self.start_btn.config(text="BERHENTI")
        self.start_btn.set_kind("danger")

    def restore_proxy(self):
        sp = self.cfg.get("saved_proxy")
        if sp:
            try:
                core.write_system_proxy(bool(sp["ProxyEnable"]), sp["ProxyServer"], sp["ProxyOverride"])
                self.log("Proxy Windows dikembalikan seperti semula.")
            except Exception as e:
                self.log(f"Gagal mengembalikan proxy: {e}")
            self.cfg["saved_proxy"] = None
            autolan.save_config(self.cfg)

    def cleanup_stale_proxy(self):
        """Jika app sebelumnya tertutup paksa, kembalikan proxy Windows."""
        if self.cfg.get("saved_proxy"):
            self.restore_proxy()

    def stop_proxy(self):
        self.restore_proxy()
        if self.proxy:
            self.proxy.stop()
            self.proxy = None
            self.log("Proxy dihentikan.")
        self.pill.config(text="● BERHENTI", fg=C["muted"])
        self.start_btn.config(text="MULAI GABUNG")
        self.start_btn.set_kind("primary")

    # ---------- loop 1 detik ----------
    def tick(self):
        n = getattr(self, "_n", 0) + 1
        self._n = n
        td = tu = 0.0
        for u in self.bal.uplinks:
            u.tick()
            td += u.speed_down
            tu += u.speed_up
        visible = self.root.state() != "iconic"
        if visible and self.page == "gabung":
            self.tot_spark.push(td)
            self.tot_down.config(text="↓ " + human(td, True))
            self.tot_up.config(text="↑ " + human(tu, True))
            dl = sum(u.down for u in self.bal.uplinks)
            ul = sum(u.up for u in self.bal.uplinks)
            self.tot_sess.config(text=f"sesi ini  ↓ {human(dl)}  ↑ {human(ul)}")
            for c in self.cards:
                c.refresh()
                if n % 5 == 0:
                    c.refresh_link()
        if n % 4 == 0:  # cek kabel / Wi-Fi tersambung-terputus (murah, via Windows API)
            sig = self._link_signature()
            if sig != self.link_sig:
                self.link_sig = sig
                self.log("Perubahan koneksi terdeteksi, memindai ulang adapter…")
                self.root.after(3000, self.refresh_ifaces)
        self.root.after(1000, self.tick)

    def on_close(self):
        self.monitor.stop()
        self.stop_proxy()
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BayuSaputra.DualNet")
    except Exception:
        pass
    load_fonts()
    root = tk.Tk()

    def report(exc, val, tb):
        import traceback
        try:
            os.makedirs(autolan.CONFIG_DIR, exist_ok=True)
            with open(os.path.join(autolan.CONFIG_DIR, "error.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n") + "".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass
    root.report_callback_exception = report
    App(root, start_min="--min" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
