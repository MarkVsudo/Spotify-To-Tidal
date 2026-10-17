#!/usr/bin/env python3

import csv
import os
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from contextlib import closing
from tkinter import messagebox, ttk

import tidalapi

import transfer as T  # reuses the login, loading and matching code

# ---------- theme ----------
BG = "#0f1115"
CARD = "#171a21"
CARD2 = "#1f232c"
TREE_BG = "#13161c"
BORDER = "#262b36"
TEXT = "#eef0f4"
MUTED = "#8b93a3"
ACCENT = "#1ed760"
ACCENT_HOVER = "#3be477"
ACCENT_DARK = "#04210e"
ACCENT_SOFT = "#1c2a22"
AMBER = "#f5b83d"
RED = "#ff6b6b"
BLUE = "#5aa9ff"
FONT = "Segoe UI"  
MONO = "Consolas"

CHECKED, UNCHECKED = "\u2611", "\u2610"

KINDS = {
    "tracks": {
        "label": "Liked songs",
        "empty": "Your liked songs will appear here.",
        "cols": [("Title", 300), ("Artist", 200), ("Album", 220)],
        "row": lambda t: (t["name"], ", ".join(a["name"] for a in t["artists"]), t["album"]["name"]),
        "match": T.match_track,
        "add": "add_track",
        "load": T.spotify_tracks,
    },
    "albums": {
        "label": "Albums",
        "empty": "Your saved albums will appear here.",
        "cols": [("Album", 300), ("Artist", 220), ("Year", 70)],
        "row": lambda a: (a["name"], ", ".join(x["name"] for x in a["artists"]), (a.get("release_date") or "")[:4]),
        "match": T.match_album,
        "add": "add_album",
        "load": T.spotify_albums,
    },
    "artists": {
        "label": "Artists",
        "empty": "The artists you follow will appear here.",
        "cols": [("Artist", 300), ("Genres", 300)],
        "row": lambda a: (a["name"], ", ".join((a.get("genres") or [])[:3])),
        "match": T.match_artist,
        "add": "add_artist",
        "load": T.spotify_artists,
    },
    "playlists": {
        "label": "Playlists",
        "empty": "Your playlists will appear here.",
        "cols": [("Playlist", 320), ("Owner", 200), ("Tracks", 70)],
        "row": lambda p: (p["name"], (p.get("owner") or {}).get("display_name") or "",
                          str((p.get("tracks") or p.get("items") or {}).get("total", ""))),
        "match": None,
        "add": None,
        "load": T.spotify_playlists,
        "reverse": False,
    },
}


def tag_for(text):
    t = (text or "").lower()
    if t.startswith(("added", "matched", "done")):
        return "ok"
    if t.startswith("no match"):
        return "warn"
    if t.startswith("error"):
        return "bad"
    if t.startswith("working"):
        return "info"
    return ""


class KindTab(tk.Frame):
    def __init__(self, parent, kind, cfg, on_change):
        super().__init__(parent, bg=CARD)
        self.kind, self.cfg, self.on_change = kind, cfg, on_change
        self.items, self.selected, self.done, self.status = [], set(), set(), {}

        bar = tk.Frame(self, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(14, 10))
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(bar, textvariable=self.query, width=32, style="Search.TEntry").pack(side="left")
        tk.Label(bar, text="Search", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=(8, 16))
        ttk.Button(bar, text="Select all", style="Ghost.TButton", command=lambda: self.set_visible(True)).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Select none", style="Ghost.TButton", command=lambda: self.set_visible(False)).pack(side="left")
        self.count = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=(FONT, 10))
        self.count.pack(side="right")

        wrap = tk.Frame(self, bg=TREE_BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        cols = ["sel"] + [c for c, _ in cfg["cols"]] + ["status"]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("sel", text="")
        self.tree.column("sel", width=44, anchor="center", stretch=False)
        for name, width in cfg["cols"]:
            self.tree.heading(name, text=name, anchor="w")
            self.tree.column(name, width=width, anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("status", width=130, stretch=False, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("ok", ACCENT), ("warn", AMBER), ("bad", RED), ("info", BLUE)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.focus()) if self.tree.focus() else None)

        self.empty = tk.Label(wrap, text=cfg["empty"], bg=TREE_BG, fg=MUTED, font=(FONT, 11))
        self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def load(self, items, done):
        self.items = [i for i in items if i.get("id")]
        self.done = set(done)
        self.selected = {i["id"] for i in self.items if i["id"] not in self.done}
        self.status = {i: "done" for i in self.done}
        self.empty.config(text="Nothing found here.")
        if self.items:
            self.empty.place_forget()
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        q = self.query.get().strip().lower()
        seq = reversed(self.items) if self.cfg.get("reverse", True) else self.items  # newest first
        shown = 0
        for item in seq:
            vals = self.cfg["row"](item)
            if q and q not in " ".join(vals).lower():
                continue
            iid = item["id"]
            mark = CHECKED if iid in self.selected else UNCHECKED
            st = self.status.get(iid, "")
            tag = tag_for(st)
            self.tree.insert("", "end", iid=iid, values=(mark, *vals, st), tags=(tag,) if tag else ())
            shown += 1
        self.shown = shown
        self.update_count()

    def update_count(self):
        if self.items:
            extra = f"{self.shown} shown \u00b7 " if self.query.get().strip() else ""
            self.count.config(text=f"{extra}{len(self.selected)} of {len(self.items)} selected")
        self.on_change()

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            iid = self.tree.identify_row(e.y)
            if iid:
                self.toggle(iid)

    def toggle(self, iid):
        if iid in self.selected:
            self.selected.discard(iid)
        else:
            self.selected.add(iid)
        if self.tree.exists(iid):
            self.tree.set(iid, "sel", CHECKED if iid in self.selected else UNCHECKED)
        self.update_count()

    def set_visible(self, on):
        for iid in self.tree.get_children():
            (self.selected.add if on else self.selected.discard)(iid)
            self.tree.set(iid, "sel", CHECKED if on else UNCHECKED)
        self.update_count()

    def set_status(self, iid, text):
        self.status[iid] = text
        if self.tree.exists(iid):
            self.tree.set(iid, "status", text)
            tag = tag_for(text)
            self.tree.item(iid, tags=(tag,) if tag else ())

    def selected_items(self):  # oldest first so recency order is kept on Tidal
        return [i for i in self.items if i["id"] in self.selected]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global FONT, MONO
        fams = set(tkfont.families())
        if FONT not in fams:
            FONT = "Helvetica Neue" if "Helvetica Neue" in fams else ("Ubuntu" if "Ubuntu" in fams else "Helvetica")
        if MONO not in fams:
            MONO = "Menlo" if "Menlo" in fams else "Courier"

        self.title("Spotify \u2192 Tidal")
        self.geometry("1100x860")
        self.minsize(980, 760)
        self.configure(bg=BG)
        self.setup_style()

        self.sp = self.tidal = None
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.loaded = False
        self.loading = False

        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        # header
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Spotify  \u2192  Tidal", bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w")
        tk.Label(head, text="Move your liked songs, albums, artists and playlists in three steps.",
                 bg=BG, fg=MUTED, font=(FONT, 11)).pack(anchor="w", pady=(2, 14))

        # step cards
        self.steps_row = tk.Frame(outer, bg=BG)
        self.steps_row.pack(fill="x")
        for i in range(3):
            self.steps_row.grid_columnconfigure(i, weight=1, uniform="s")
        self.steps = {
            "spotify": self.make_step(0, 1, "Spotify", "Connect Spotify", self.connect_spotify),
            "tidal": self.make_step(1, 2, "Tidal", "Connect Tidal", self.connect_tidal),
            "library": self.make_step(2, 3, "Your library", "Load library", self.load_library),
        }
        self.set_conn("spotify", "idle", "Not connected")
        self.set_conn("tidal", "idle", "Not connected")
        self.set_conn("library", "idle", "Not loaded")

        # hint line
        self.hint = tk.Label(outer, text="", bg=BG, fg=TEXT, font=(FONT, 11), anchor="w", justify="left")
        self.hint.pack(fill="x", pady=(14, 10))

        # bottom area is packed first so it always stays visible on short screens
        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(side="bottom", fill="x")

        # tabs
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        self.tabs = {}
        for kind, cfg in KINDS.items():
            tab = KindTab(self.nb, kind, cfg, self.refresh_state)
            self.tabs[kind] = tab
            self.nb.add(tab, text=cfg["label"])

        # action bar
        bar = tk.Frame(bottom, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        bar.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)
        self.dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Preview only (don't change Tidal yet)", variable=self.dry,
                        style="Card.TCheckbutton", command=self.refresh_state).pack(side="left")
        self.btn_go = ttk.Button(inner, text="Preview matches", style="Primary.TButton", command=self.start_transfer)
        self.btn_go.pack(side="right")
        self.btn_stop = ttk.Button(inner, text="Stop", style="Danger.TButton", command=self.stop.set, state="disabled")
        self.btn_stop.pack(side="right", padx=(0, 8))
        mid = tk.Frame(inner, bg=CARD)
        mid.pack(side="left", fill="x", expand=True, padx=24)
        self.lbl_prog = tk.Label(mid, text="", bg=CARD, fg=MUTED, font=(FONT, 9), width=9, anchor="e")
        self.lbl_prog.pack(side="right", padx=(8, 0))
        self.progress = ttk.Progressbar(mid, style="Accent.Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)

        # activity log
        tk.Label(bottom, text="ACTIVITY", bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(12, 4))
        self.log_box = tk.Text(bottom, height=5, state="disabled", bg="#0c0e12", fg="#b6bdc9", relief="flat",
                               borderwidth=0, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=8,
                               font=(MONO, 9), wrap="word")
        self.log_box.pack(fill="x")
        self.log_box.tag_configure("err", foreground=RED)
        self.log_box.tag_configure("ok", foreground=ACCENT)
        self.log("Welcome! Follow the three steps above. Preview is on, so nothing changes on Tidal until you turn it off.")

        self._ready = True
        self.refresh_state()
        self.after(100, self.poll)

    # ----- styling -----
    def setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=(FONT, 10))

        def button(name, bg, fg, hover, dis_bg, dis_fg, pad, bold):
            s.configure(name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                        focuscolor=bg, focusthickness=0, borderwidth=1, relief="flat", padding=pad,
                        font=(FONT, 10, "bold" if bold else "normal"))
            s.map(name,
                  background=[("disabled", dis_bg), ("active", hover)],
                  bordercolor=[("disabled", dis_bg), ("active", hover)],
                  lightcolor=[("disabled", dis_bg), ("active", hover)],
                  darkcolor=[("disabled", dis_bg), ("active", hover)],
                  foreground=[("disabled", dis_fg)])

        button("Primary.TButton", ACCENT, ACCENT_DARK, ACCENT_HOVER, "#2a2f3a", MUTED, (22, 11), True)
        button("Ghost.TButton", CARD2, TEXT, "#2b3140", CARD2, MUTED, (14, 8), False)
        button("Danger.TButton", "#3a2226", RED, "#4d2c32", CARD2, MUTED, (16, 11), True)

        s.configure("Search.TEntry", fieldbackground=CARD2, foreground=TEXT, insertcolor=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=(10, 7))
        s.map("Search.TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])

        s.configure("Treeview", background=TREE_BG, fieldbackground=TREE_BG, foreground=TEXT,
                    rowheight=34, borderwidth=0, font=(FONT, 10))
        s.configure("Treeview.Heading", background=CARD2, foreground=MUTED, font=(FONT, 9, "bold"),
                    relief="flat", padding=(10, 9), borderwidth=0)
        s.map("Treeview", background=[("selected", "#232a36")], foreground=[("selected", TEXT)])
        s.map("Treeview.Heading", background=[("active", "#262b36")])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        s.configure("Vertical.TScrollbar", background=CARD2, troughcolor=TREE_BG, bordercolor=TREE_BG,
                    arrowcolor=MUTED, lightcolor=CARD2, darkcolor=CARD2, gripcount=0, arrowsize=12)
        s.map("Vertical.TScrollbar", background=[("active", "#323949")])

        s.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG,
                    darkcolor=BG, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(22, 11),
                    font=(FONT, 10, "bold"), borderwidth=0, bordercolor=BG, lightcolor=BG,
                    darkcolor=BG, focuscolor=BG)
        s.map("TNotebook.Tab",
              background=[("selected", CARD), ("active", CARD2)],
              bordercolor=[("selected", CARD), ("active", CARD2)],
              lightcolor=[("selected", CARD), ("active", CARD2)],
              darkcolor=[("selected", CARD), ("active", CARD2)],
              foreground=[("selected", TEXT), ("active", TEXT)])

        s.configure("Accent.Horizontal.TProgressbar", troughcolor=CARD2, background=ACCENT, bordercolor=CARD,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

        s.configure("Card.TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10), focuscolor=CARD,
                    indicatorbackground=CARD2, indicatorforeground=ACCENT_DARK)
        s.map("Card.TCheckbutton", background=[("active", CARD)],
              indicatorbackground=[("selected", ACCENT), ("!selected", CARD2)],
              indicatorforeground=[("selected", ACCENT_DARK)])

    def make_step(self, col, n, title, btn_text, cmd):
        card = tk.Frame(self.steps_row, bg=CARD, padx=18, pady=16, highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        num = tk.Label(head, text=str(n), bg=ACCENT_SOFT, fg=ACCENT, font=(FONT, 10, "bold"), width=3, pady=3)
        num.pack(side="left")
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=(FONT, 13, "bold")).pack(side="left", padx=10)
        status = tk.Label(card, text="", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w")
        status.pack(fill="x", pady=(12, 14))
        btn = ttk.Button(card, text=btn_text, style="Ghost.TButton", command=cmd)
        btn.pack(fill="x")
        return {"n": n, "num": num, "status": status, "btn": btn}

    def set_conn(self, key, state, text):
        colors = {"idle": MUTED, "wait": AMBER, "ok": ACCENT, "bad": RED}
        card = self.steps[key]
        card["status"].config(text=f"\u25cf  {text}", fg=colors[state])
        card["num"].config(text="\u2713" if state == "ok" else str(card["n"]))
        self.refresh_state()

    def refresh_state(self):
        if not getattr(self, "_ready", False):  # UI still being built
            return
        # which step should the user do next?
        if not self.sp:
            current = "spotify"
            msg = "Step 1 \u2014 connect your Spotify account."
        elif not self.tidal:
            current = "tidal"
            msg = "Step 2 \u2014 connect your Tidal account."
        elif not self.loaded:
            current = "library"
            msg = "Step 3 \u2014 load your Spotify library."
        else:
            current = None
            msg = ("Tick what you want to move. Preview is on, so nothing changes on Tidal yet \u2014 "
                   "untick it when you're ready." if self.dry.get()
                   else "Ready. Tick what you want to move, then press the green button.")
        if self.busy:
            msg = "Working\u2026 you can press Stop at any time."
        elif self.loading:
            msg = "Loading your Spotify library \u2014 this can take a minute."
        self.hint.config(text=msg)
        for key, card in self.steps.items():
            card["btn"].config(style="Primary.TButton" if key == current else "Ghost.TButton")

        total = 0
        for kind, tab in self.tabs.items():
            total += len(tab.selected)
            if tab.items:
                self.nb.tab(tab, text=f"{KINDS[kind]['label']}  {len(tab.selected)}/{len(tab.items)}")
        dry = self.dry.get()
        if self.busy:
            self.btn_go.config(text="Working\u2026", state="disabled")
            return
        if not self.loaded:
            text = "Preview matches" if dry else "Transfer to Tidal"
        elif total == 0:
            text = "Nothing selected"
        else:
            text = f"Preview {total} items" if dry else f"Transfer {total} items to Tidal"
        self.btn_go.config(text=text, state="normal" if (self.loaded and total and self.tidal) else "disabled")

    # ----- thread plumbing -----
    def ui(self, fn, *args):
        self.q.put((fn, args))

    def poll(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def log(self, msg):
        low = msg.lower()
        tag = "err" if ("fail" in low or "error" in low) else (
            "ok" if msg.startswith(("Finished", "Spotify connected", "Tidal connected", "Loaded")) else "")
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n", tag if tag else ())
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # ----- connections -----
    def connect_spotify(self):
        if not (os.environ.get("SPOTIPY_CLIENT_ID") and os.environ.get("SPOTIPY_CLIENT_SECRET")):
            messagebox.showerror("Missing credentials", T.env_help())
            return
        self.set_conn("spotify", "wait", "Waiting for your browser\u2026")

        def work():
            try:
                sp = T.spotify_client()
                name = sp.current_user().get("display_name") or "your account"
                self.sp = sp
                self.ui(self.set_conn, "spotify", "ok", f"Connected as {name}")
                self.ui(self.steps["spotify"]["btn"].config, {"text": "Reconnect"})
                self.ui(self.log, f"Spotify connected as {name}.")
            except BaseException as e:
                self.ui(self.set_conn, "spotify", "bad", "Connection failed")
                self.ui(self.log, f"Spotify login failed: {e}")
        self.run_bg(work)

    def connect_tidal(self):
        self.set_conn("tidal", "wait", "Waiting for your browser\u2026")

        def work():
            try:
                s = tidalapi.Session()
                ok = False
                if T.TIDAL_SESSION_FILE.exists():
                    try:
                        ok = s.load_session_from_file(T.TIDAL_SESSION_FILE) and s.check_login()
                    except Exception:
                        ok = False
                if not ok:
                    login, future = s.login_oauth()
                    url = login.verification_uri_complete
                    if not url.startswith("http"):
                        url = "https://" + url
                    self.ui(self.log, f"Approve the Tidal login in your browser: {url}")
                    webbrowser.open(url)
                    future.result()
                    try:
                        s.save_session_to_file(T.TIDAL_SESSION_FILE)
                    except Exception:
                        pass
                if not s.check_login():
                    raise RuntimeError("login not completed")
                self.tidal = s
                self.ui(self.set_conn, "tidal", "ok", "Connected")
                self.ui(self.steps["tidal"]["btn"].config, {"text": "Reconnect"})
                self.ui(self.log, "Tidal connected.")
            except BaseException as e:
                self.ui(self.set_conn, "tidal", "bad", "Connection failed")
                self.ui(self.log, f"Tidal login failed: {e}")
        self.run_bg(work)

    # ----- loading -----
    def load_library(self):
        if not self.sp:
            messagebox.showinfo("Spotify", "Connect Spotify first (step 1).")
            return
        if self.loading:
            return
        self.loading = True
        self.set_conn("library", "wait", "Loading\u2026")
        self.steps["library"]["btn"].config(state="disabled")

        def work():
            counts = 0
            try:
                state = T.load_state()
                for kind, cfg in KINDS.items():
                    items = cfg["load"](self.sp)
                    self.ui(self.tabs[kind].load, items, state.get(kind, []))
                    self.ui(self.log, f"Loaded {len(items)} {cfg['label'].lower()}.")
                    counts += len(items)
                self.loaded = True
                self.ui(self.set_conn, "library", "ok", f"Loaded {counts} items")
                self.ui(self.steps["library"]["btn"].config, {"text": "Reload"})
                self.ui(self.log, "Tick what you want to move, then press the green button. "
                                  "Items already transferred are unticked.")
            except BaseException as e:
                self.ui(self.set_conn, "library", "bad", "Loading failed")
                self.ui(self.log, f"Loading failed: {e}")
            finally:
                self.loading = False
                self.ui(self.steps["library"]["btn"].config, {"state": "normal"})
                self.ui(self.refresh_state)
        self.run_bg(work)

    # ----- transfer -----
    def start_transfer(self):
        if self.busy:
            return
        if not self.tidal:
            messagebox.showinfo("Tidal", "Connect Tidal first (step 2).")
            return
        jobs = {k: t.selected_items() for k, t in self.tabs.items()}
        total = sum(len(v) for v in jobs.values())
        if not total:
            messagebox.showinfo("Nothing selected", "Load your library and tick some items first.")
            return
        dry = self.dry.get()
        if not dry and not messagebox.askyesno("Confirm", f"Add up to {total} items to your Tidal favorites?"):
            return
        self.busy = True
        self.stop.clear()
        self.btn_stop.config(state="normal")
        self.progress.config(maximum=total, value=0)
        self.refresh_state()
        self.run_bg(lambda: self.transfer_worker(jobs, total, dry))

    def transfer_worker(self, jobs, total, dry):
        state = T.load_state()
        fav = self.tidal.user.favorites
        unmatched, n, added = [], 0, 0
        existing = T.tidal_playlists_by_name(self.tidal) if jobs.get("playlists") else {}

        def tick():
            self.ui(self.progress.config, {"value": n})
            self.ui(self.lbl_prog.config, {"text": f"{n} / {total}"})

        try:
            for kind, items in jobs.items():
                cfg, tab = KINDS[kind], self.tabs[kind]
                if kind == "playlists":
                    for item in items:
                        if self.stop.is_set():
                            raise KeyboardInterrupt
                        n += 1
                        label = T.describe(kind, item)
                        url = item.get("external_urls", {}).get("spotify", "")
                        try:
                            self.ui(tab.set_status, item["id"], "working...")
                            self.ui(self.log, f"Playlist '{item['name']}': matching tracks...")
                            ok, tot, missing = T.transfer_playlist(self.sp, self.tidal, item, existing, dry)
                            unmatched.extend(missing)
                            self.ui(tab.set_status, item["id"], f"{'matched' if dry else 'added'} {ok}/{tot}")
                            if not dry:
                                state[kind].append(item["id"])
                            added += 1
                        except Exception as e:
                            self.ui(tab.set_status, item["id"], "error")
                            self.ui(self.log, f"Error on playlist {item['name']}: {e}")
                            unmatched.append((kind, label, url))
                        tick()
                    continue

                adder = getattr(fav, cfg["add"])
                # Lookups run in parallel and adds happen one by one in the original (oldest-first) order
                with closing(T.pipelined(items, cfg["match"], self.tidal)) as results:
                    for item, tid, err in results:
                        if self.stop.is_set():
                            raise KeyboardInterrupt
                        n += 1
                        label = T.describe(kind, item)
                        url = item.get("external_urls", {}).get("spotify", "")
                        try:
                            if err is not None:
                                raise err
                            if tid is None:
                                self.ui(tab.set_status, item["id"], "no match")
                                unmatched.append((kind, label, url))
                            elif dry:
                                self.ui(tab.set_status, item["id"], "matched")
                                added += 1
                            else:
                                T.retry(adder, tid)
                                if item["id"] not in state[kind]:
                                    state[kind].append(item["id"])
                                self.ui(tab.set_status, item["id"], "added")
                                added += 1
                        except Exception as e:
                            self.ui(tab.set_status, item["id"], "error")
                            self.ui(self.log, f"Error on {label}: {e}")
                            unmatched.append((kind, label, url))
                        tick()
                        if n % 25 == 0 and not dry:
                            T.save_state(state)
        except KeyboardInterrupt:
            self.ui(self.log, "Stopped.")
        finally:
            if not dry:
                T.save_state(state)
            if unmatched:
                with T.UNMATCHED_FILE.open("w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["type", "name", "spotify_url"])
                    w.writerows(unmatched)
            verb = "matched" if dry else "added"
            msg = f"Finished: {added} {verb}, {len(unmatched)} not transferred"
            if unmatched:
                msg += f" (see {T.UNMATCHED_FILE.name})"
            self.ui(self.log, msg + ".")
            self.ui(self.finish, msg + ".")

    def finish(self, msg):
        self.busy = False
        self.btn_stop.config(state="disabled")
        self.refresh_state()
        messagebox.showinfo("Done", msg)


if __name__ == "__main__":
    try:  # crisp text on high-DPI Windows screens
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
