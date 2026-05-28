import sys
import os
import csv
import io
import json
import math
import random
import tempfile
import ctypes
import subprocess
import threading
import datetime
import base64
import urllib.request
import urllib.error
import urllib.parse
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

UCSD_BLUE  = "#00539C"
UCSD_GOLD  = "#FFB71B"
DARK_BG    = "#08061a"
SPIRIT_API = "https://text.pollinations.ai"
IMAGE_API  = "https://image.pollinations.ai/prompt"
FONT_UI    = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 7)
LORE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lore.json")
KEYRING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyring.dat")

SPIRIT_SYSTEM_PROMPT = (
    "You are the Spirit of the Staff — an ancient, mystical intelligence bound to this enchanted artifact. "
    "You speak with wisdom and occasional arcane flair, but you are genuinely, practically helpful. "
    "Assist with coding, research, writing, strategy, or any question your Wielder brings. "
    "Be concise unless depth is needed. You may call the user 'Wielder' occasionally but don't overdo it."
)

try:
    from PIL import Image, ImageDraw, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

WIN_W, WIN_H  = 520, 500
PANEL_X       = 350
BTN_W         = WIN_W - PANEL_X - 8
TAB_Y         = 8
CONTENT_Y     = 58
CONTENT_H     = WIN_H - CONTENT_Y - 36


# ── Windows-native system info ────────────────────────────────────────────────

class _MEMSTATEX(ctypes.Structure):
    _fields_ = [
        ("dwLength",                ctypes.c_ulong),
        ("dwMemoryLoad",            ctypes.c_ulong),
        ("ullTotalPhys",            ctypes.c_ulonglong),
        ("ullAvailPhys",            ctypes.c_ulonglong),
        ("ullTotalPageFile",        ctypes.c_ulonglong),
        ("ullAvailPageFile",        ctypes.c_ulonglong),
        ("ullTotalVirtual",         ctypes.c_ulonglong),
        ("ullAvailVirtual",         ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _ram_info():
    s = _MEMSTATEX()
    s.dwLength = ctypes.sizeof(s)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.dwMemoryLoad, s.ullTotalPhys - s.ullAvailPhys, s.ullTotalPhys


def _disk_info(path="C:\\"):
    free  = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(path, None, ctypes.byref(total), ctypes.byref(free))
    used = total.value - free.value
    return int(used / total.value * 100) if total.value else 0, used, total.value


def _cpu_percent():
    try:
        out = subprocess.check_output(
            ["wmic", "cpu", "get", "loadpercentage", "/value"],
            timeout=4, creationflags=subprocess.CREATE_NO_WINDOW
        ).decode(errors="replace")
        for line in out.splitlines():
            if "=" in line:
                val = line.split("=")[1].strip()
                if val.isdigit():
                    return float(val)
    except Exception:
        pass
    return 0.0


def _get_processes():
    try:
        out = subprocess.check_output(
            ["tasklist", "/fo", "csv"],
            timeout=6, creationflags=subprocess.CREATE_NO_WINDOW
        ).decode(errors="replace")
        procs = []
        reader = csv.reader(io.StringIO(out))
        next(reader)
        for row in reader:
            if len(row) < 5:
                continue
            mem_str = row[4].replace(",", "").replace("\xa0", "").replace(" K", "").strip()
            try:
                mem_kb = int(mem_str)
            except ValueError:
                mem_kb = 0
            pid_str = row[1].strip('"')
            procs.append({
                "name":    row[0].strip('"'),
                "pid":     int(pid_str) if pid_str.isdigit() else 0,
                "mem_kb":  mem_kb,
                "session": row[2].strip('"'),
            })
        return procs
    except Exception:
        return []


def _kill_pid(pid):
    subprocess.call(
        ["taskkill", "/PID", str(pid), "/F"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _desktops():
    paths = set()
    paths.add(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
    paths.add(buf.value)
    return [p for p in paths if os.path.isdir(p)]


# ── Windows DPAPI encryption (zero deps, tied to your Windows login) ──────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_encrypt(plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    buf  = ctypes.create_string_buffer(data, len(data))
    inp  = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out  = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("DPAPI encrypt failed")
    enc = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return base64.b64encode(enc).decode("ascii")


def _dpapi_decrypt(b64_cipher: str) -> str:
    raw  = base64.b64decode(b64_cipher)
    buf  = ctypes.create_string_buffer(raw, len(raw))
    inp  = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out  = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("DPAPI decrypt failed")
    txt = ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return txt


# ── UI helpers ────────────────────────────────────────────────────────────────

def _darken(hex_color, f=0.65):
    r, g, b = int(hex_color[1:3],16), int(hex_color[3:5],16), int(hex_color[5:7],16)
    return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"


def _build_bg():
    if not PIL_AVAILABLE:
        return None
    try:
        base    = Image.open("Triton (54).png").resize((WIN_W, WIN_H), Image.LANCZOS).convert("RGBA")
        overlay = Image.new("RGBA", (WIN_W, WIN_H), (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)
        start   = WIN_W // 4
        for x in range(start, WIN_W):
            t     = (x - start) / (WIN_W - start)
            alpha = int(min(1.0, t * 1.4) * 215)
            draw.line([(x, 0), (x, WIN_H - 1)], fill=(8, 6, 26, alpha))
        return ImageTk.PhotoImage(Image.alpha_composite(base, overlay).convert("RGB"))
    except Exception:
        return None


# ── Main app ──────────────────────────────────────────────────────────────────

class ConceptStaff:

    def __init__(self, root):
        self.root = root
        self.root.title("Concept Staff")
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.resizable(False, False)
        self.root.configure(bg=DARK_BG)

        self.websites = [
            "https://www.blackbox.ai/",
            "https://chat.openai.com/",
            "https://gemini.google.com/",
            "https://poe.com/",
            "https://civitai.com/",
            "https://huggingface.co/",
            "https://github.com/oobabooga/text-generation-webui",
            "https://app.diagrams.net/",
            "https://github.com/AUTOMATIC1111",
            "https://voyant-tools.org/",
        ]
        self.spirit_history = []
        self._particles     = []
        self._forge_photo   = None

        # ── Background canvas ─────────────────────────────────────────────
        self._canvas = tk.Canvas(self.root, width=WIN_W, height=WIN_H,
                                 bd=0, highlightthickness=0, bg=DARK_BG)
        self._canvas.place(x=0, y=0)
        self.photo = _build_bg()
        if self.photo:
            self._canvas.create_image(0, 0, image=self.photo, anchor="nw")

        # ── Status badge ──────────────────────────────────────────────────
        self.label = tk.Label(self.root, text="Ready", bg=UCSD_GOLD,
                              fg="black", font=("Segoe UI", 9, "bold"), padx=5)
        self.label.place(x=WIN_W - 70, y=10, width=62, height=22)

        # ── Tab bar ───────────────────────────────────────────────────────
        tab_bar = tk.Frame(self.root, bg=DARK_BG)
        tab_bar.place(x=PANEL_X, y=TAB_Y, width=BTN_W, height=44)

        self._tab_frames = {}
        self._tab_btns   = {}

        for key, label in [("original", "ORIGINAL"), ("zone_a", "ZONE A"), ("arcane", "ARCANE")]:
            btn = tk.Button(tab_bar, text=label, font=FONT_SMALL,
                            relief=tk.FLAT, bd=0, cursor="hand2",
                            command=lambda k=key: self._switch_tab(k))
            btn.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=1, pady=2)
            self._tab_btns[key] = btn

            frame = tk.Frame(self.root, bg=DARK_BG)
            frame.place(x=PANEL_X, y=CONTENT_Y, width=BTN_W, height=CONTENT_H)
            self._tab_frames[key] = frame

        # ── Build tab contents ────────────────────────────────────────────
        def tbtn(tab, text, cmd, color=UCSD_BLUE):
            h = _darken(color)
            b = tk.Button(self._tab_frames[tab], text=text, command=cmd,
                          bg=color, fg="white", font=FONT_UI,
                          relief=tk.FLAT, bd=0, cursor="hand2",
                          activebackground=h, activeforeground="white",
                          anchor="w", padx=8)
            b.pack(fill=tk.X, pady=2)
            b.bind("<Enter>", lambda e, b=b, h=h: b.config(bg=h))
            b.bind("<Leave>", lambda e, b=b, c=color: b.config(bg=c))
            return b

        # ORIGINAL
        tbtn("original", "Target Assignment",  self.open_websites)
        tbtn("original", "Atune Staff",        self.atune_staff)
        tbtn("original", "Activate God Mode",  self.create_god_mode_panel)
        tbtn("original", "Create Hidden Zone", self.create_hidden_zone)

        # ZONE A
        self._spirit_btn = tbtn("zone_a", "Commune with Spirit",
                                self.commune_with_spirit, color="#4B0082")
        tbtn("zone_a", "Scrying Glass",   self.scrying_glass)
        tbtn("zone_a", "Teleport",        self.teleport)
        tbtn("zone_a", "Summon Terminal", self.summon_terminal)
        tbtn("zone_a", "Sense Mana",      self.sense_mana)

        # ARCANE
        tbtn("arcane", "Image Forge",    self.image_forge,    color="#1a0a2e")
        tbtn("arcane", "Lore Engine",    self.lore_engine,    color="#0a1a0a")
        tbtn("arcane", "Screen Capture", self.screen_capture, color="#0a1a2e")
        tbtn("arcane", "Key Ring",       self.key_ring,       color="#1a0a0a")

        # ── Quit (always visible) ─────────────────────────────────────────
        quit_btn = tk.Button(self.root, text="Quit", command=self.root.quit,
                             bg="#2a0000", fg="white", font=FONT_UI,
                             relief=tk.FLAT, bd=0, cursor="hand2")
        quit_btn.place(x=PANEL_X, y=WIN_H - 34, width=BTN_W, height=26)

        # ── Init ──────────────────────────────────────────────────────────
        self._switch_tab("original")
        self.root.after(200, self._animate_particles)
        self.root.after(200, lambda: self._pulse_spirit(0))

    # ── Tab switching ─────────────────────────────────────────────────────────

    def _switch_tab(self, key):
        for k, frame in self._tab_frames.items():
            if k == key:
                frame.place(x=PANEL_X, y=CONTENT_Y, width=BTN_W, height=CONTENT_H)
            else:
                frame.place_forget()
        for k, btn in self._tab_btns.items():
            if k == key:
                btn.config(bg=UCSD_GOLD, fg="black")
            else:
                btn.config(bg="#14122a", fg="#555577")

    # ── Animations ────────────────────────────────────────────────────────────

    def _animate_particles(self):
        if not self.root.winfo_exists():
            return
        c = self._canvas
        if random.random() < 0.4 and len(self._particles) < 25:
            x    = random.randint(PANEL_X + 4, WIN_W - 6)
            y    = random.randint(WIN_H // 2, WIN_H)
            life = random.randint(60, 160)
            size = random.randint(1, 3)
            col  = random.choice(["#3333aa","#5533bb","#7733cc","#3355aa","#aa44cc"])
            pid  = c.create_oval(x, y, x+size, y+size, fill=col, outline="")
            self._particles.append({
                "id": pid, "x": float(x), "y": float(y),
                "vx": random.uniform(-0.15, 0.15),
                "vy": random.uniform(-0.9, -0.3),
                "life": life,
            })
        alive = []
        for p in self._particles:
            p["life"] -= 1
            p["x"]    += p["vx"]
            p["y"]    += p["vy"]
            if p["life"] > 0 and 0 <= p["x"] <= WIN_W and 0 <= p["y"] <= WIN_H:
                c.coords(p["id"], p["x"], p["y"], p["x"]+2, p["y"]+2)
                alive.append(p)
            else:
                c.delete(p["id"])
        self._particles = alive
        self.root.after(40, self._animate_particles)

    def _pulse_spirit(self, t):
        if not self.root.winfo_exists():
            return
        r = int(0x4B + math.sin(t) * 18)
        g = int(0x00 + math.sin(t) * 8)
        b = int(0x82 + math.sin(t) * 30)
        try:
            self._spirit_btn.config(bg=f"#{r:02x}{g:02x}{b:02x}")
        except Exception:
            return
        self.root.after(50, lambda: self._pulse_spirit(t + 0.07))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, text):
        self.label.config(text=text)
        self.root.update()

    def _query_spirit(self, messages):
        parts = [SPIRIT_SYSTEM_PROMPT, ""]
        for m in messages[:-1]:
            parts.append(("Wielder" if m["role"] == "user" else "Spirit") + ": " + m["content"])
        parts += [f"Wielder: {messages[-1]['content']}", "Spirit:"]
        full = "\n".join(parts)
        if len(full) > 1200:
            parts = parts[:2] + parts[-6:]
            full  = "\n".join(parts)
        url = f"{SPIRIT_API}/{urllib.parse.quote(full, safe='')}"
        req = urllib.request.Request(url, headers={"User-Agent": "ConceptStaff/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace").strip()

    def _chat_window(self, title, bg_color, header_color, history, on_save=None):
        """Reusable chat panel. on_save(history) called after each reply."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("480x500")
        win.configure(bg=bg_color)
        tk.Label(win, text=f"~ {title} ~", bg=bg_color, fg=header_color,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 0))

        if on_save and history:
            tk.Label(win, text=f"{len(history)//2} exchanges remembered",
                     bg=bg_color, fg=_darken(header_color, 0.8),
                     font=("Segoe UI", 8)).pack()

        chat_box = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, bg=_darken(bg_color, 0.5), fg="#e0c8ff",
            font=("Consolas", 10), state=tk.DISABLED, relief=tk.FLAT)
        chat_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        chat_box.tag_config("wielder", foreground=UCSD_GOLD)
        chat_box.tag_config("spirit",  foreground=header_color)
        chat_box.tag_config("error",   foreground="#ff5555")

        # Replay history into chat
        for m in history:
            prefix = "Wielder" if m["role"] == "user" else "Spirit"
            tag    = "wielder" if m["role"] == "user" else "spirit"
            chat_box.config(state=tk.NORMAL)
            chat_box.insert(tk.END, f"{prefix}: {m['content']}\n\n", tag)
            chat_box.config(state=tk.DISABLED)

        bot   = tk.Frame(win, bg=bg_color)
        bot.pack(fill=tk.X, padx=10, pady=(0, 10))
        entry = tk.Entry(bot, font=("Consolas", 10), bg=_darken(bg_color, 0.3),
                         fg="white", insertbackground="white", relief=tk.FLAT)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

        def append(text, tag):
            chat_box.config(state=tk.NORMAL)
            chat_box.insert(tk.END, text, tag)
            chat_box.see(tk.END)
            chat_box.config(state=tk.DISABLED)

        def send(event=None):
            msg = entry.get().strip()
            if not msg:
                return
            entry.delete(0, tk.END)
            cast_btn.config(state=tk.DISABLED)
            append(f"Wielder: {msg}\n", "wielder")
            history.append({"role": "user", "content": msg})

            def call():
                try:
                    reply = self._query_spirit(history)
                    history.append({"role": "assistant", "content": reply})
                    if on_save:
                        on_save(history)
                    append(f"Spirit: {reply}\n\n", "spirit")
                except Exception:
                    append("Spirit: The aether is silent... check your connection.\n\n", "error")
                finally:
                    cast_btn.config(state=tk.NORMAL)

            threading.Thread(target=call, daemon=True).start()

        cast_btn = tk.Button(bot, text="Cast", command=send,
                             bg="#4B0082", fg="white", font=FONT_UI,
                             relief=tk.FLAT, padx=12)
        cast_btn.pack(side=tk.RIGHT, padx=(6, 0))
        entry.bind("<Return>", send)
        entry.focus()
        if not history:
            append("Spirit: The Staff stirs... I am here, Wielder. Ask what you will.\n\n", "spirit")
        return win

    # ── ORIGINAL abilities ────────────────────────────────────────────────────

    def open_websites(self):
        self._set_status("Opening...")
        import webbrowser
        for url in self.websites:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        self._set_status("Ready")

    def atune_staff(self):
        win = tk.Toplevel(self.root)
        win.title("Atune Staff")
        win.geometry("300x180")
        win.configure(bg=DARK_BG)
        tk.Label(win, text="Website URL:", bg=DARK_BG, fg="white",
                 font=FONT_UI).pack(pady=(12, 2))
        entry = tk.Entry(win, width=34, font=("Consolas", 9))
        entry.pack(pady=4)

        def add():
            url = entry.get().strip()
            if url:
                self.websites.append(url)
                entry.delete(0, tk.END)
                messagebox.showinfo("Atune Staff", "Site added.")
            else:
                messagebox.showwarning("Atune Staff", "Enter a URL first.")

        def remove():
            url = entry.get().strip()
            if url in self.websites:
                self.websites.remove(url)
                entry.delete(0, tk.END)
                messagebox.showinfo("Atune Staff", "Site removed.")
            else:
                messagebox.showwarning("Atune Staff", "URL not found.")

        bf = tk.Frame(win, bg=DARK_BG)
        bf.pack(pady=6)
        tk.Button(bf, text="Add",    command=add,    bg=UCSD_BLUE,  fg="white",
                  font=FONT_UI, relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=4)
        tk.Button(bf, text="Remove", command=remove, bg="#660000", fg="white",
                  font=FONT_UI, relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=4)

    def create_god_mode_panel(self):
        try:
            created = []
            for desk in _desktops():
                path = os.path.join(desk, "GodMode.{ED7BA470-8E54-465E-825C-99712043E01C}")
                if not os.path.exists(path):
                    os.makedirs(path)
                    created.append(desk)
            opened = False
            for desk in _desktops():
                gm_path = os.path.join(desk, "GodMode.{ED7BA470-8E54-465E-825C-99712043E01C}")
                if os.path.exists(gm_path):
                    try:
                        subprocess.Popen(["explorer.exe", gm_path])
                        opened = True
                        break
                    except Exception:
                        pass
            if created:
                messagebox.showinfo("God Mode", "God Mode activated — panel opening.")
            else:
                messagebox.showwarning("God Mode", "Already active — opening panel.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def create_hidden_zone(self):
        try:
            created = []
            for desk in _desktops():
                path = os.path.join(desk, "​")
                if not os.path.exists(path):
                    os.makedirs(path)
                    ctypes.windll.kernel32.SetFileAttributesW(path, 2)
                    created.append(desk)
            if created:
                messagebox.showinfo("Hidden Zone", "Hidden zone created on Desktop.")
                self.root.iconify()
            else:
                messagebox.showwarning("Hidden Zone", "Already exists.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── ZONE A abilities ──────────────────────────────────────────────────────

    def commune_with_spirit(self):
        self._chat_window("Spirit of the Staff", "#1a0033", "#c8a2c8",
                          self.spirit_history)

    def scrying_glass(self):
        win = tk.Toplevel(self.root)
        win.title("Scrying Glass")
        win.geometry("300x185")
        win.configure(bg="#001a33")
        win.resizable(False, False)
        tk.Label(win, text="~ Scrying Glass ~", bg="#001a33", fg=UCSD_GOLD,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 6))
        frame = tk.Frame(win, bg="#001a33")
        frame.pack(fill=tk.BOTH, expand=True, padx=16)

        def make_row(lbl):
            row = tk.Frame(frame, bg="#001a33")
            row.pack(fill=tk.X, pady=4)
            tk.Label(row, text=lbl, bg="#001a33", fg="#aaaacc",
                     font=("Consolas", 10), width=6, anchor="w").pack(side=tk.LEFT)
            bg_f = tk.Frame(row, bg="#0d0d2b", height=14, width=150)
            bg_f.pack(side=tk.LEFT, padx=(4, 6))
            bg_f.pack_propagate(False)
            fill = tk.Frame(bg_f, bg=UCSD_BLUE, height=14)
            fill.place(x=0, y=0, height=14, width=0)
            val = tk.Label(row, text="--", bg="#001a33", fg="white", font=("Consolas", 10))
            val.pack(side=tk.LEFT)
            return fill, val

        cpu_bar, cpu_val = make_row("CPU")
        ram_bar, ram_val = make_row("RAM")
        dsk_bar, dsk_val = make_row("Disk")
        cpu_cache = [0.0]

        def update():
            if not win.winfo_exists():
                return
            cpu = cpu_cache[0]
            cpu_bar.place(width=int(1.5 * cpu))
            cpu_bar.config(bg="#ff4444" if cpu > 80 else UCSD_BLUE)
            cpu_val.config(text=f"{cpu:.0f}%")
            rp, ru, rt = _ram_info()
            ram_bar.place(width=int(1.5 * rp))
            ram_val.config(text=f"{rp}%  {ru//(1024**3):.1f}/{rt//(1024**3):.1f}GB")
            dp, du, dt = _disk_info()
            dsk_bar.place(width=int(1.5 * dp))
            dsk_val.config(text=f"{dp}%  {du//(1024**3):.0f}/{dt//(1024**3):.0f}GB")
            threading.Thread(target=lambda: cpu_cache.__setitem__(0, _cpu_percent()), daemon=True).start()
            win.after(2000, update)

        threading.Thread(target=lambda: cpu_cache.__setitem__(0, _cpu_percent()), daemon=True).start()
        win.after(300, update)

    def teleport(self):
        home = os.environ["USERPROFILE"]
        folders = {
            "Desktop":      os.path.join(home, "Desktop"),
            "Documents":    os.path.join(home, "Documents"),
            "Downloads":    os.path.join(home, "Downloads"),
            "Pictures":     os.path.join(home, "Pictures"),
            "Music":        os.path.join(home, "Music"),
            "Videos":       os.path.join(home, "Videos"),
            "AppData":      os.environ.get("APPDATA", ""),
            "Staff Folder": os.path.dirname(os.path.abspath(__file__)),
        }
        win = tk.Toplevel(self.root)
        win.title("Teleport")
        win.geometry("220x290")
        win.configure(bg="#001a33")
        win.resizable(False, False)
        tk.Label(win, text="~ Teleport ~", bg="#001a33", fg=UCSD_GOLD,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 6))
        for name, path in folders.items():
            if not path:
                continue
            tk.Button(win, text=name, bg=UCSD_BLUE, fg="white", font=FONT_UI,
                      relief=tk.FLAT, cursor="hand2",
                      command=lambda p=path: os.startfile(p)
                      ).pack(fill=tk.X, padx=16, pady=2)

    def summon_terminal(self):
        here = os.path.dirname(os.path.abspath(__file__))
        profiles = {
            "PowerShell (here)": ["powershell", "-NoExit", "-Command", f"Set-Location '{here}'"],
            "PowerShell (home)": ["powershell", "-NoExit", "-Command", "Set-Location $env:USERPROFILE"],
            "PowerShell (admin)": "admin",
            "Windows Terminal":   ["wt"],
            "CMD":                ["cmd"],
        }
        win = tk.Toplevel(self.root)
        win.title("Summon Terminal")
        win.geometry("240x220")
        win.configure(bg="#001a33")
        win.resizable(False, False)
        tk.Label(win, text="~ Summon Terminal ~", bg="#001a33", fg=UCSD_GOLD,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 6))

        def launch(cmd):
            if cmd == "admin":
                try:
                    ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell", "", None, 1)
                except Exception as e:
                    messagebox.showerror("Error", str(e))
                return
            try:
                subprocess.Popen(cmd)
            except FileNotFoundError:
                messagebox.showerror("Error", f"Not found: {cmd[0]}")

        for name, cmd in profiles.items():
            tk.Button(win, text=name, bg=UCSD_BLUE, fg="white", font=FONT_UI,
                      relief=tk.FLAT, cursor="hand2",
                      command=lambda c=cmd: launch(c)
                      ).pack(fill=tk.X, padx=16, pady=3)

    def sense_mana(self):
        win = tk.Toplevel(self.root)
        win.title("Sense Mana")
        win.geometry("560x420")
        win.configure(bg="#001a33")
        tk.Label(win, text="~ Sense Mana ~", bg="#001a33", fg=UCSD_GOLD,
                 font=("Segoe UI", 12, "bold")).pack(pady=(8, 4))

        sort_col  = {"key": "mem_kb", "reverse": True}
        cols      = ("pid", "name", "mem", "session")
        style     = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Mana.Treeview", background="#0d0d2b", foreground="white",
                        fieldbackground="#0d0d2b", rowheight=22, font=("Consolas", 9))
        style.configure("Mana.Treeview.Heading", background=UCSD_BLUE, foreground="white",
                        font=("Segoe UI", 9, "bold"))
        style.map("Mana.Treeview", background=[("selected", "#4B0082")])

        frame = tk.Frame(win, bg="#001a33")
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        sb = tk.Scrollbar(frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            style="Mana.Treeview", yscrollcommand=sb.set)
        sb.config(command=tree.yview)
        for c, label, width in [("pid","PID",60),("name","Process",240),
                                 ("mem","Memory",90),("session","Session",100)]:
            col_key = {"pid":"pid","name":"name","mem":"mem_kb","session":"session"}[c]
            tree.heading(c, text=label,
                         command=lambda ck=col_key: (sort_col.update(
                             reverse=not sort_col["reverse"] if sort_col["key"]==ck else True,
                             key=ck) or populate()))
            tree.column(c, width=width, anchor="center" if c != "name" else "w")
        tree.pack(fill=tk.BOTH, expand=True)

        bar = tk.Frame(win, bg="#001a33")
        bar.pack(fill=tk.X, padx=10, pady=(2, 8))
        info_var = tk.StringVar(value="Loading...")
        tk.Label(bar, textvariable=info_var, bg="#001a33", fg="#aaaacc",
                 font=("Consolas", 9)).pack(side=tk.LEFT)

        def kill_selected():
            sel = tree.selection()
            if not sel:
                return
            pid  = int(tree.item(sel[0])["values"][0])
            name = tree.item(sel[0])["values"][1]
            if messagebox.askyesno("Confirm", f"Terminate {name} (PID {pid})?"):
                _kill_pid(pid)
                populate()

        tk.Button(bar, text="Kill Process", command=kill_selected,
                  bg="#8B0000", fg="white", font=FONT_UI,
                  relief=tk.FLAT, padx=8).pack(side=tk.RIGHT)

        _running = [True]

        def populate():
            if not win.winfo_exists() or not _running[0]:
                return
            procs = _get_processes()
            procs.sort(key=lambda p: p.get(sort_col["key"]) or 0, reverse=sort_col["reverse"])
            sel_pid = int(tree.item(tree.selection()[0])["values"][0]) if tree.selection() else None
            tree.delete(*tree.get_children())
            for p in procs[:100]:
                iid = tree.insert("", tk.END, values=(
                    p["pid"], p["name"][:40], f"{p['mem_kb']/1024:.1f} MB", p["session"]))
                if p["pid"] == sel_pid:
                    tree.selection_set(iid); tree.see(iid)
            _, ru, rt = _ram_info()
            info_var.set(f"RAM {int(ru/rt*100) if rt else 0}%  "
                         f"({ru//(1024**2):.0f}/{rt//(1024**2):.0f} MB)   Procs: {len(procs)}")
            win.after(3000, lambda: threading.Thread(target=lambda: win.after(0, populate),
                                                     daemon=True).start())

        win.protocol("WM_DELETE_WINDOW", lambda: (_running.__setitem__(0, False), win.destroy()))
        threading.Thread(target=populate, daemon=True).start()

    # ── ARCANE abilities ──────────────────────────────────────────────────────

    def image_forge(self):
        win = tk.Toplevel(self.root)
        win.title("Image Forge")
        win.geometry("540x560")
        win.configure(bg="#0d0820")
        tk.Label(win, text="~ Image Forge ~", bg="#0d0820", fg=UCSD_GOLD,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 4))

        top = tk.Frame(win, bg="#0d0820")
        top.pack(fill=tk.X, padx=16, pady=(0, 8))
        entry = tk.Entry(top, font=("Consolas", 10), bg="#1a1040",
                         fg="white", insertbackground="white", relief=tk.FLAT)
        entry.insert(0, "a wizard's glowing staff in a dark mystical forest")
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

        img_label = tk.Label(win, bg="#0d0820", text="Enter a prompt and click Forge",
                             fg="#444466", font=("Segoe UI", 10),
                             width=50, height=18)
        img_label.pack(pady=4)

        status_var = tk.StringVar(value="")
        tk.Label(win, textvariable=status_var, bg="#0d0820", fg="#888888",
                 font=("Consolas", 8)).pack()

        def forge(event=None):
            prompt = entry.get().strip()
            if not prompt:
                return
            forge_btn.config(state=tk.DISABLED)
            status_var.set("Forging image...")
            img_label.config(text="Summoning...", image="", fg="#555577")

            def generate():
                try:
                    encoded = urllib.parse.quote(prompt, safe='')
                    url = f"{IMAGE_API}/{encoded}?width=512&height=400&nologo=true&seed={random.randint(1,9999)}"
                    req = urllib.request.Request(url, headers={"User-Agent": "ConceptStaff/1.0"})
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = r.read()

                    if PIL_AVAILABLE:
                        from PIL import Image as PILImage
                        img = PILImage.open(io.BytesIO(data)).resize((500, 390), PILImage.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                    else:
                        tmp = os.path.join(tempfile.gettempdir(), "staff_forge.png")
                        with open(tmp, "wb") as f:
                            f.write(data)
                        photo = tk.PhotoImage(file=tmp)

                    self._forge_photo = photo
                    img_label.config(image=photo, text="", width=0, height=0)
                    img_label.image = photo
                    status_var.set(f'"{prompt[:60]}"')
                except Exception as e:
                    status_var.set(f"Forge failed — check connection")
                    img_label.config(text="Forge failed", image="", fg="#ff5555")
                finally:
                    forge_btn.config(state=tk.NORMAL)

            threading.Thread(target=generate, daemon=True).start()

        forge_btn = tk.Button(top, text="Forge", command=forge,
                              bg="#4B0082", fg="white", font=FONT_UI,
                              relief=tk.FLAT, padx=12)
        forge_btn.pack(side=tk.RIGHT, padx=(6, 0))
        entry.bind("<Return>", forge)

    def lore_engine(self):
        history = []
        try:
            if os.path.exists(LORE_FILE):
                with open(LORE_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
        except Exception:
            history = []

        def save_lore(h):
            try:
                with open(LORE_FILE, "w", encoding="utf-8") as f:
                    json.dump(h[-40:], f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        self._chat_window("Lore Engine", "#0d1a0d", "#44cc88",
                          history, on_save=save_lore)

    def screen_capture(self):
        win = tk.Toplevel(self.root)
        win.title("Screen Capture")
        win.geometry("460x380")
        win.configure(bg="#0a0d1a")
        win.resizable(False, False)
        tk.Label(win, text="~ Screen Capture ~", bg="#0a0d1a", fg=UCSD_GOLD,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 4))

        preview = tk.Label(win, bg="#0d0d2b",
                           text="Click Capture — windows minimize, screenshot taken after 2s",
                           fg="#444466", font=("Segoe UI", 9),
                           width=54, height=14, wraplength=420)
        preview.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        preview.image = None

        status_var = tk.StringVar(value="")
        tk.Label(win, textvariable=status_var, bg="#0a0d1a", fg="#888888",
                 font=("Consolas", 8)).pack(pady=(2, 0))

        self._capture_photo = None

        def do_capture():
            btn.config(state=tk.DISABLED)
            status_var.set("Capturing in 2 seconds...")
            win.update()
            self.root.iconify()
            win.iconify()
            win.after(2000, lambda: threading.Thread(target=_grab, daemon=True).start())

        def _grab():
            try:
                ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                desks    = _desktops()
                save_dir = desks[0] if desks else os.environ["USERPROFILE"]
                save_path = os.path.join(save_dir, f"capture_{ts}.png")

                if PIL_AVAILABLE:
                    from PIL import ImageGrab
                    img = ImageGrab.grab(all_screens=False)
                    img.save(save_path)
                    thumb = img.resize((420, 260), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(thumb)
                    win.after(0, lambda: _show(photo, save_path))
                else:
                    ps = (
                        "Add-Type -AssemblyName System.Windows.Forms;"
                        "Add-Type -AssemblyName System.Drawing;"
                        "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                        "$bmp=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
                        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                        "$g.CopyFromScreen($s.Left,$s.Top,0,0,$s.Size);"
                        f"$bmp.Save('{save_path}');"
                        "$g.Dispose();$bmp.Dispose()"
                    )
                    subprocess.run(
                        ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
                    win.after(0, lambda: _show(None, save_path))
            except Exception as e:
                win.after(0, lambda err=e: _fail(err))

        def _show(photo, path):
            self.root.deiconify()
            win.deiconify()
            if photo:
                self._capture_photo = photo
                preview.config(image=photo, text="", bg="#0d0d2b")
                preview.image = photo
            else:
                preview.config(text=f"Saved (no preview — install Pillow for thumbnails)\n{os.path.basename(path)}",
                               fg="#aaaacc")
            status_var.set(f"Saved to Desktop: {os.path.basename(path)}")
            btn.config(state=tk.NORMAL)

        def _fail(err):
            self.root.deiconify()
            win.deiconify()
            status_var.set(f"Capture failed: {err}")
            preview.config(text="Capture failed", fg="#ff5555")
            btn.config(state=tk.NORMAL)

        btn = tk.Button(win, text="Capture Screen", command=do_capture,
                        bg="#003366", fg="white", font=FONT_UI,
                        relief=tk.FLAT, padx=14, pady=4, cursor="hand2")
        btn.pack(pady=(4, 12))

    def key_ring(self):
        entries = []
        try:
            if os.path.exists(KEYRING_FILE):
                with open(KEYRING_FILE, "r", encoding="utf-8") as f:
                    entries = json.load(f)
        except Exception:
            entries = []

        def save():
            try:
                with open(KEYRING_FILE, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=2)
            except Exception as e:
                messagebox.showerror("Key Ring", f"Save failed: {e}", parent=win)

        win = tk.Toplevel(self.root)
        win.title("Key Ring")
        win.geometry("480x400")
        win.configure(bg="#14080a")
        win.resizable(False, False)
        tk.Label(win, text="~ Key Ring ~", bg="#14080a", fg=UCSD_GOLD,
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 4))

        list_frame = tk.Frame(win, bg="#14080a")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb = tk.Listbox(list_frame, yscrollcommand=sb.set,
                        bg="#0d0508", fg="#e0c8cc", font=("Consolas", 10),
                        selectbackground="#4B0082", activestyle="none",
                        relief=tk.FLAT, bd=0)
        sb.config(command=lb.yview)
        lb.pack(fill=tk.BOTH, expand=True)

        def refresh():
            lb.delete(0, tk.END)
            for e in entries:
                user = f"  {e.get('username','')}" if e.get("username") else ""
                lb.insert(tk.END, f"  {e.get('label','?')}{user}")

        refresh()

        def add_entry():
            dlg = tk.Toplevel(win)
            dlg.title("Add Credential")
            dlg.geometry("300x260")
            dlg.configure(bg="#14080a")
            dlg.grab_set()
            fields = {}
            for lbl, key, masked in [
                ("Label / Site:", "label", False),
                ("Username / Email:", "username", False),
                ("Password:", "password", True),
                ("URL (optional):", "url", False),
            ]:
                tk.Label(dlg, text=lbl, bg="#14080a", fg="#ccaaaa",
                         font=FONT_UI).pack(anchor="w", padx=14, pady=(6, 0))
                e = tk.Entry(dlg, font=("Consolas", 9), bg="#0d0508",
                             fg="white", insertbackground="white", relief=tk.FLAT,
                             show="•" if masked else "")
                e.pack(fill=tk.X, padx=14, ipady=4)
                fields[key] = e

            def confirm():
                label = fields["label"].get().strip()
                pwd   = fields["password"].get()
                if not label or not pwd:
                    messagebox.showwarning("Key Ring", "Label and password required.", parent=dlg)
                    return
                try:
                    enc = _dpapi_encrypt(pwd)
                except Exception as e:
                    messagebox.showerror("Key Ring", f"Encryption failed: {e}", parent=dlg)
                    return
                entries.append({
                    "label":        label,
                    "username":     fields["username"].get().strip(),
                    "password_enc": enc,
                    "url":          fields["url"].get().strip(),
                })
                save()
                refresh()
                dlg.destroy()

            tk.Button(dlg, text="Save Credential", command=confirm,
                      bg=UCSD_BLUE, fg="white", font=FONT_UI,
                      relief=tk.FLAT, padx=12).pack(pady=10)

        def copy_password():
            sel = lb.curselection()
            if not sel:
                return
            try:
                pwd = _dpapi_decrypt(entries[sel[0]]["password_enc"])
                win.clipboard_clear()
                win.clipboard_append(pwd)
                win.update()
                status_var.set("Password copied to clipboard.")
            except Exception as e:
                messagebox.showerror("Key Ring", f"Decrypt failed: {e}", parent=win)

        def show_password():
            sel = lb.curselection()
            if not sel:
                return
            entry = entries[sel[0]]
            try:
                pwd = _dpapi_decrypt(entry["password_enc"])
            except Exception as e:
                messagebox.showerror("Key Ring", f"Decrypt failed: {e}", parent=win)
                return
            popup = tk.Toplevel(win)
            popup.title(entry["label"])
            popup.geometry("300x120")
            popup.configure(bg="#14080a")
            popup.grab_set()
            tk.Label(popup, text=entry["label"], bg="#14080a", fg=UCSD_GOLD,
                     font=("Segoe UI", 10, "bold")).pack(pady=(10, 2))
            tk.Label(popup, text=pwd, bg="#14080a", fg="white",
                     font=("Consolas", 12)).pack()
            tk.Label(popup, text="Auto-closes in 15s", bg="#14080a",
                     fg="#333344", font=("Segoe UI", 7)).pack(pady=(4, 0))
            popup.after(15000, popup.destroy)

        def delete_entry():
            sel = lb.curselection()
            if not sel:
                return
            label = entries[sel[0]]["label"]
            if messagebox.askyesno("Key Ring", f"Delete '{label}'?", parent=win):
                entries.pop(sel[0])
                save()
                refresh()

        bar = tk.Frame(win, bg="#14080a")
        bar.pack(fill=tk.X, padx=12, pady=(0, 4))
        for txt, cmd, col in [
            ("+ Add",      add_entry,      "#1a3366"),
            ("Copy Pwd",   copy_password,  "#4B0082"),
            ("Show Pwd",   show_password,  "#4B0082"),
            ("Delete",     delete_entry,   "#660000"),
        ]:
            tk.Button(bar, text=txt, command=cmd, bg=col, fg="white",
                      font=FONT_SMALL, relief=tk.FLAT, padx=8, pady=3,
                      cursor="hand2").pack(side=tk.LEFT, padx=2)

        status_var = tk.StringVar(value="")
        tk.Label(win, textvariable=status_var, bg="#14080a", fg="#888888",
                 font=("Consolas", 8)).pack()
        tk.Label(win, text="Protected by Windows DPAPI — only decryptable on this account",
                 bg="#14080a", fg="#2a1a1a", font=("Segoe UI", 7)).pack(pady=(2, 6))


def main():
    root = tk.Tk()
    root.attributes("-alpha", 0.0)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{WIN_W}x{WIN_H}+{(sw-WIN_W)//2}+{(sh-WIN_H)//2}")
    ConceptStaff(root)

    def _fade(a=0.0):
        a = min(1.0, a + 0.07)
        root.attributes("-alpha", a)
        if a < 1.0:
            root.after(16, lambda: _fade(a))

    root.after(80, _fade)
    root.mainloop()


if __name__ == "__main__":
    main()
