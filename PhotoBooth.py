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

# Windows printing via pywin32
import win32print
import win32ui
import win32con
import win32gui
from PIL import ImageWin


# ---------------------------------------------------------------------------
# Constants & configuration
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

FONT_SIZES = [32, 40, 48, 60, 72, 90, 112, 132]

WINDOWS_FONTS_DIR = r"C:\Windows\Fonts"

COUNTDOWN_SECONDS = 3
BURST_COUNT       = 3
BURST_DELAY       = 2

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
EFFECT_DISPLAY_NAMES = [e[0] for e in EFFECTS]
EFFECT_KEYS          = {e[0]: e[1] for e in EFFECTS}

# Sound frequencies (Hz) and durations (ms)
SND_BEEP_FREQ    = 880    # countdown tick
SND_BEEP_MS      = 120
SND_CAPTURE_FREQ = 1400   # shutter "boop" — first tone
SND_CAPTURE_MS   = 80
SND_CAPTURE2_FREQ = 1800  # second tone (two-tone boop)
SND_CAPTURE2_MS  = 120


# ---------------------------------------------------------------------------
# Sound helpers  (non-blocking — run in daemon thread)
# ---------------------------------------------------------------------------

def _play_beep():
    """Short high beep for countdown ticks."""
    winsound.Beep(SND_BEEP_FREQ, SND_BEEP_MS)


def _play_capture():
    """Two-tone ascending boop for shutter."""
    winsound.Beep(SND_CAPTURE_FREQ,  SND_CAPTURE_MS)
    winsound.Beep(SND_CAPTURE2_FREQ, SND_CAPTURE2_MS)


def play_async(fn):
    threading.Thread(target=fn, daemon=True).start()


# ---------------------------------------------------------------------------
# Image adjustments  (operate on BGR numpy arrays)
# ---------------------------------------------------------------------------

def apply_adjustments(bgr: np.ndarray,
                      brightness: float,   # -100 … +100, 0 = neutral
                      contrast:   float,   # -100 … +100, 0 = neutral
                      exposure:   float,   # -100 … +100, 0 = neutral (EV-style)
                      shadows:    float,   # -100 … +100, 0 = neutral
                      ) -> np.ndarray:
    """Apply brightness, contrast, exposure and shadow lift to a BGR frame."""
    img = bgr.astype(np.float32)

    # Exposure: multiplicative EV-style scale (±2 stops)
    ev_scale = 2.0 ** (exposure / 50.0)   # exposure=100 → ×4, exposure=-100 → ×0.25
    img *= ev_scale

    # Brightness: simple additive offset
    img += brightness * 2.55              # map ±100 → ±255

    # Contrast: scale around mid-grey (128)
    c_factor = (contrast + 100) / 100.0  # 0 … 2
    img = (img - 128) * c_factor + 128

    # Shadows: lift the dark tones only (gamma-curve in the shadows)
    # Positive shadow = brighten darks; negative = crush them
    if shadows != 0:
        norm = img / 255.0
        norm = np.clip(norm, 0, 1)
        shadow_strength = shadows / 100.0
        # Apply only where luminance < 0.5
        mask = 1.0 - np.clip(norm * 2.0, 0, 1)   # 1 in pure black, 0 at mid-grey
        img += mask * shadow_strength * 80

    return np.clip(img, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Camera effects  (operate on BGR numpy arrays)
# ---------------------------------------------------------------------------

def _remap(bgr, map_x, map_y):
    return cv2.remap(bgr, map_x, map_y,
                     interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REFLECT)


def apply_effect(bgr: np.ndarray, effect_key: str) -> np.ndarray:
    if effect_key == "none" or bgr is None:
        return bgr

    h, w = bgr.shape[:2]
    cx, cy = w / 2.0, h / 2.0

    if effect_key == "mirror_left":
        left = bgr[:, :w // 2]
        return np.hstack([left, cv2.flip(left, 1)])

    if effect_key == "mirror_right":
        right = bgr[:, w // 2:]
        return np.hstack([cv2.flip(right, 1), right])

    if effect_key == "kaleidoscope":
        tl   = bgr[:h // 2, :w // 2]
        top  = np.hstack([tl, cv2.flip(tl, 1)])
        return np.vstack([top, cv2.flip(top, 0)])

    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = xs - cx, ys - cy
    r      = np.sqrt(dx**2 + dy**2)
    theta  = np.arctan2(dy, dx)

    if effect_key == "fisheye":
        max_r = min(cx, cy)
        r_new = max_r * (np.clip(r / max_r, 0, 1) ** 1.6)
        return _remap(bgr,
                      (cx + r_new * np.cos(theta)).astype(np.float32),
                      (cy + r_new * np.sin(theta)).astype(np.float32))

    if effect_key == "bulge":
        max_r = min(cx, cy)
        r_new = max_r * np.sqrt(np.clip(r / max_r, 0, 1))
        return _remap(bgr,
                      (cx + r_new * np.cos(theta)).astype(np.float32),
                      (cy + r_new * np.sin(theta)).astype(np.float32))

    if effect_key == "pinch":
        max_r = min(cx, cy)
        r_new = max_r * (np.clip(r / max_r, 0, 1) ** 2.0)
        return _remap(bgr,
                      (cx + r_new * np.cos(theta)).astype(np.float32),
                      (cy + r_new * np.sin(theta)).astype(np.float32))

    if effect_key == "twist":
        max_r  = min(cx, cy)
        angle  = 2.5 * (1.0 - np.clip(r / max_r, 0, 1))
        t_new  = theta + angle
        rc     = np.clip(r, 0, max_r)
        return _remap(bgr,
                      (cx + rc * np.cos(t_new)).astype(np.float32),
                      (cy + rc * np.sin(t_new)).astype(np.float32))

    if effect_key == "dent":
        map_x = np.mgrid[0:h, 0:w][1].astype(np.float32)
        map_y = (np.mgrid[0:h, 0:w][0]
                 + h * 0.06 * np.sin(map_x / w * 6 * np.pi)).astype(np.float32)
        return _remap(bgr, map_x, map_y)

    return bgr


# ---------------------------------------------------------------------------
# Caption / timestamp / countdown helpers
# ---------------------------------------------------------------------------

def _load_pil_font(font_filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(WINDOWS_FONTS_DIR, font_filename)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _draw_outlined_text(draw, xy, text, font, fill=(255,255,255,255),
                         stroke_width=3, stroke_fill=(0,0,0,200),
                         align="left", anchor=None):
    """Unified helper: draw text with outline using multiline_text."""
    kwargs = dict(font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill,
                  align=align)
    if anchor:
        kwargs["anchor"] = anchor
    draw.multiline_text(xy, text, **kwargs)


def _apply_caption(pil_img, text, font_name, font_size, position):
    if not text.strip():
        return pil_img

    img  = pil_img.copy().convert("RGBA")
    w, h = img.size
    font = _load_pil_font(FONTS.get(font_name, "arial.ttf"), font_size)

    draw = ImageDraw.Draw(img)
    bbox = draw.multiline_textbbox((0, 0), text, font=font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    PADDING = 12
    pos = position.lower()
    tx = (PADDING if "left" in pos
          else w - tw - PADDING if "right" in pos
          else (w - tw) // 2)
    ty = (PADDING if "top" in pos
          else h - th - PADDING * 4 if "bottom" in pos
          else (h - th) // 2)

    stroke = int(min(font_size / 6, 8))
    _draw_outlined_text(draw, (tx, ty), text, font,
                         stroke_width=stroke, align="center")
    return img.convert("RGB")


def _apply_timestamp(pil_img, font_size=22):
    """Render a small timestamp in the bottom-right corner."""
    img  = pil_img.copy().convert("RGBA")
    w, h = img.size
    font = _load_pil_font("arialbd.ttf", font_size)

    ts   = datetime.now().strftime("%y-%m-%d  %H:%M:%S")
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), ts, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    PAD = 10
    tx  = w - tw - PAD
    ty  = h - th - PAD

    _draw_outlined_text(draw, (tx, ty), ts, font, stroke_width=2)
    return img.convert("RGB")


def _apply_countdown(pil_img, number):
    """Large centred countdown digit — preview only, never printed."""
    img  = pil_img.copy().convert("RGBA")
    w, h = img.size
    cx, cy = w // 2, h // 2

    font_size = int(h * 0.52)
    font_path = os.path.join(WINDOWS_FONTS_DIR, "impact.ttf")
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()

    text  = str(number)
    dummy = ImageDraw.Draw(img)
    bbox  = dummy.textbbox((cx, cy), text, font=font, anchor="mm")
    r_pad = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) // 2 + 36

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(
        [cx - r_pad, cy - r_pad, cx + r_pad, cy + r_pad],
        fill=(0, 0, 0, 150),
    )
    img = Image.alpha_composite(img, overlay)

    stroke = int(min(font_size / 6, 14))
    ImageDraw.Draw(img).text(
        (cx, cy), text, font=font, anchor="mm",
        fill=(255, 255, 255, 255),
        stroke_width=stroke, stroke_fill=(0, 0, 0, 220),
    )
    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Printer
# ---------------------------------------------------------------------------

def print_image(pil_image: Image.Image) -> None:
    pil_image    = pil_image.rotate(-90, expand=True)
    printer_name = win32print.GetDefaultPrinter()
    hprinter     = win32print.OpenPrinter(printer_name)
    hdc          = None
    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        px = hdc.GetDeviceCaps(win32con.HORZRES)
        py = hdc.GetDeviceCaps(win32con.VERTRES)
        iw, ih = pil_image.size
        scale  = min(px / iw, py / ih)
        hdc.StartDoc("Photo Booth")
        hdc.StartPage()
        ImageWin.Dib(pil_image).draw(
            hdc.GetHandleOutput(), (0, 0, int(iw * scale), int(ih * scale))
        )
        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()
    finally:
        win32print.ClosePrinter(hprinter)


# ---------------------------------------------------------------------------
# Background camera thread
# ---------------------------------------------------------------------------

class CameraThread(threading.Thread):
    def __init__(self, cap, frame_queue: queue.Queue):
        super().__init__(daemon=True)
        self.cap         = cap
        self.frame_queue = frame_queue
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            ret, frame = self.cap.read()
            if ret:
                while not self.frame_queue.empty():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        break
                self.frame_queue.put(frame)
            time.sleep(0.01)

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class WebcamPrintApp:
    PREVIEW_W = 1280
    PREVIEW_H = 720

    BG       = "#1e1e2e"
    CARD     = "#313244"
    FG       = "#cdd6f4"
    ACCENT   = "#cba6f7"
    RED      = "#f38ba8"
    GREEN    = "#a6e3a1"
    YELLOW   = "#f9e2af"
    MUTED    = "#585b70"
    INPUT_BG = "#45475a"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Booth")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.cap            = None
        self._cam_thread    = None
        self._frame_queue   = queue.Queue(maxsize=2)
        self.running        = False
        self.last_frame     = None
        self.frozen_frame   = None
        self.preview_frozen = False
        self._after_id      = None

        # Sequence state
        self._sequence_active   = False
        self._burst_remaining   = 0
        self._sequence_after    = None
        self._countdown_overlay = None   # int digit or None

        # ── Caption ───────────────────────────────────────────────────
        self.caption_enabled  = tk.BooleanVar(value=True)
        self.caption_font     = tk.StringVar(value="Impact")
        self.caption_size     = tk.IntVar(value=72)
        self.caption_position = tk.StringVar(value="Bottom Centre")

        # ── Countdown / burst ─────────────────────────────────────────
        self.countdown_enabled = tk.BooleanVar(value=False)
        self.burst_enabled     = tk.BooleanVar(value=False)

        # ── Effect / mirror / grayscale ───────────────────────────────
        self.effect_name    = tk.StringVar(value="None")
        self.mirror_enabled = tk.BooleanVar(value=True)
        self.grayscale_enabled = tk.BooleanVar(value=False)

        # ── Timestamp ─────────────────────────────────────────────────
        self.timestamp_enabled = tk.BooleanVar(value=False)

        # ── Image adjustments  (all default to 0 = neutral) ──────────
        self.adj_brightness = tk.DoubleVar(value=0)
        self.adj_contrast   = tk.DoubleVar(value=0)
        self.adj_exposure   = tk.DoubleVar(value=0)
        self.adj_shadows    = tk.DoubleVar(value=0)

        # ── Save ──────────────────────────────────────────────────────
        self.save_enabled = tk.BooleanVar(value=True)

        self._build_ui()
        self._start_camera()

    # ------------------------------------------------------------------
    # Caption text helper
    # ------------------------------------------------------------------

    def _get_caption_text(self) -> str:
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
        if self.entry_caption.get("1.0", "end-1c").count("\n") >= 2:
            return "break"

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        BG    = self.BG;  CARD  = self.CARD;  FG    = self.FG
        ACCENT= self.ACCENT; RED = self.RED; GREEN = self.GREEN
        YELLOW= self.YELLOW; MUTED = self.MUTED; IB = self.INPUT_BG

        # ── Header ────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="📷  Photo Booth  📷",
                 font=("Segoe UI", 18, "bold"), fg=ACCENT, bg=BG).pack()

        # ── Camera canvas ─────────────────────────────────────────────
        cf = tk.Frame(self.root, bg=CARD, padx=4, pady=4)
        cf.pack(padx=20, pady=(0, 0))
        self.canvas = tk.Canvas(cf, width=self.PREVIEW_W, height=self.PREVIEW_H,
                                bg="#11111b", highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_text(self.PREVIEW_W // 2, self.PREVIEW_H // 2,
                                text="Connecting to camera…",
                                fill=MUTED, font=("Segoe UI", 16), tag="placeholder")

        # ── Combobox style ────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                         fieldbackground=IB, background=IB, foreground=FG,
                         selectbackground=IB, selectforeground=FG, arrowcolor=FG)
        combo_cfg = dict(state="readonly", font=("Segoe UI", 10),
                         background=IB, foreground=CARD)

        # ── Controls panel ────────────────────────────────────────────
        outer = tk.Frame(self.root, bg=CARD, padx=10, pady=6)
        outer.pack(fill="x", padx=20, pady=(4, 0))

        row = tk.Frame(outer, bg=CARD)
        row.pack(fill="x")

        chk_cfg = dict(bg=CARD, activebackground=CARD, selectcolor=BG,
                       font=("Segoe UI", 10, "bold"), cursor="hand2", anchor="w")
        lbl_cfg = dict(font=("Segoe UI", 9), fg=MUTED, bg=CARD, anchor="w")

        # ── Col A: Caption + text entry ───────────────────────────────
        col_a = tk.Frame(row, bg=CARD)
        col_a.pack(side="left", anchor="n", padx=(0, 6))

        tk.Checkbutton(col_a, text="Caption", variable=self.caption_enabled,
                        fg=ACCENT, command=self._refresh_live_preview,
                        **chk_cfg).pack(anchor="w")
        self.entry_caption = tk.Text(
            col_a, font=("Segoe UI", 11), bg=IB, fg=FG,
            insertbackground=FG, relief="flat", bd=4,
            height=3, width=28, wrap="word")
        self.entry_caption.pack(anchor="w")
        self.entry_caption.bind("<KeyRelease>", self._on_caption_key)
        self.entry_caption.bind("<Return>",     self._on_caption_return)

        # ── Col B: Caption options stacked ────────────────────────────
        col_b = tk.Frame(row, bg=CARD, padx=6)
        col_b.pack(side="left", anchor="n")

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
        sel_row(col_b, "Effect",   self.effect_name,      EFFECT_DISPLAY_NAMES, 13)

        # ── Col C: Toggles ────────────────────────────────────────────
        col_c = tk.Frame(row, bg=CARD, padx=6)
        col_c.pack(side="left", anchor="n")

        tk.Checkbutton(col_c, text=f"⏱ Countdown ({COUNTDOWN_SECONDS}s)",
                        variable=self.countdown_enabled, fg=GREEN, **chk_cfg
                        ).pack(fill="x", pady=1)
        tk.Checkbutton(col_c, text=f"💥 Burst ({BURST_COUNT} shots)",
                        variable=self.burst_enabled, fg=ACCENT, **chk_cfg
                        ).pack(fill="x", pady=1)
        tk.Checkbutton(col_c, text="🪞 Mirror",
                        variable=self.mirror_enabled, fg=FG,
                        command=self._refresh_live_preview, **chk_cfg
                        ).pack(fill="x", pady=1)
        tk.Checkbutton(col_c, text="⬛ Grayscale",
                        variable=self.grayscale_enabled, fg=FG,
                        command=self._refresh_live_preview, **chk_cfg
                        ).pack(fill="x", pady=1)
        tk.Checkbutton(col_c, text="🕐 Timestamp",
                        variable=self.timestamp_enabled, fg=YELLOW,
                        command=self._refresh_live_preview, **chk_cfg
                        ).pack(fill="x", pady=1)

        # ── Col D: Image adjustments ──────────────────────────────────
        col_d = tk.Frame(row, bg=CARD, padx=8)
        col_d.pack(side="left", anchor="n")

        tk.Label(col_d, text="Image Adjustments",
                 font=("Segoe UI", 9, "bold"), fg=MUTED, bg=CARD
                 ).pack(anchor="w", pady=(0, 2))

        def adj_row(parent, label, var, frm=-100, to=100):
            r = tk.Frame(parent, bg=CARD)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, width=11, **lbl_cfg).pack(side="left")
            sl = tk.Scale(
                r, variable=var, from_=frm, to=to,
                orient="horizontal", length=160,
                bg=CARD, fg=FG, troughcolor=IB,
                highlightthickness=0, bd=0,
                showvalue=False, resolution=1,
                command=lambda _: self._refresh_live_preview(),
            )
            sl.pack(side="left")
            # Live value label
            val_lbl = tk.Label(r, textvariable=var, width=4,
                                font=("Segoe UI", 8), fg=MUTED, bg=CARD)
            val_lbl.pack(side="left")
            # Reset button
            tk.Button(r, text="↺", font=("Segoe UI", 8), fg=MUTED, bg=CARD,
                       relief="flat", cursor="hand2", bd=0,
                       command=lambda v=var: (v.set(0), self._refresh_live_preview())
                       ).pack(side="left", padx=(2, 0))

        adj_row(col_d, "Brightness", self.adj_brightness)
        adj_row(col_d, "Contrast",   self.adj_contrast)
        adj_row(col_d, "Exposure",   self.adj_exposure)
        adj_row(col_d, "Shadows",    self.adj_shadows)

        # Reset all button
        tk.Button(col_d, text="Reset All", font=("Segoe UI", 8, "bold"),
                   fg=MUTED, bg=self.INPUT_BG, relief="flat", cursor="hand2", bd=0,
                   padx=4, pady=2,
                   command=self._reset_adjustments
                   ).pack(anchor="e", pady=(4, 0))

        # ── Status bar ────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Starting camera…")
        tk.Label(self.root, textvariable=self.status_var,
                 font=("Segoe UI", 10), fg=FG, bg="#181825",
                 anchor="w", padx=10).pack(fill="x", pady=(4, 0))

        # ── Capture button ────────────────────────────────────────────
        bf = tk.Frame(self.root, bg=BG, pady=10)
        bf.pack()
        self.btn_capture = tk.Button(
            bf, text="📸", bg=RED, fg=BG,
            font=("Segoe UI", 22), relief="flat",
            cursor="hand2", padx=5, pady=0, bd=0,
            command=self._on_capture_pressed)
        self.btn_capture.pack()

        # ── Printer label ─────────────────────────────────────────────
        try:
            pn = win32print.GetDefaultPrinter()
        except Exception:
            pn = "Unknown"
        pf = tk.Frame(self.root, bg=BG, pady=2)
        pf.pack()
        tk.Label(pf, text=f"Default printer:  {pn}",
                 font=("Segoe UI", 9), fg=MUTED, bg=BG).pack()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _reset_adjustments(self):
        for v in (self.adj_brightness, self.adj_contrast,
                  self.adj_exposure, self.adj_shadows):
            v.set(0)
        self._refresh_live_preview()

    # ------------------------------------------------------------------
    # Frame composition pipeline
    # ------------------------------------------------------------------

    def _caption_params(self):
        return dict(
            text      = self._get_caption_text() if self.caption_enabled.get() else "",
            font_name = self.caption_font.get(),
            font_size = int(self.caption_size.get()),
            position  = self.caption_position.get(),
        )

    def _compose_frame(self, bgr_frame, countdown_num=None) -> Image.Image:
        """
        Full pipeline (BGR numpy → PIL RGB):
          1. Camera effect  (distortion)
          2. Mirror
          3. Image adjustments (brightness / contrast / exposure / shadows)
          4. Grayscale
          5. → PIL RGB
          6. Caption  (text, not affected by effects)
          7. Timestamp
          8. Countdown overlay  (preview only)
        """
        frame = bgr_frame.copy()

        # 1. Effect
        effect_key = EFFECT_KEYS.get(self.effect_name.get(), "none")
        frame = apply_effect(frame, effect_key)

        # 2. Mirror
        if self.mirror_enabled.get():
            frame = cv2.flip(frame, 1)

        # 3. Image adjustments
        adj_needed = any(v.get() != 0 for v in (
            self.adj_brightness, self.adj_contrast,
            self.adj_exposure, self.adj_shadows))
        if adj_needed:
            frame = apply_adjustments(
                frame,
                brightness = self.adj_brightness.get(),
                contrast   = self.adj_contrast.get(),
                exposure   = self.adj_exposure.get(),
                shadows    = self.adj_shadows.get(),
            )

        # 4. Grayscale
        if self.grayscale_enabled.get():
            gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame = cv2.cvtColor(gray,  cv2.COLOR_GRAY2BGR)

        # 5. → PIL
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        # 6. Caption
        p = self._caption_params()
        if p["text"].strip():
            pil = _apply_caption(pil, **p)

        # 7. Timestamp
        if self.timestamp_enabled.get():
            pil = _apply_timestamp(pil)

        # 8. Countdown (preview only — caller passes None for printable frames)
        if countdown_num is not None:
            pil = _apply_countdown(pil, countdown_num)

        return pil

    def _refresh_live_preview(self):
        frame = self.frozen_frame if self.preview_frozen else self.last_frame
        if frame is not None:
            self._display_pil(self._compose_frame(frame))

    # ------------------------------------------------------------------
    # Camera — background thread + UI poll loop
    # ------------------------------------------------------------------

    def _start_camera(self) -> None:
        threading.Thread(target=self._open_camera, daemon=True).start()

    def _open_camera(self) -> None:
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
            "✅  Camera ready — press 📸 to capture & print"))

    def _schedule_frame(self) -> None:
        if not self.running:
            return
        if not self.preview_frozen:
            try:
                frame = self._frame_queue.get_nowait()
                self.last_frame = frame
                self._display_pil(
                    self._compose_frame(frame, countdown_num=self._countdown_overlay)
                )
            except queue.Empty:
                pass
        self._after_id = self.root.after(33, self._schedule_frame)

    def _display_pil(self, pil_img: Image.Image) -> None:
        w, h   = pil_img.size
        scale  = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = int(w * scale), int(h * scale)
        preview = pil_img.resize((nw, nh), Image.LANCZOS)
        tk_img  = ImageTk.PhotoImage(preview)
        self.canvas._tk_img = tk_img
        self.canvas.delete("all")
        self.canvas.create_image(self.PREVIEW_W // 2, self.PREVIEW_H // 2,
                                  anchor="center", image=tk_img)

    # ------------------------------------------------------------------
    # Capture / countdown / burst
    # ------------------------------------------------------------------

    def _on_capture_pressed(self) -> None:
        if self.last_frame is None:
            messagebox.showwarning("No frame", "Camera is not ready yet.")
            return
        if self._sequence_active:
            return

        self._sequence_active = True
        self.btn_capture.config(state="disabled")
        self._burst_remaining = BURST_COUNT if self.burst_enabled.get() else 1

        if self.countdown_enabled.get():
            self._run_countdown(COUNTDOWN_SECONDS)
        else:
            self._do_capture_and_print()

    def _run_countdown(self, seconds_left: int) -> None:
        if seconds_left <= 0:
            self._countdown_overlay = None
            self._do_capture_and_print()
            return
        self._countdown_overlay = seconds_left
        self.status_var.set(f"⏱  Get ready… {seconds_left}")
        play_async(_play_beep)
        self._sequence_after = self.root.after(
            1000, lambda: self._run_countdown(seconds_left - 1))

    def _run_inter_burst_countdown(self, seconds_left: int) -> None:
        if seconds_left <= 0:
            self._countdown_overlay = None
            self._do_capture_and_print()
            return
        self._countdown_overlay = seconds_left
        self.status_var.set(f"💥  Next burst shot in {seconds_left}…")
        play_async(_play_beep)
        self._sequence_after = self.root.after(
            1000, lambda: self._run_inter_burst_countdown(seconds_left - 1))

    def _do_capture_and_print(self) -> None:
        # Grab the freshest available frame
        try:
            frame = self._frame_queue.get_nowait()
            self.last_frame = frame
        except queue.Empty:
            frame = self.last_frame
        if frame is None:
            self._end_sequence()
            return

        self.frozen_frame = frame.copy()

        # Compose WITHOUT countdown overlay
        composited = self._compose_frame(frame, countdown_num=None)
        self.preview_frozen = True
        self._display_pil(composited)

        play_async(_play_capture)

        shot_num   = (BURST_COUNT - self._burst_remaining + 1) if self.burst_enabled.get() else 1
        burst_info = f" ({shot_num}/{BURST_COUNT})" if self.burst_enabled.get() else ""
        self.status_var.set(f"📸  Captured{burst_info} — sending to printer…")

        if self.save_enabled.get():
            try:
                cv2.imwrite(
                    datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg",
                    self.frozen_frame)
            except Exception:
                pass

        self._burst_remaining -= 1

        threading.Thread(target=self._do_print,
                          args=(composited.copy(), self._burst_remaining),
                          daemon=True).start()

    def _do_print(self, pil_image, burst_remaining):
        try:
            print_image(pil_image)
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
        if self._after_id:
            self.root.after_cancel(self._after_id)
        if self._sequence_after:
            self.root.after_cancel(self._sequence_after)
        if self._cam_thread:
            self._cam_thread.stop()
        if self.cap:
            self.cap.release()
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