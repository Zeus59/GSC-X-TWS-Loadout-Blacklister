#!/usr/bin/env python3
"""
GSC X TWS Loadout Blacklister

Builds the blacklist for the Loadout Editor: every item prefab from all installed mods that is NOT listed in any arsenal catalog.

Start: double-click Start.bat (it installs customtkinter on first run).

How it works
  1. Reads every mod in the addons folder (data.pak = file index + prefabs, resourceDatabase.rdb = path -> GUID).
  2. Reads Configs/EntityCatalog/**.conf of the selected arsenal mods and collects every prefab in them
     (including lists in other mods that a catalog references as {GUID}path.conf) = allowed GUIDs.
  3. A prefab counts as an item if it, or a parent prefab, has an InventoryItemComponent. Everything else
     (vehicles, buildings, characters) is never blocked.
  4. Items whose GUID is not allowed are written as "{GUID}Path # Mod" to the output file.

Copy the output to the server: <profile>/LoadoutEditor/native_blocked_auto.txt  (takes effect after a restart).
The mods are only read, never changed. The window remembers its settings in Blacklister.json.
"""
import collections, json, os, queue, re, struct, threading, zlib
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "Blacklister.json")
DEFAULT_ADDONS = os.path.join(os.path.expanduser("~"), "Documents", "My Games", "ArmaReforger", "addons")
DEFAULT_OUT = os.path.join(HERE, "native_blocked_auto.txt")
DEFAULT_KEEP = ["radio"]
ITEM_PATH_HINTS = ("/weapons/", "/items/", "/headgear/", "/vest", "/backpack", "/boots", "/pants", "/jacket", "/gloves",
                   "/magazines/", "/attachments/", "/equipment/", "/grenades/", "/ammo", "/clothes", "/gear/")
# Prefabs that never sit in a loadout (projectiles, tests, vehicles, editor props): kept out of the list
SKIP = re.compile(r"/vehicles/|prefabseditable|/tests/|/props/|/weapons/ammo/|/ammo/(spall|penetrators)|/munitions/|/warheads/|/aircraftweapons/|/mrls/", re.I)
ROW_H, HEAD_H = 34, 36                 # table row and header height
ROW_A, ROW_B = "#2b2b2b", "#313131"    # alternating row colors
COL_ARSENAL, COL_TRUST = 140, 160      # width of the two checkbox columns
REF = re.compile(r'"\{([0-9A-Fa-f]{16})\}([^"]*\.(?:et|conf))"')
PARENT = re.compile(r'^\s*\w+\s*:\s*"\{([0-9A-Fa-f]{16})\}([^"]*)"', re.M)


# ----------------------------------------------------------------------------------------------------------------------
# File formats
# ----------------------------------------------------------------------------------------------------------------------
def list_pak(path):
    """File index of a PAC1 archive: {path: (offset, compressed, original)}. Reads only the FILE chunk."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"FORM" or head[8:12] != b"PAC1":
            return {}
        end = os.path.getsize(path)
        p, tree = 12, None
        while p + 8 <= end:
            f.seek(p)
            tag, sz = f.read(4), struct.unpack(">I", f.read(4))[0]
            if tag == b"FILE":
                tree = f.read(sz)
                break
            p += 8 + sz
    if tree is None:
        return {}
    pos, files = 0, {}

    def node(prefix):
        nonlocal pos
        t, ln = tree[pos], tree[pos + 1]
        name = tree[pos + 2:pos + 2 + ln].decode("utf-8", "replace")
        pos += 2 + ln
        if t == 0:
            n = struct.unpack("<I", tree[pos:pos + 4])[0]
            pos += 4
            for _ in range(n):
                node(prefix + name + "/")
        else:
            off, csz, sz = struct.unpack("<III", tree[pos:pos + 12])
            pos += 24
            files[(prefix + name).lstrip("/")] = (off, csz, sz)

    node("")
    return files


def read_entry(path, entry):
    off, csz, sz = entry
    with open(path, "rb") as f:
        f.seek(off)
        raw = f.read(csz)
    if csz != sz:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    return raw


def read_rdb(path):
    """{path: GUID} for all .et/.conf. Entry: u32 length, path, NUL, 6 bytes flags, u64 GUID, u64 timestamp."""
    d = open(path, "rb").read()
    out = {}
    for m in re.finditer(rb"[\x20-\x7e]{3,400}\.(?:et|conf)", d):
        for back in range(0, 400):
            ls = m.start() - back - 4
            if ls < 0:
                break
            if struct.unpack("<I", d[ls:ls + 4])[0] == m.end() - (m.start() - back) + 1 and d[m.end():m.end() + 1] == b"\x00":
                guid = struct.unpack("<Q", d[m.end() + 7:m.end() + 15])[0]
                out[d[m.start() - back:m.end()].decode()] = "%016X" % guid
                break
    return out


def mod_folders(addons):
    """All mod folders that have a data.pak and a resourceDatabase.rdb."""
    if not os.path.isdir(addons):
        return []
    return [d for d in sorted(os.listdir(addons))
            if os.path.isfile(os.path.join(addons, d, "data.pak")) and os.path.isfile(os.path.join(addons, d, "resourceDatabase.rdb"))]


# ----------------------------------------------------------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------------------------------------------------------
def generate(addons, arsenal, out, keep, trust=(), log=print):
    """arsenal: folder names of the arsenal mods, keep: list of regex, trust: mods whose items are always allowed (baseline).
    Returns the number of blocked items."""
    names = mod_folders(addons)
    if not names:
        raise ValueError("No mods with a data.pak were found in the addons folder:\n" + addons)
    unknown = [m for m in arsenal if m not in names]
    if unknown or not arsenal:
        raise ValueError("No arsenal mod selected or not found: %s" % (", ".join(unknown) or "(none selected)"))
    try:
        keep_re = [re.compile(k, re.I) for k in keep]
    except re.error as e:
        raise ValueError("Invalid regex under 'Never block': %s" % e)

    mods = {}
    for d in names:
        mods[d] = (os.path.join(addons, d, "data.pak"), list_pak(os.path.join(addons, d, "data.pak")),
                   read_rdb(os.path.join(addons, d, "resourceDatabase.rdb")))
    log("Mods read: %d" % len(mods))

    # GUID -> (path, mod) over all mods; path -> (mod, entry) for reading .conf files
    by_guid, by_path = {}, {}
    for mod, (pak, files, rdb) in mods.items():
        for path, guid in rdb.items():
            by_guid[guid] = (path, mod)
            if path in files:
                by_path.setdefault(path, (mod, files[path]))

    # allowed GUIDs from the arsenal catalogs (including references to further .conf files in any mod)
    allowed, seen, todo = set(), set(), []
    for mod in arsenal:
        todo += [(mod, p) for p in mods[mod][1] if p.startswith("Configs/EntityCatalog/") and p.endswith(".conf")]
    if not todo:
        raise ValueError("The selected arsenal mods contain no catalogs (Configs/EntityCatalog).")
    while todo:
        mod, path = todo.pop()
        if (mod, path) in seen:
            continue
        seen.add((mod, path))
        text = read_entry(mods[mod][0], mods[mod][1][path]).decode("utf-8", "replace")
        for guid, ref in REF.findall(text):
            if ref.endswith(".et"):
                allowed.add(guid.upper())
            elif ref in by_path:
                todo.append((by_path[ref][0], ref))
    log("Allowed prefabs from the arsenal lists: %d" % len(allowed))

    # item detection through inheritance
    texts = {}
    for mod, (pak, files, rdb) in mods.items():
        for path, guid in rdb.items():
            if path.endswith(".et") and path in files:
                texts[guid] = read_entry(pak, files[path]).decode("utf-8", "replace")
    memo = {}

    def is_item(guid, depth=0):
        if guid in memo:
            return memo[guid]
        memo[guid] = False   # guard against cycles
        text, res = texts.get(guid), False
        if text is not None:
            if "InventoryItemComponent" in text:
                res = True
            else:
                m = PARENT.search(text)
                if m and depth < 30:
                    pg, pp = m.group(1).upper(), m.group(2)
                    if pg in texts:
                        res = is_item(pg, depth + 1)
                    else:   # parent is in the base game: judge by its path only
                        res = any(h in "/" + pp.lower() for h in ITEM_PATH_HINTS)
        memo[guid] = res
        return res

    blocked, kept, items, trusted = [], 0, 0, 0
    for guid, (path, mod) in by_guid.items():
        if not path.endswith(".et") or SKIP.search(path) or not is_item(guid):
            continue
        items += 1
        if guid in allowed:
            continue
        if mod in trust:
            trusted += 1
            continue
        if any(k.search(path) for k in keep_re):
            kept += 1
            continue
        blocked.append((mod, path, guid))
    blocked.sort()

    per_mod = collections.Counter(m for m, _, _ in blocked)
    log("Items in the mods: %d" % items)
    log("Spared by allowed mods: %d (%s)" % (trusted, ", ".join(trust) or "none"))
    log("Spared by 'Never block': %d" % kept)
    log("Blocked: %d" % len(blocked))
    for m, n in per_mod.most_common(15):
        log("   %4d  %s" % (n, m))

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# Generated by the GSC X TWS Loadout Blacklister: items that are in no arsenal list.\n")
        for mod, path, guid in blocked:
            f.write("{%s}%s # %s\n" % (guid, path, mod))
    log("Written: " + os.path.abspath(out))
    return len(blocked)


# ----------------------------------------------------------------------------------------------------------------------
# Window
# ----------------------------------------------------------------------------------------------------------------------
def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("GSC X TWS Loadout Blacklister")
        self.geometry("820x880")
        self.minsize(700, 720)
        cfg = load_settings()
        self.queue = queue.Queue()
        self.checks = {}   # mod folder -> bool (arsenal mod: read its lists)
        self.trust = {}    # mod folder -> bool (allow the whole mod)
        self.addons_var = ctk.StringVar(value=cfg.get("addons", DEFAULT_ADDONS))
        self.out_var = ctk.StringVar(value=cfg.get("out", DEFAULT_OUT))
        self.saved_arsenal = cfg.get("arsenal")
        self.saved_trust = cfg.get("trust", [])

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(7, weight=2)

        title = ctk.CTkFrame(self, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(title, text="GSC X TWS Loadout Blacklister", font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title, text="Blocks every mod item that is not in any arsenal list.", text_color="gray").pack(anchor="w")

        self.row_picker(1, "Addons folder", self.addons_var, "Browse", self.pick_dir)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 0))
        head.columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="Mods", font=ctk.CTkFont(weight="bold"), width=110, anchor="w").grid(row=0, column=0, sticky="w")
        self.search = ctk.CTkEntry(head, placeholder_text="Search mods ...")
        self.search.grid(row=0, column=1, sticky="ew", padx=8)
        self.search.bind("<KeyRelease>", lambda _e: self.show_mods())
        ctk.CTkLabel(head, justify="left", text_color="gray", anchor="w", wraplength=760,
                     text="Click a checkbox to switch it. Blue = \"Arsenal lists\": read this mod's catalogs as the allowed list. "
                          "Green = \"Allow whole mod\": never block any item of this mod (baseline).").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # One single canvas instead of hundreds of widgets: the rows are drawn, so it stays fast with thousands of mods
        holder = ctk.CTkFrame(self)
        holder.grid(row=3, column=0, sticky="nsew", padx=20, pady=6)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(1, weight=1)
        self.visible = []   # mod folders currently shown (after the search filter)
        self.head = tk.Canvas(holder, height=HEAD_H, bg="#1f538d", highlightthickness=0)
        self.head.grid(row=0, column=0, sticky="ew")
        self.body = tk.Canvas(holder, bg=ROW_A, highlightthickness=0, yscrollincrement=10)
        self.body.grid(row=1, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(holder, command=self.body.yview)
        scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.body.configure(yscrollcommand=scroll.set)
        self.body.bind("<Configure>", lambda _e: self.draw())
        self.body.bind("<Button-1>", self.toggle)
        self.body.bind("<MouseWheel>", lambda e: self.body.yview_scroll(int(-e.delta / 120) * 3, "units"))

        self.row_picker(4, "Output file", self.out_var, "Save as", self.pick_out)

        ctk.CTkLabel(self, text="Never block (regex on the prefab path, one per line)", font=ctk.CTkFont(weight="bold")).grid(
            row=5, column=0, sticky="w", padx=20, pady=(10, 0))
        self.keep_box = ctk.CTkTextbox(self, height=70)
        self.keep_box.grid(row=6, column=0, sticky="ew", padx=20, pady=6)
        self.keep_box.insert("1.0", "\n".join(cfg.get("keep", DEFAULT_KEEP)))

        self.log_box = ctk.CTkTextbox(self, state="disabled")
        self.log_box.grid(row=7, column=0, sticky="nsew", padx=20, pady=6)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=8, column=0, sticky="ew", padx=20, pady=(6, 16))
        bottom.columnconfigure(1, weight=1)
        self.button = ctk.CTkButton(bottom, text="Generate list", height=38, command=self.start)
        self.button.grid(row=0, column=0)
        self.progress = ctk.CTkProgressBar(bottom, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=16)
        self.progress.grid_remove()   # only visible while running

        self.refresh_mods()
        self.after(100, self.poll)

    def row_picker(self, row, label, var, button_text, command):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=20, pady=(12, 0) if row == 1 else (6, 0))
        frame.columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=label, width=110, anchor="w", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0)
        ctk.CTkEntry(frame, textvariable=var).grid(row=0, column=1, sticky="ew", padx=8)
        ctk.CTkButton(frame, text=button_text, width=130, command=command).grid(row=0, column=2)

    # --- Mods -------------------------------------------------------------------------------------------------------
    def refresh_mods(self):
        names = mod_folders(self.addons_var.get())
        old, old_trust = self.checks, self.trust
        self.checks, self.trust = {}, {}
        for n in names:
            if n in old:
                chosen, trusted = old[n], old_trust.get(n, False)
            elif self.saved_arsenal is not None:
                chosen, trusted = n in self.saved_arsenal, n in self.saved_trust
            else:
                chosen, trusted = "arsenal" in n.lower(), False
            self.checks[n] = chosen
            self.trust[n] = trusted
        self.saved_arsenal = None
        self.show_mods()
        self.write("%d mods found." % len(names) if names else "No mods found in the addons folder.")

    def show_mods(self):
        text = self.search.get().strip().lower()
        self.visible = [n for n, chosen in self.checks.items() if not text or text in n.lower() or chosen or self.trust[n]]
        self.body.yview_moveto(0)
        self.draw()

    def draw(self):
        """Draws header and rows. Column centers sit at fixed distances from the right edge."""
        c, h = self.body, self.head
        w = c.winfo_width()
        arsenal_x, trust_x = w - COL_TRUST - COL_ARSENAL // 2 - 20, w - COL_TRUST // 2 - 10
        h.delete("all")
        h.create_text(14, HEAD_H // 2, text="Mod", anchor="w", fill="white", font=("Segoe UI", 10, "bold"))
        h.create_text(arsenal_x, HEAD_H // 2, text="Arsenal lists", fill="white", font=("Segoe UI", 10, "bold"))
        h.create_text(trust_x, HEAD_H // 2, text="Allow whole mod", fill="white", font=("Segoe UI", 10, "bold"))
        c.delete("all")
        for i, name in enumerate(self.visible):
            y = i * ROW_H
            c.create_rectangle(0, y, w, y + ROW_H, fill=ROW_A if i % 2 == 0 else ROW_B, width=0)
            c.create_text(14, y + ROW_H // 2, text=name, anchor="w", fill="#dce4ee", font=("Segoe UI", 10))
            self.draw_box(arsenal_x, y + ROW_H // 2, self.checks[name], "#1f6aa5")
            self.draw_box(trust_x, y + ROW_H // 2, self.trust[name], "#2e9e5b")
        c.configure(scrollregion=(0, 0, w, max(len(self.visible) * ROW_H, c.winfo_height())))

    def draw_box(self, cx, cy, on, color):
        r = 11
        if on:
            self.body.create_rectangle(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color)
            self.body.create_line(cx - 6, cy, cx - 2, cy + 5, cx + 6, cy - 5, fill="white", width=3, capstyle="round", joinstyle="round")
        else:
            self.body.create_rectangle(cx - r, cy - r, cx + r, cy + r, fill="#1b1b1b", outline="#8d939c", width=2)

    def toggle(self, event):
        """Click on a checkbox: the left box reads this mod's arsenal lists, the right box allows the whole mod."""
        i = int(self.body.canvasy(event.y) // ROW_H)
        if not 0 <= i < len(self.visible):
            return
        name, w = self.visible[i], self.body.winfo_width()
        if event.x > w - COL_TRUST - 10:
            self.trust[name] = not self.trust[name]
        elif event.x > w - COL_TRUST - COL_ARSENAL - 20:
            self.checks[name] = not self.checks[name]
        else:
            return
        self.draw()

    # --- File pickers -----------------------------------------------------------------------------------------------
    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.addons_var.get() or None, title="Folder with the mods (addons)")
        if d:
            self.addons_var.set(d)
            self.refresh_mods()

    def pick_out(self):
        f = filedialog.asksaveasfilename(initialfile=os.path.basename(self.out_var.get()), initialdir=os.path.dirname(self.out_var.get()) or None,
                                         defaultextension=".txt", filetypes=[("Text file", "*.txt")])
        if f:
            self.out_var.set(f)

    # --- Run --------------------------------------------------------------------------------------------------------
    def write(self, line):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start(self):
        arsenal = [n for n, v in self.checks.items() if v]
        trust = [n for n, v in self.trust.items() if v]
        keep = [k.strip() for k in self.keep_box.get("1.0", "end").splitlines() if k.strip()]
        save_settings({"addons": self.addons_var.get(), "out": self.out_var.get(), "keep": keep, "arsenal": arsenal, "trust": trust})
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.write("Reading mods, this takes about a minute ...")
        self.button.configure(state="disabled")
        self.progress.grid()
        self.progress.start()
        threading.Thread(target=self.work, args=(self.addons_var.get(), arsenal, self.out_var.get(), keep, trust), daemon=True).start()

    def work(self, addons, arsenal, out, keep, trust):
        try:
            generate(addons, arsenal, out, keep, trust, log=lambda s: self.queue.put(("log", s)))
            self.queue.put(("done", None))
        except Exception as e:   # errors should show in the window, not end the program
            self.queue.put(("err", str(e)))

    def poll(self):
        try:
            while True:
                kind, val = self.queue.get_nowait()
                if kind == "log":
                    self.write(val)
                    continue
                self.progress.stop()
                self.progress.grid_remove()
                self.button.configure(state="normal")
                if kind == "err":
                    messagebox.showerror("Error", val)
                else:
                    messagebox.showinfo("Done", "The list has been written.\n\nCopy it to the server:\n$profile:LoadoutEditor/native_blocked_auto.txt\nand restart the server.")
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()
