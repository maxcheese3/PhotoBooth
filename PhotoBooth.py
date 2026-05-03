"""
Photo Booth Application
-----------------------
Captures images from a webcam, overlays an optional caption, and sends
the result directly to the default Windows printer via the GDI API.

Requirements (install with pip):
    pip install opencv-python pillow pywin32 numpy

Run:
    python PhotoBooth.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import os
import time
import winsound
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageTk, ImageDraw, ImageFont

import win32print
import win32ui
import win32con
import win32gui
from PIL import ImageWin


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FONTS = {
    "Arial Bold":      "arialbd.ttf",
    "Comic Sans":      "comic.ttf",
    "Courier New":     "cour.ttf",
    "Georgia":         "georgia.ttf",
    "Freestyle":       "freescpt.ttf",
    "Impact":          "impact.ttf",
    "Ink Free":        "Inkfree.ttf",
    "Segoe Script":    "segoesc.ttf",
    "Times New Roman": "times.ttf",
    "Verdana":         "verdana.ttf",
}

CAPTION_POSITIONS = [
    "Bottom Centre", "Bottom Left", "Bottom Right",
    "Top Centre",    "Top Left",    "Top Right",
    "Centre",
]

FONT_SIZES        = [32, 40, 48, 60, 72, 90, 112, 132]
WINDOWS_FONTS_DIR = r"C:\Windows\Fonts"
COUNTDOWN_SECONDS = 3
BURST_COUNT       = 3
BURST_DELAY       = 2
MIN_W, MIN_H      = 900, 700

EFFECTS = [
    ("None",         "none"),
    ("Mirror Left",  "mirror_left"),
    ("Mirror Right", "mirror_right"),
    ("Kaleidoscope", "kaleidoscope"),
    ("Fisheye",      "fisheye"),
    ("Bulge",        "bulge"),
    ("Pinch",        "pinch"),
    ("Twist",        "twist"),
    ("Dent",         "dent"),
]
EFFECT_NAMES = [e[0] for e in EFFECTS]
EFFECT_KEYS  = {e[0]: e[1] for e in EFFECTS}

# Theme palettes
DARK  = dict(bg="#1e1e2e", card="#313244", fg="#cdd6f4",
             muted="#585b70", ib="#45475a", accent="#cba6f7",
             status_bg="#181825")
LIGHT = dict(bg="#ffffff", card="#f2f2f2", fg="#1e1e2e",
             muted="#888888", ib="#e0e0e0", accent="#7c3aed",
             status_bg="#e8e8f0")


# ---------------------------------------------------------------------------
# Sound helpers
# ---------------------------------------------------------------------------

def _play_beep():    winsound.Beep(880,  120)
def _play_capture(): winsound.Beep(1400, 80);  winsound.Beep(1800, 120)
def play_async(fn):  threading.Thread(target=fn, daemon=True).start()


# ---------------------------------------------------------------------------
# Image adjustments
# ---------------------------------------------------------------------------

def apply_adjustments(bgr, brightness, contrast, exposure, shadows):
    img = bgr.astype(np.float32)
    img *= 2.0 ** (exposure / 50.0)
    img += brightness * 2.55
    img  = (img - 128) * ((contrast + 100) / 100.0) + 128
    if shadows != 0:
        norm = np.clip(img / 255.0, 0, 1)
        mask = 1.0 - np.clip(norm * 2.0, 0, 1)
        img += mask * (shadows / 100.0) * 80
    return np.clip(img, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Camera effects
# ---------------------------------------------------------------------------

def _remap(bgr, mx, my):
    return cv2.remap(bgr, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

def apply_effect(bgr, key):
    if key == "none" or bgr is None: return bgr
    h, w = bgr.shape[:2]; cx, cy = w/2.0, h/2.0
    if key == "mirror_left":
        l = bgr[:, :w//2]; return np.hstack([l, cv2.flip(l, 1)])
    if key == "mirror_right":
        r = bgr[:, w//2:]; return np.hstack([cv2.flip(r,1), r])
    if key == "kaleidoscope":
        tl  = bgr[:h//2,:w//2]; top = np.hstack([tl, cv2.flip(tl,1)])
        return np.vstack([top, cv2.flip(top,0)])
    ys,xs = np.mgrid[0:h,0:w].astype(np.float32)
    dx,dy = xs-cx, ys-cy; r = np.sqrt(dx**2+dy**2); theta=np.arctan2(dy,dx)
    if key=="fisheye":
        mr=min(cx,cy); rn=np.clip(r/mr,0,1)
        return _remap(bgr,(cx+mr*(rn**1.6)*np.cos(theta)).astype(np.float32),
                          (cy+mr*(rn**1.6)*np.sin(theta)).astype(np.float32))
    if key=="bulge":
        mr=min(cx,cy); rn=np.clip(r/mr,0,1)
        return _remap(bgr,(cx+mr*np.sqrt(rn)*np.cos(theta)).astype(np.float32),
                          (cy+mr*np.sqrt(rn)*np.sin(theta)).astype(np.float32))
    if key=="pinch":
        mr=min(cx,cy); rn=np.clip(r/mr,0,1)
        return _remap(bgr,(cx+mr*(rn**2)*np.cos(theta)).astype(np.float32),
                          (cy+mr*(rn**2)*np.sin(theta)).astype(np.float32))
    if key=="twist":
        mr=min(cx,cy); a=2.5*(1-np.clip(r/mr,0,1)); t2=theta+a; rc=np.clip(r,0,mr)
        return _remap(bgr,(cx+rc*np.cos(t2)).astype(np.float32),
                          (cy+rc*np.sin(t2)).astype(np.float32))
    if key=="dent":
        mx2=np.mgrid[0:h,0:w][1].astype(np.float32)
        my2=(np.mgrid[0:h,0:w][0]+h*0.06*np.sin(mx2/w*6*np.pi)).astype(np.float32)
        return _remap(bgr,mx2,my2)
    return bgr


# ---------------------------------------------------------------------------
# Image overlays
# ---------------------------------------------------------------------------

def _load_pil_font(fname, size):
    try:    return ImageFont.truetype(os.path.join(WINDOWS_FONTS_DIR, fname), size)
    except: return ImageFont.load_default()

def _apply_caption(pil_img, text, font_name, font_size, position):
    if not text.strip(): return pil_img
    img  = pil_img.copy().convert("RGBA"); w,h = img.size
    font = _load_pil_font(FONTS.get(font_name,"arial.ttf"), font_size)
    draw = ImageDraw.Draw(img)
    bbox = draw.multiline_textbbox((0,0), text, font=font, align="center")
    tw,th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    pad=12; pos=position.lower()
    tx = pad if "left" in pos else (w-tw-pad if "right" in pos else (w-tw)//2)
    ty = pad if "top"  in pos else (h-th-pad*4 if "bottom" in pos else (h-th)//2)
    stroke = int(min(font_size/6, 8))
    draw.multiline_text((tx,ty), text, font=font, fill=(255,255,255,255),
                         stroke_width=stroke, stroke_fill=(0,0,0,200), align="center")
    return img.convert("RGB")

def _apply_timestamp(pil_img, font_size=22):
    img=pil_img.copy().convert("RGBA"); w,h=img.size
    font=_load_pil_font("arialbd.ttf", font_size)
    ts=datetime.now().strftime("%y-%m-%d  %H:%M:%S")
    draw=ImageDraw.Draw(img)
    bbox=draw.textbbox((0,0),ts,font=font); tw,th=bbox[2]-bbox[0],bbox[3]-bbox[1]
    pad=10
    draw.text((w-tw-pad,h-th-pad),ts,font=font,fill=(255,255,255,255),
               stroke_width=2,stroke_fill=(0,0,0,200))
    return img.convert("RGB")

def _apply_countdown(pil_img, number):
    img=pil_img.copy().convert("RGBA"); w,h=img.size; cx,cy=w//2,h//2
    fsz=int(h*0.52)
    try:    font=ImageFont.truetype(os.path.join(WINDOWS_FONTS_DIR,"impact.ttf"),fsz)
    except: font=ImageFont.load_default()
    text=str(number); dummy=ImageDraw.Draw(img)
    bbox=dummy.textbbox((cx,cy),text,font=font,anchor="mm")
    r_pad=max(bbox[2]-bbox[0],bbox[3]-bbox[1])//2+36
    ov=Image.new("RGBA",img.size,(0,0,0,0))
    ImageDraw.Draw(ov).ellipse([cx-r_pad,cy-r_pad,cx+r_pad,cy+r_pad],fill=(0,0,0,150))
    img=Image.alpha_composite(img,ov)
    stroke=int(min(fsz/6,14))
    ImageDraw.Draw(img).text((cx,cy),text,font=font,anchor="mm",
                              fill=(255,255,255,255),stroke_width=stroke,stroke_fill=(0,0,0,220))
    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Printer
# ---------------------------------------------------------------------------

def print_image(pil_image, printer_name, landscape=True):
    """Rotate for landscape (default) or keep upright for portrait."""
    if landscape:
        pil_image = pil_image.rotate(-90, expand=True)
    hprinter = win32print.OpenPrinter(printer_name)
    hdc = None
    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        px,py = hdc.GetDeviceCaps(win32con.HORZRES), hdc.GetDeviceCaps(win32con.VERTRES)
        iw,ih = pil_image.size
        scale  = min(px/iw, py/ih)
        hdc.StartDoc("Photo Booth"); hdc.StartPage()
        ImageWin.Dib(pil_image).draw(hdc.GetHandleOutput(),(0,0,int(iw*scale),int(ih*scale)))
        hdc.EndPage(); hdc.EndDoc(); hdc.DeleteDC()
    finally:
        win32print.ClosePrinter(hprinter)


# ---------------------------------------------------------------------------
# Background camera thread
# ---------------------------------------------------------------------------

class CameraThread(threading.Thread):
    def __init__(self, cap, q):
        super().__init__(daemon=True); self.cap=cap; self.q=q
        self._stop=threading.Event()
    def run(self):
        while not self._stop.is_set():
            ret,frame=self.cap.read()
            if ret:
                while not self.q.empty():
                    try: self.q.get_nowait()
                    except queue.Empty: break
                self.q.put(frame)
            time.sleep(0.01)
    def stop(self): self._stop.set()


# ---------------------------------------------------------------------------
# Settings flyout
# ---------------------------------------------------------------------------

class SettingsFlyout(tk.Toplevel):

    def __init__(self, app: "WebcamPrintApp"):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.transient(app.root)
        self._build()
        self._reposition()

    def _reposition(self):
        self.update_idletasks()
        rx = self.app.root.winfo_rootx() + self.app.root.winfo_width() + 4
        ry = self.app.root.winfo_rooty()
        sw = self.app.root.winfo_screenwidth()
        # If no room on right, open to the left
        if rx + self.winfo_width() > sw:
            rx = self.app.root.winfo_rootx() - self.winfo_width() - 4
        self.geometry(f"+{rx}+{ry}")

    def _build(self):
        app   = self.app
        pal   = app._pal()
        BG    = pal["bg"]; FG = pal["fg"]; MUTED = pal["muted"]
        IB    = pal["ib"]; ACCENT = pal["accent"]

        self.configure(bg=BG)

        lbl_cfg = dict(font=("Segoe UI", 9), fg=MUTED, bg=BG, anchor="w")
        chk_cfg = dict(bg=BG, activebackground=BG, selectcolor=pal["card"],
                       font=("Segoe UI", 10), cursor="hand2", anchor="w", fg=FG)

        def section(text):
            tk.Label(self, text=text, font=("Segoe UI", 9, "bold"),
                     fg=ACCENT, bg=BG, anchor="w").pack(fill="x", padx=12, pady=(10,2))
            tk.Frame(self, bg=MUTED, height=1).pack(fill="x", padx=12)

        def combo_row(parent, label, var, values, width=22):
            r = tk.Frame(parent, bg=BG)
            r.pack(fill="x", padx=12, pady=3)
            tk.Label(r, text=label, width=12, **lbl_cfg).pack(side="left")
            cmb = ttk.Combobox(r, textvariable=var, values=values,
                                state="readonly", width=width, font=("Segoe UI", 10))
            cmb.pack(side="left")
            return cmb

        # ── Camera ───────────────────────────────────────────────────
        section("Camera")
        combo_row(self, "Device", app.camera_index_var, app.camera_labels
                  ).bind("<<ComboboxSelected>>", lambda _: app._on_camera_change())
        tk.Checkbutton(self, text="🪞  Mirror preview",
                        variable=app.mirror_enabled,
                        command=app._refresh_live_preview,
                        **chk_cfg).pack(fill="x", padx=12, pady=2)

        # ── Printer ───────────────────────────────────────────────────
        section("Printer")
        combo_row(self, "Printer", app.printer_var, app.printer_list)

        tk.Checkbutton(self, text="🖨  Landscape (rotate 90°)",
                        variable=app.landscape_enabled,
                        **chk_cfg).pack(fill="x", padx=12, pady=2)

        tk.Checkbutton(self, text="💾  Save photos to disk",
                        variable=app.save_enabled,
                        **chk_cfg).pack(fill="x", padx=12, pady=2)

        # ── Sound ────────────────────────────────────────────────────
        section("Sound")
        tk.Checkbutton(self, text="🔔  Beep sounds enabled",
                        variable=app.sounds_enabled,
                        **chk_cfg).pack(fill="x", padx=12, pady=2)

        # ── Image Adjustments ────────────────────────────────────────
        section("Image Adjustments")

        def adj_row(label, var):
            r = tk.Frame(self, bg=BG)
            r.pack(fill="x", padx=12, pady=1)
            tk.Label(r, text=label, width=11, **lbl_cfg).pack(side="left")
            tk.Scale(r, variable=var, from_=-100, to=100, orient="horizontal",
                      length=160, bg=BG, fg=FG, troughcolor=IB,
                      highlightthickness=0, bd=0, showvalue=False, resolution=1,
                      command=lambda _: app._refresh_live_preview()
                      ).pack(side="left")
            tk.Label(r, textvariable=var, width=4,
                      font=("Segoe UI",8), fg=MUTED, bg=BG).pack(side="left")
            tk.Button(r, text="↺", font=("Segoe UI",8), fg=MUTED, bg=BG,
                       relief="flat", cursor="hand2", bd=0,
                       command=lambda v=var: (v.set(0), app._refresh_live_preview())
                       ).pack(side="left", padx=(2,0))

        adj_row("Brightness", app.adj_brightness)
        adj_row("Contrast",   app.adj_contrast)
        adj_row("Exposure",   app.adj_exposure)
        adj_row("Shadows",    app.adj_shadows)

        tk.Button(self, text="Reset All", font=("Segoe UI",8,"bold"),
                   fg=MUTED, bg=IB, relief="flat", cursor="hand2", bd=0,
                   padx=6, pady=3,
                   command=lambda: (
                       [v.set(0) for v in (app.adj_brightness, app.adj_contrast,
                                            app.adj_exposure,   app.adj_shadows)],
                       app._refresh_live_preview()
                   )).pack(anchor="e", padx=12, pady=(4,12))

    def _on_close(self):
        self.app._settings_flyout = None
        self.app.btn_settings.config(relief="flat")
        self.destroy()

    def recolour(self):
        for w in self.winfo_children():
            w.destroy()
        self._build()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class WebcamPrintApp:

    def _pal(self):
        return LIGHT if self.flash_enabled.get() else DARK

    # Convenience shorthands used by SettingsFlyout
    def _bg(self):     return self._pal()["bg"]
    def _card(self):   return self._pal()["card"]
    def _fg(self):     return self._pal()["fg"]
    def _muted(self):  return self._pal()["muted"]
    def _ib(self):     return self._pal()["ib"]
    def _accent(self): return self._pal()["accent"]

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Booth")
        self.root.minsize(MIN_W, MIN_H)
        self.root.resizable(True, True)
        self.root.configure(bg=DARK["bg"])

        # Camera
        self.cap             = None
        self._cam_thread     = None
        self._frame_queue    = queue.Queue(maxsize=2)
        self.running         = False
        self.last_frame      = None
        self.frozen_frame    = None
        self.preview_frozen  = False
        self._after_id       = None
        self._settings_flyout = None

        # Sequence
        self._sequence_active   = False
        self._burst_remaining   = 0
        self._sequence_after    = None
        self._countdown_overlay = None

        # ── tk.Vars ───────────────────────────────────────────────────
        self.caption_enabled   = tk.BooleanVar(value=True)
        self.caption_font      = tk.StringVar(value="Impact")
        self.caption_size      = tk.IntVar(value=72)
        self.caption_position  = tk.StringVar(value="Bottom Centre")

        self.countdown_enabled = tk.BooleanVar(value=False)
        self.burst_enabled     = tk.BooleanVar(value=False)

        self.effect_name       = tk.StringVar(value="None")
        self.mirror_enabled    = tk.BooleanVar(value=True)
        self.grayscale_enabled = tk.BooleanVar(value=False)
        self.timestamp_enabled = tk.BooleanVar(value=False)
        self.flash_enabled     = tk.BooleanVar(value=False)

        self.landscape_enabled = tk.BooleanVar(value=True)
        self.save_enabled      = tk.BooleanVar(value=True)
        self.sounds_enabled    = tk.BooleanVar(value=True)

        self.adj_brightness    = tk.DoubleVar(value=0)
        self.adj_contrast      = tk.DoubleVar(value=0)
        self.adj_exposure      = tk.DoubleVar(value=0)
        self.adj_shadows       = tk.DoubleVar(value=0)

        # Camera & printer lists
        self.camera_labels    = self._enumerate_cameras()
        self.camera_index_var = tk.StringVar(
            value=self.camera_labels[0] if self.camera_labels else "Camera 0")
        self.printer_list = self._enumerate_printers()
        try:    default_printer = win32print.GetDefaultPrinter()
        except: default_printer = self.printer_list[0] if self.printer_list else ""
        self.printer_var = tk.StringVar(value=default_printer)

        # Keep track of all themed widgets for flash recolouring
        self._themed_widgets = []   # list of (widget, role) tuples

        self._build_ui()
        self._start_camera()

        self.root.bind("<space>",  self._on_space)
        self.root.bind("<F11>",    self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)
        self._fullscreen = False
        self.root.bind("<Configure>", lambda _: self._refresh_live_preview())

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def _enumerate_cameras(self):
        labels = []
        for i in range(6):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                labels.append(f"Camera {i}")
                cap.release()
        return labels or ["Camera 0"]

    def _enumerate_printers(self):
        try:
            printers = win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
            return [p[2] for p in printers]
        except Exception:
            try:    return [win32print.GetDefaultPrinter()]
            except: return ["Default Printer"]

    # ------------------------------------------------------------------
    # Caption helpers
    # ------------------------------------------------------------------

    def _get_caption_text(self):
        return self.entry_caption.get("1.0", "end-1c")

    def _on_caption_key(self, event=None):
        content = self.entry_caption.get("1.0", "end-1c")
        lines   = content.split("\n")
        if len(lines) > 3:
            self.entry_caption.delete("1.0", "end")
            self.entry_caption.insert("1.0", "\n".join(lines[:3]))
            self.entry_caption.mark_set("insert", "end-1c")
        self._refresh_live_preview()

    def _on_caption_return(self, event=None):
        if self.entry_caption.get("1.0","end-1c").count("\n") >= 2:
            return "break"

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def _on_space(self, event):
        if self.root.focus_get() is self.entry_caption:
            return
        self._on_capture_pressed()

    def _toggle_fullscreen(self, event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    def _exit_fullscreen(self, event=None):
        if self._fullscreen:
            self._fullscreen = False
            self.root.attributes("-fullscreen", False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        pal   = self._pal()
        BG    = pal["bg"]; CARD  = pal["card"]; FG = pal["fg"]
        MUTED = pal["muted"]; IB = pal["ib"]; ACCENT = pal["accent"]
        RED   = "#f38ba8"; GREEN = "#a6e3a1"; YELLOW = "#f9e2af"

        self.root.configure(bg=BG)

        # ttk style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=IB, background=CARD,
                          foreground=ACCENT, selectbackground=IB,
                          selectforeground=FG, arrowcolor=FG)
        combo_cfg = dict(state="readonly", font=("Segoe UI", 11))
        chk_cfg   = dict(activebackground=CARD, selectcolor=BG,
                          font=("Segoe UI", 10, "bold"), cursor="hand2", anchor="w")
        lbl_cfg   = dict(font=("Segoe UI", 9), fg=MUTED, bg=CARD, anchor="w")

        # ── Header: title + ⚙ on same line ───────────────────────────
        self.w_header = tk.Frame(self.root, bg=BG, pady=8)
        self.w_header.pack(fill="x")

        self.w_title = tk.Label(
            self.w_header, text="📷  Photo Booth  📷",
            font=("Segoe UI", 12, "bold"), fg=ACCENT, bg=BG)
        self.w_title.pack(anchor="center", side="left", padx=20)

        self.btn_settings = tk.Button(
            self.w_header, text="⚙",
            font=("Segoe UI", 12), fg=MUTED, bg=BG,
            relief="flat", cursor="hand2", bd=0,
            command=self._toggle_settings)
        self.btn_settings.pack(side="right", padx=20)

        # ── Camera canvas ─────────────────────────────────────────────
        self.w_canvas_frame = tk.Frame(self.root, bg=CARD, padx=4, pady=4)
        self.w_canvas_frame.pack(fill="both", expand=True, padx=20, pady=(0,0))

        self.canvas = tk.Canvas(self.w_canvas_frame, bg="#11111b",
                                 highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(400, 300, text="Connecting to camera…",
                                 fill=MUTED, font=("Segoe UI",16), tag="placeholder")

        # ── Controls panel ────────────────────────────────────────────
        self.w_ctrl = tk.Frame(self.root, bg=CARD, padx=10, pady=6)
        self.w_ctrl.pack(fill="x", padx=20, pady=(4,0))

        row = tk.Frame(self.w_ctrl, bg=CARD)
        row.pack(fill="x")
        self.w_ctrl_row = row

        # -- Col A: Caption toggle + text entry -----------------------
        col_a = tk.Frame(row, bg=CARD)
        col_a.pack(side="left", anchor="n", padx=(0,6))
        self.w_col_a = col_a

        self.chk_caption_btn = tk.Checkbutton(
            col_a, text="Caption", variable=self.caption_enabled,
            fg=ACCENT, bg=CARD, command=self._refresh_live_preview, **chk_cfg)
        self.chk_caption_btn.pack(anchor="w")

        self.entry_caption = tk.Text(
            col_a, font=("Segoe UI",11), bg=IB, fg=FG,
            insertbackground=FG, relief="flat", bd=4,
            height=3, width=28, wrap="word")
        self.entry_caption.pack(anchor="w")
        self.entry_caption.bind("<KeyRelease>", self._on_caption_key)
        self.entry_caption.bind("<Return>",     self._on_caption_return)

        # -- Col B: Caption options stacked ---------------------------
        col_b = tk.Frame(row, bg=CARD, padx=6)
        col_b.pack(side="left", anchor="n")
        self.w_col_b = col_b

        def sel_row(parent, label, var, values, w):
            r = tk.Frame(parent, bg=CARD, pady=1)
            r.pack(fill="x")
            tk.Label(r, text=label, width=7, **lbl_cfg).pack(side="left")
            cmb = ttk.Combobox(r, textvariable=var, values=values,
                                 width=w, **combo_cfg)
            cmb.pack(side="left")
            cmb.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_preview())
            return cmb

        sel_row(col_b, "Font",     self.caption_font,     sorted(FONTS.keys()), 14)
        sel_row(col_b, "Size",     self.caption_size,     FONT_SIZES,            5)
        sel_row(col_b, "Position", self.caption_position, CAPTION_POSITIONS,    14)
        sel_row(col_b, "Effect",   self.effect_name,      EFFECT_NAMES,         13)

        # -- Col C: Toggles -------------------------------------------
        col_c = tk.Frame(row, bg=CARD, padx=6)
        col_c.pack(side="left", anchor="n")
        self.w_col_c = col_c

        def mchk(parent, text, var, fg, cmd=None):
            kw = dict(chk_cfg)
            kw["bg"] = CARD
            kw["activebackground"] = CARD
            kw["selectcolor"] = BG
            c = tk.Checkbutton(parent, text=text, variable=var, fg=fg,
                                command=cmd or (lambda: None), **kw)
            c.pack(fill="x", pady=1)
            return c

        mchk(col_c, f"⏱ Countdown ({COUNTDOWN_SECONDS}s)", self.countdown_enabled, GREEN)
        mchk(col_c, f"💥 Burst ({BURST_COUNT} shots)",      self.burst_enabled,     ACCENT)
        mchk(col_c, "⬛ Grayscale",  self.grayscale_enabled, FG,     self._refresh_live_preview)
        mchk(col_c, "🕐 Timestamp",  self.timestamp_enabled, YELLOW, self._refresh_live_preview)
        mchk(col_c, "⚡ Flash mode", self.flash_enabled,     "#f9e2af", self._on_flash_toggle)

        # ── Status bar ────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Starting camera…")
        self.w_status = tk.Label(self.root, textvariable=self.status_var,
                                  font=("Segoe UI",10), fg=FG,
                                  bg=pal["status_bg"], anchor="w", padx=10)
        self.w_status.pack(fill="x", pady=(4,0))

        # ── Capture button + printer hint ─────────────────────────────
        self.w_btn_frame = tk.Frame(self.root, bg=BG, pady=8)
        self.w_btn_frame.pack()

        self.btn_capture = tk.Button(
            self.w_btn_frame, text="📸", bg=RED, fg=BG,
            font=("Segoe UI",22), relief="flat",
            cursor="hand2", padx=5, pady=0, bd=0,
            command=self._on_capture_pressed)
        self.btn_capture.pack()

        self.w_printer_lbl = tk.Label(
            self.w_btn_frame, textvariable=self.printer_var,
            font=("Segoe UI",9), fg=MUTED, bg=BG)
        self.w_printer_lbl.pack(pady=(2,0))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Flash mode — recolour every themed widget without rebuilding
    # ------------------------------------------------------------------

    def _on_flash_toggle(self):
        pal   = self._pal()
        BG    = pal["bg"]; CARD  = pal["card"]; FG = pal["fg"]
        MUTED = pal["muted"]; IB = pal["ib"]; ACCENT = pal["accent"]
        YELLOW = "#f9e2af"; GREEN = "#a6e3a1"

        flash = self.flash_enabled.get()

        # Root & top-level frames
        self.root.configure(bg=BG)
        self.w_header.configure(bg=BG)
        self.w_title.configure(bg=BG, fg=ACCENT)
        self.btn_settings.configure(bg=BG, fg=MUTED)
        self.w_canvas_frame.configure(bg=CARD,
                                       padx=60 if flash else 4,
                                       pady=60 if flash else 4)
        self.canvas.configure(bg="white" if flash else "#11111b")

        # Controls panel background
        self.w_ctrl.configure(bg=CARD)
        self.w_ctrl_row.configure(bg=CARD)
        for col in (self.w_col_a, self.w_col_b, self.w_col_c):
            col.configure(bg=CARD)

        # Recursively recolour all children of the controls panel
        self._recolour_children(self.w_ctrl, CARD, BG, FG, MUTED, IB, ACCENT)

        # Caption text widget colours
        self.entry_caption.configure(bg=IB, fg=FG, insertbackground=FG)

        # Status, button frame, printer label
        self.w_status.configure(fg=FG, bg=pal["status_bg"])
        self.w_btn_frame.configure(bg=BG)
        self.w_printer_lbl.configure(bg=BG, fg=MUTED)

        # ttk combobox style update
        style = ttk.Style()
        style.configure("TCombobox", fieldbackground=IB, background=IB,
                          foreground=ACCENT, selectbackground=IB,
                          selectforeground=FG, arrowcolor=FG)

        if self._settings_flyout and self._settings_flyout.winfo_exists():
            self._settings_flyout.recolour()

        self._refresh_live_preview()

    def _recolour_children(self, widget, card, bg, fg, muted, ib, accent):
        """Walk widget tree and recolour Label, Frame, Checkbutton children."""
        for child in widget.winfo_children():
            cls = child.winfo_class()
            if cls == "Frame":
                child.configure(bg=card)
                self._recolour_children(child, card, bg, fg, muted, ib, accent)
            elif cls == "Label":
                child.configure(bg=card, fg=muted)
            elif cls == "Checkbutton":
                # Preserve each checkbox's fg colour (green/accent/yellow/white)
                child.configure(bg=card, activebackground=card, selectcolor=bg)
                self._recolour_children(child, card, bg, fg, muted, ib, accent)

    # ------------------------------------------------------------------
    # Settings flyout
    # ------------------------------------------------------------------

    def _toggle_settings(self):
        if self._settings_flyout and self._settings_flyout.winfo_exists():
            self._settings_flyout._on_close()
        else:
            self.btn_settings.config(relief="sunken")
            self._settings_flyout = SettingsFlyout(self)

    # ------------------------------------------------------------------
    # Camera change
    # ------------------------------------------------------------------

    def _on_camera_change(self):
        label = self.camera_index_var.get()
        idx   = int(label.split()[-1]) if label else 0
        threading.Thread(target=self._switch_camera, args=(idx,), daemon=True).start()

    def _switch_camera(self, idx):
        self.running = False
        if self._cam_thread:
            self._cam_thread.stop(); self._cam_thread.join(timeout=1)
        if self.cap: self.cap.release()
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.root.after(0, lambda: self.status_var.set("❌  Could not open camera."))
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap = cap
        self._frame_queue = queue.Queue(maxsize=2)
        self._cam_thread  = CameraThread(cap, self._frame_queue)
        self._cam_thread.start()
        self.running = True
        self.root.after(0, lambda: self.status_var.set(
            "✅  Camera ready — press 📸 or Space to capture & print"))

    # ------------------------------------------------------------------
    # Frame composition
    # ------------------------------------------------------------------

    def _caption_params(self):
        return dict(text=self._get_caption_text() if self.caption_enabled.get() else "",
                     font_name=self.caption_font.get(),
                     font_size=int(self.caption_size.get()),
                     position=self.caption_position.get())

    def _compose_frame(self, bgr_frame, countdown_num=None):
        frame = bgr_frame.copy()
        frame = apply_effect(frame, EFFECT_KEYS.get(self.effect_name.get(), "none"))
        if self.mirror_enabled.get():
            frame = cv2.flip(frame, 1)
        if any(v.get()!=0 for v in (self.adj_brightness,self.adj_contrast,
                                     self.adj_exposure,self.adj_shadows)):
            frame = apply_adjustments(frame,
                                       self.adj_brightness.get(), self.adj_contrast.get(),
                                       self.adj_exposure.get(),   self.adj_shadows.get())
        if self.grayscale_enabled.get():
            frame = cv2.cvtColor(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),cv2.COLOR_GRAY2BGR)
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        p = self._caption_params()
        if p["text"].strip():
            pil = _apply_caption(pil, **p)
        if self.timestamp_enabled.get():
            pil = _apply_timestamp(pil)
        if countdown_num is not None:
            pil = _apply_countdown(pil, countdown_num)
        return pil

    def _refresh_live_preview(self):
        frame = self.frozen_frame if self.preview_frozen else self.last_frame
        if frame is not None:
            self._display_pil(self._compose_frame(frame))

    # ------------------------------------------------------------------
    # Camera loop
    # ------------------------------------------------------------------

    def _start_camera(self):
        threading.Thread(target=self._open_camera, daemon=True).start()

    def _open_camera(self):
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.root.after(0, lambda: self.status_var.set(
                "❌  No webcam found. Connect a camera and restart."))
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap = cap
        self._cam_thread = CameraThread(cap, self._frame_queue)
        self._cam_thread.start()
        self.running = True
        self.root.after(0, self._schedule_frame)
        self.root.after(0, lambda: self.status_var.set(
            "✅  Camera ready — press 📸 or Space to capture & print"))

    def _schedule_frame(self):
        if not self.running: return
        if not self.preview_frozen:
            try:
                frame = self._frame_queue.get_nowait()
                self.last_frame = frame
                self._display_pil(
                    self._compose_frame(frame, countdown_num=self._countdown_overlay))
            except queue.Empty:
                pass
        self._after_id = self.root.after(33, self._schedule_frame)

    def _display_pil(self, pil_img):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10: return
        iw,ih  = pil_img.size
        scale  = min(cw/iw, ch/ih)
        nw,nh  = int(iw*scale), int(ih*scale)
        preview = pil_img.resize((nw,nh), Image.LANCZOS)
        tk_img  = ImageTk.PhotoImage(preview)
        self.canvas._tk_img = tk_img
        self.canvas.delete("all")
        self.canvas.configure(bg="white" if self.flash_enabled.get() else "#11111b")
        self.canvas.create_image(cw//2, ch//2, anchor="center", image=tk_img)

    # ------------------------------------------------------------------
    # Capture / countdown / burst
    # ------------------------------------------------------------------

    def _on_capture_pressed(self):
        if self.last_frame is None:
            messagebox.showwarning("No frame", "Camera is not ready yet.")
            return
        if self._sequence_active: return
        self._sequence_active = True
        self.btn_capture.config(state="disabled")
        self._burst_remaining = BURST_COUNT if self.burst_enabled.get() else 1
        if self.countdown_enabled.get():
            self._run_countdown(COUNTDOWN_SECONDS)
        else:
            self._do_capture_and_print()

    def _run_countdown(self, n):
        if n <= 0:
            self._countdown_overlay = None
            self._do_capture_and_print(); return
        self._countdown_overlay = n
        self.status_var.set(f"⏱  Get ready… {n}")
        if self.sounds_enabled.get():
            play_async(_play_beep)
        self._sequence_after = self.root.after(1000, lambda: self._run_countdown(n-1))

    def _run_inter_burst_countdown(self, n):
        if n <= 0:
            self._countdown_overlay = None
            self._do_capture_and_print(); return
        self._countdown_overlay = n
        self.status_var.set(f"💥  Next burst shot in {n}…")
        if self.sounds_enabled.get():
            play_async(_play_beep)
        self._sequence_after = self.root.after(1000, lambda: self._run_inter_burst_countdown(n-1))

    def _do_capture_and_print(self):
        try:    frame = self._frame_queue.get_nowait(); self.last_frame = frame
        except queue.Empty: frame = self.last_frame
        if frame is None: self._end_sequence(); return

        self.frozen_frame   = frame.copy()
        composited          = self._compose_frame(frame, countdown_num=None)
        self.preview_frozen = True
        self._display_pil(composited)

        if self.sounds_enabled.get():
            play_async(_play_capture)

        shot_num   = (BURST_COUNT - self._burst_remaining + 1) if self.burst_enabled.get() else 1
        burst_info = f" ({shot_num}/{BURST_COUNT})" if self.burst_enabled.get() else ""
        self.status_var.set(f"📸  Captured{burst_info} — sending to printer…")

        if self.save_enabled.get():
            try:
                cv2.imwrite(datetime.now().strftime("%Y%m%d-%H%M%S")+".jpg",
                             self.frozen_frame)
            except Exception: pass

        self._burst_remaining -= 1
        threading.Thread(target=self._do_print,
                          args=(composited.copy(), self._burst_remaining),
                          daemon=True).start()

    def _do_print(self, pil_image, burst_remaining):
        try:
            printer = self.printer_var.get() or win32print.GetDefaultPrinter()
            print_image(pil_image, printer, landscape=self.landscape_enabled.get())
            self.root.after(0, lambda: self._after_print(burst_remaining))
        except Exception as exc:
            self.root.after(0, lambda: self._print_error(str(exc)))

    def _after_print(self, burst_remaining):
        self.preview_frozen = False
        if burst_remaining > 0:
            self._run_inter_burst_countdown(BURST_DELAY)
        else:
            shots = BURST_COUNT if self.burst_enabled.get() else 1
            self.status_var.set(
                f"✅  {shots} prints sent — ready for next round!" if shots > 1
                else "✅  Print job sent — ready for next shot!")
            self._end_sequence()

    def _end_sequence(self):
        self._sequence_active   = False
        self._countdown_overlay = None
        self.preview_frozen     = False
        self.frozen_frame       = None
        self.btn_capture.config(state="normal")

    def _print_error(self, msg):
        self.status_var.set(f"❌  Print failed: {msg}")
        messagebox.showerror("Print Error", f"Could not print:\n\n{msg}")
        self._end_sequence()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_close(self):
        self.running = False
        if self._after_id:       self.root.after_cancel(self._after_id)
        if self._sequence_after: self.root.after_cancel(self._sequence_after)
        if self._settings_flyout and self._settings_flyout.winfo_exists():
            self._settings_flyout.destroy()
        if self._cam_thread:     self._cam_thread.stop()
        if self.cap:             self.cap.release()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    WebcamPrintApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()