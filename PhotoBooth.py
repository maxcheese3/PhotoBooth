"""
Photo Booth Application
-----------------------
Captures images from a webcam, overlays an optional caption, and sends
the result directly to the default Windows printer via the GDI API.

Requirements (install with pip):
    pip install opencv-python pillow pywin32 numpy scipy

Run:
    python PhotoBooth.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import os
import time
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

# Camera effects — (display name, internal key)
EFFECTS = [
    ("None",             "none"),
    ("Mirror Left",      "mirror_left"),
    ("Mirror Right",     "mirror_right"),
    ("Kaleidoscope",     "kaleidoscope"),
    ("Fisheye",          "fisheye"),
    ("Bulge",            "bulge"),
    ("Pinch",            "pinch"),
    ("Twist",            "twist"),
    ("Dent",             "dent"),
]
EFFECT_DISPLAY_NAMES = [e[0] for e in EFFECTS]
EFFECT_KEYS          = {e[0]: e[1] for e in EFFECTS}


# ---------------------------------------------------------------------------
# Camera effects  (operate on BGR numpy arrays)
# ---------------------------------------------------------------------------

def _remap_effect(bgr: np.ndarray, map_x: np.ndarray, map_y: np.ndarray) -> np.ndarray:
    return cv2.remap(bgr, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REFLECT)


def _make_polar_maps(h, w):
    """Shared polar coordinate grids used by several effects."""
    cx, cy  = w / 2.0, h / 2.0
    ys, xs  = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy  = xs - cx, ys - cy
    r       = np.sqrt(dx**2 + dy**2)
    theta   = np.arctan2(dy, dx)
    return cx, cy, dx, dy, r, theta


def apply_effect(bgr: np.ndarray, effect_key: str) -> np.ndarray:
    if effect_key == "none" or bgr is None:
        return bgr

    h, w = bgr.shape[:2]
    cx, cy = w / 2.0, h / 2.0

    # ── Mirror Left: left half mirrored to fill the whole frame ──────
    if effect_key == "mirror_left":
        left = bgr[:, :w // 2]
        return np.hstack([left, cv2.flip(left, 1)])

    # ── Mirror Right: right half mirrored ────────────────────────────
    if effect_key == "mirror_right":
        right = bgr[:, w // 2:]
        return np.hstack([cv2.flip(right, 1), right])

    # ── Kaleidoscope: 4-quadrant mirror ──────────────────────────────
    if effect_key == "kaleidoscope":
        top_left = bgr[:h // 2, :w // 2]
        top      = np.hstack([top_left, cv2.flip(top_left, 1)])
        bottom   = cv2.flip(top, 0)
        return np.vstack([top, bottom])

    # ── All remaining effects use polar remap ─────────────────────────
    _, _, dx, dy, r, theta = _make_polar_maps(h, w)

    if effect_key == "fisheye":
        # Barrel distortion: compress centre outwards
        max_r  = min(cx, cy)
        r_norm = r / max_r
        r_new  = max_r * (r_norm ** 1.6)
        map_x  = (cx + r_new * np.cos(theta)).astype(np.float32)
        map_y  = (cy + r_new * np.sin(theta)).astype(np.float32)
        return _remap_effect(bgr, map_x, map_y)

    if effect_key == "bulge":
        # Pushes pixels outward from the centre
        max_r  = min(cx, cy)
        r_norm = np.clip(r / max_r, 0, 1)
        r_new  = max_r * np.sqrt(r_norm)
        map_x  = (cx + r_new * np.cos(theta)).astype(np.float32)
        map_y  = (cy + r_new * np.sin(theta)).astype(np.float32)
        return _remap_effect(bgr, map_x, map_y)

    if effect_key == "pinch":
        # Pulls pixels inward toward the centre
        max_r  = min(cx, cy)
        r_norm = np.clip(r / max_r, 0, 1)
        r_new  = max_r * (r_norm ** 2.0)
        map_x  = (cx + r_new * np.cos(theta)).astype(np.float32)
        map_y  = (cy + r_new * np.sin(theta)).astype(np.float32)
        return _remap_effect(bgr, map_x, map_y)

    if effect_key == "twist":
        # Rotates pixels by an angle proportional to their distance from centre
        max_r      = min(cx, cy)
        twist_amt  = 2.5   # radians at the centre
        angle      = twist_amt * (1.0 - np.clip(r / max_r, 0, 1))
        t_new      = theta + angle
        r_clamped  = np.clip(r, 0, max_r)
        map_x      = (cx + r_clamped * np.cos(t_new)).astype(np.float32)
        map_y      = (cy + r_clamped * np.sin(t_new)).astype(np.float32)
        return _remap_effect(bgr, map_x, map_y)

    if effect_key == "dent":
        # Horizontal sine-wave warp — looks like a dented mirror
        frequency  = 3.0
        amplitude  = h * 0.06
        map_x      = (np.mgrid[0:h, 0:w][1]).astype(np.float32)
        map_y      = (np.mgrid[0:h, 0:w][0]
                      + amplitude * np.sin(map_x / w * 2 * np.pi * frequency)
                      ).astype(np.float32)
        return _remap_effect(bgr, map_x, map_y)

    return bgr


# ---------------------------------------------------------------------------
# Caption helpers
# ---------------------------------------------------------------------------

def _load_pil_font(font_filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(WINDOWS_FONTS_DIR, font_filename)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _apply_caption(pil_img, text, font_name, font_size, position):
    """Render centred multi-line caption with strong outline onto the image."""
    if not text.strip():
        return pil_img

    img  = pil_img.copy().convert("RGBA")
    w, h = img.size

    font_file = FONTS.get(font_name, "arial.ttf")
    font      = _load_pil_font(font_file, font_size)

    # Measure the full multi-line block using align="center"
    dummy = ImageDraw.Draw(img)
    bbox  = dummy.multiline_textbbox((0, 0), text, font=font, align="center")
    tw    = bbox[2] - bbox[0]
    th    = bbox[3] - bbox[1]

    PADDING = 12
    pos = position.lower()

    # Horizontal: left/right/centre
    if "left" in pos:
        tx = PADDING
    elif "right" in pos:
        tx = w - tw - PADDING
    else:
        tx = (w - tw) // 2

    # Vertical: top/bottom/centre
    if "top" in pos:
        ty = PADDING
    elif "bottom" in pos:
        ty = h - th - PADDING * 4
    else:
        ty = (h - th) // 2

    stroke = int(min(font_size / 6, 8))
    draw   = ImageDraw.Draw(img)
    draw.multiline_text(
        (tx, ty), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 200),
        align="center",
    )

    return img.convert("RGB")


def _apply_countdown(pil_img, number):
    """
    Overlay a large countdown digit CENTRED on the image.
    Preview-only — never called before printing.
    Uses anchor='mm' (middle-middle) so Pillow centres the glyph perfectly.
    """
    img  = pil_img.copy().convert("RGBA")
    w, h = img.size
    cx, cy = w // 2, h // 2

    font_size = int(h * 0.52)
    font_path = os.path.join(WINDOWS_FONTS_DIR, "impact.ttf")
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()

    text = str(number)

    # Measure via textbbox with anchor "mm" to get the true visual extent
    dummy = ImageDraw.Draw(img)
    bbox  = dummy.textbbox((cx, cy), text, font=font, anchor="mm")
    r_pad = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) // 2 + 36

    # Dark circle behind digit
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(
        [cx - r_pad, cy - r_pad, cx + r_pad, cy + r_pad],
        fill=(0, 0, 0, 150),
    )
    img = Image.alpha_composite(img, overlay)

    # Draw digit centred using anchor "mm"
    stroke = int(min(font_size / 6, 14))
    ImageDraw.Draw(img).text(
        (cx, cy), text, font=font,
        anchor="mm",
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 220),
    )

    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Printer
# ---------------------------------------------------------------------------

def print_image(pil_image: Image.Image) -> None:
    """Send a PIL Image to the default Windows printer using GDI."""
    pil_image    = pil_image.rotate(-90, expand=True)
    printer_name = win32print.GetDefaultPrinter()
    hprinter     = win32print.OpenPrinter(printer_name)
    hdc          = None
    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        printer_x = hdc.GetDeviceCaps(win32con.HORZRES)
        printer_y = hdc.GetDeviceCaps(win32con.VERTRES)
        img_w, img_h = pil_image.size
        scale  = min(printer_x / img_w, printer_y / img_h)
        new_w  = int(img_w * scale)
        new_h  = int(img_h * scale)
        hdc.StartDoc("Photo Booth")
        hdc.StartPage()
        ImageWin.Dib(pil_image).draw(hdc.GetHandleOutput(), (0, 0, new_w, new_h))
        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()
    finally:
        win32print.ClosePrinter(hprinter)


# ---------------------------------------------------------------------------
# Background camera thread
# ---------------------------------------------------------------------------

class CameraThread(threading.Thread):
    """
    Continuously reads frames from the webcam in a background thread and
    puts them into a queue.  This decouples frame acquisition from the UI
    tick rate, keeping the preview smooth even during countdown waits.
    """
    def __init__(self, cap, frame_queue: queue.Queue):
        super().__init__(daemon=True)
        self.cap         = cap
        self.frame_queue = frame_queue
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            ret, frame = self.cap.read()
            if ret:
                # Keep only the latest frame — discard stale ones
                while not self.frame_queue.empty():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        break
                self.frame_queue.put(frame)
            time.sleep(0.01)   # ~100 fps cap, well above display rate

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
        self._sequence_active = False
        self._burst_remaining = 0
        self._sequence_after  = None
        # Current countdown digit — shared between timer tick and display loop
        self._countdown_overlay = None   # None = no overlay; int = show digit

        # ── Caption vars ──────────────────────────────────────────────
        self.caption_enabled  = tk.BooleanVar(value=True)
        self.caption_font     = tk.StringVar(value="Impact")
        self.caption_size     = tk.IntVar(value=72)
        self.caption_position = tk.StringVar(value="Bottom Centre")

        # ── Countdown & burst vars ────────────────────────────────────
        self.countdown_enabled = tk.BooleanVar(value=False)
        self.burst_enabled     = tk.BooleanVar(value=False)

        # ── Effect & mirror vars ──────────────────────────────────────
        self.effect_name   = tk.StringVar(value="None")
        self.mirror_enabled = tk.BooleanVar(value=True)

        # ── Save var ──────────────────────────────────────────────────
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
        content = self.entry_caption.get("1.0", "end-1c")
        if content.count("\n") >= 2:
            return "break"
        return None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        BG     = self.BG
        CARD   = self.CARD
        FG     = self.FG
        ACCENT = self.ACCENT
        RED    = self.RED
        GREEN  = self.GREEN
        MUTED  = self.MUTED
        IB     = self.INPUT_BG

        # ── Header ────────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=BG, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="📷  Photo Booth  📷",
                 font=("Segoe UI", 18, "bold"), fg=ACCENT, bg=BG).pack()

        # ── Camera canvas ─────────────────────────────────────────────
        canvas_frame = tk.Frame(self.root, bg=CARD, padx=4, pady=4)
        canvas_frame.pack(padx=20, pady=(0, 0))
        self.canvas = tk.Canvas(canvas_frame,
                                width=self.PREVIEW_W, height=self.PREVIEW_H,
                                bg="#11111b", highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_text(
            self.PREVIEW_W // 2, self.PREVIEW_H // 2,
            text="Connecting to camera…",
            fill=MUTED, font=("Segoe UI", 16), tag="placeholder",
        )

        # ── Style comboboxes once ─────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                         fieldbackground=IB, background=IB,
                         foreground=FG, selectbackground=IB,
                         selectforeground=FG, arrowcolor=FG)
        combo_cfg = dict(state="readonly", font=("Segoe UI", 10),
                         background=IB, foreground=CARD)

        # ── Controls panel ────────────────────────────────────────────
        cap_outer = tk.Frame(self.root, bg=CARD, padx=10, pady=6)
        cap_outer.pack(fill="x", padx=20, pady=(4, 0))

        #
        # Single row: [Caption chk] [Text entry] | [stacked selectors] | [toggles]
        #
        row0 = tk.Frame(cap_outer, bg=CARD)
        row0.pack(fill="x")

        # ── Left: Caption toggle + text entry ─────────────────────────
        left_col = tk.Frame(row0, bg=CARD)
        left_col.pack(side="left", anchor="n")

        self.chk_caption = tk.Checkbutton(
            left_col, text="Caption", variable=self.caption_enabled,
            font=("Segoe UI", 10, "bold"), fg=ACCENT, bg=CARD,
            activebackground=CARD, selectcolor=BG, cursor="hand2",
            command=self._refresh_live_preview,
        )
        self.chk_caption.pack(anchor="w")

        self.entry_caption = tk.Text(
            left_col,
            font=("Segoe UI", 11), bg=IB, fg=FG,
            insertbackground=FG, relief="flat", bd=4,
            height=3, width=32, wrap="word",
        )
        self.entry_caption.pack(anchor="w")
        self.entry_caption.bind("<KeyRelease>", self._on_caption_key)
        self.entry_caption.bind("<Return>",     self._on_caption_return)

        # ── Middle: vertically stacked Font / Size / Position ─────────
        mid_col = tk.Frame(row0, bg=CARD, padx=10)
        mid_col.pack(side="left", anchor="n")

        def sel_row(parent, label_text, widget_factory):
            r = tk.Frame(parent, bg=CARD, pady=2)
            r.pack(fill="x")
            tk.Label(r, text=label_text, font=("Segoe UI", 9),
                     fg=MUTED, bg=CARD, width=7, anchor="w").pack(side="left")
            widget_factory(r).pack(side="left")

        sel_row(mid_col, "Font",
                lambda p: self._make_combo(p, self.caption_font,
                                           sorted(FONTS.keys()), 14, combo_cfg))
        sel_row(mid_col, "Size",
                lambda p: self._make_combo(p, self.caption_size,
                                           FONT_SIZES, 5, combo_cfg))
        sel_row(mid_col, "Position",
                lambda p: self._make_combo(p, self.caption_position,
                                           CAPTION_POSITIONS, 14, combo_cfg))
        sel_row(mid_col, "Effect",
                lambda p: self._make_combo(p, self.effect_name,
                                           EFFECT_DISPLAY_NAMES, 14, combo_cfg))

        # ── Right: toggles ────────────────────────────────────────────
        right_col = tk.Frame(row0, bg=CARD, padx=6)
        right_col.pack(side="left", anchor="n")

        chk_cfg = dict(bg=CARD, activebackground=CARD, selectcolor=BG,
                       font=("Segoe UI", 10, "bold"), cursor="hand2",
                       anchor="w")

        tk.Checkbutton(
            right_col, text=f"⏱ Countdown ({COUNTDOWN_SECONDS}s)",
            variable=self.countdown_enabled, fg=GREEN, **chk_cfg,
        ).pack(fill="x", pady=1)

        tk.Checkbutton(
            right_col, text=f"💥 Burst ({BURST_COUNT} shots)",
            variable=self.burst_enabled, fg=ACCENT, **chk_cfg,
        ).pack(fill="x", pady=1)

        tk.Checkbutton(
            right_col, text="🪞 Mirror",
            variable=self.mirror_enabled, fg=FG,
            command=self._refresh_live_preview, **chk_cfg,
        ).pack(fill="x", pady=1)

        # ── Status bar ────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Starting camera…")
        tk.Label(self.root, textvariable=self.status_var,
                 font=("Segoe UI", 10), fg=FG, bg="#181825",
                 anchor="w", padx=10).pack(fill="x", pady=(4, 0))

        # ── Capture button ────────────────────────────────────────────
        btn_frame = tk.Frame(self.root, bg=BG, pady=10)
        btn_frame.pack()
        self.btn_capture = tk.Button(
            btn_frame, text="📸",
            bg=RED, fg=BG,
            font=("Segoe UI", 22), relief="flat",
            cursor="hand2", padx=5, pady=0, bd=0,
            command=self._on_capture_pressed,
        )
        self.btn_capture.pack()

        # ── Printer label ─────────────────────────────────────────────
        try:
            printer_name = win32print.GetDefaultPrinter()
        except Exception:
            printer_name = "Unknown"
        info_frame = tk.Frame(self.root, bg=BG, pady=2)
        info_frame.pack()
        tk.Label(info_frame, text=f"Default printer:  {printer_name}",
                 font=("Segoe UI", 9), fg=MUTED, bg=BG).pack()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _make_combo(self, parent, var, values, width, combo_cfg):
        cmb = ttk.Combobox(parent, textvariable=var, values=values,
                            width=width, **combo_cfg)
        cmb.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_preview())
        return cmb

    # ------------------------------------------------------------------
    # Frame composition
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
        Pipeline:
          1. Apply selected camera effect  (numpy/cv2)
          2. Mirror if enabled             (numpy)
          3. Convert to PIL RGB
          4. Apply caption                 (Pillow) — caption never affected by effects
          5. Optionally overlay countdown digit (Pillow, preview only)
        """
        # 1. Effect
        effect_key = EFFECT_KEYS.get(self.effect_name.get(), "none")
        frame = apply_effect(bgr_frame, effect_key)

        # 2. Mirror
        if self.mirror_enabled.get():
            frame = cv2.flip(frame, 1)

        # 3. PIL
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)

        # 4. Caption
        p = self._caption_params()
        if p["text"].strip():
            pil = _apply_caption(pil, **p)

        # 5. Countdown digit (preview only — caller decides)
        if countdown_num is not None:
            pil = _apply_countdown(pil, countdown_num)

        return pil

    def _refresh_live_preview(self):
        frame = self.frozen_frame if self.preview_frozen else self.last_frame
        if frame is not None:
            self._display_pil(self._compose_frame(frame))

    # ------------------------------------------------------------------
    # Camera — background thread feeds a queue; UI polls the queue
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
        """
        UI polling loop — runs at ~30 fps regardless of countdown state.
        During a sequence it still reads the queue so the preview stays live;
        it just also overlays the countdown digit if one is pending.
        """
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
                pass   # nothing new from camera yet — keep old frame on screen

        self._after_id = self.root.after(33, self._schedule_frame)

    def _display_pil(self, pil_img: Image.Image) -> None:
        w, h   = pil_img.size
        scale  = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = int(w * scale), int(h * scale)
        preview = pil_img.resize((nw, nh), Image.LANCZOS)
        tk_img  = ImageTk.PhotoImage(preview)
        self.canvas._tk_img = tk_img
        self.canvas.delete("all")
        self.canvas.create_image(
            self.PREVIEW_W // 2, self.PREVIEW_H // 2,
            anchor="center", image=tk_img,
        )

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

        # Set the shared overlay value — _schedule_frame will pick it up
        self._countdown_overlay = seconds_left
        self.status_var.set(f"⏱  Get ready… {seconds_left}")
        self._sequence_after = self.root.after(
            1000, lambda: self._run_countdown(seconds_left - 1)
        )

    def _run_inter_burst_countdown(self, seconds_left: int) -> None:
        if seconds_left <= 0:
            self._countdown_overlay = None
            self._do_capture_and_print()
            return

        self._countdown_overlay = seconds_left
        self.status_var.set(f"💥  Next burst shot in {seconds_left}…")
        self._sequence_after = self.root.after(
            1000, lambda: self._run_inter_burst_countdown(seconds_left - 1)
        )

    def _do_capture_and_print(self) -> None:
        # Grab the very latest frame from the queue (don't use a stale one)
        try:
            frame = self._frame_queue.get_nowait()
            self.last_frame = frame
        except queue.Empty:
            frame = self.last_frame

        if frame is None:
            self._end_sequence()
            return

        self.frozen_frame = frame.copy()

        # Compose WITHOUT countdown overlay — this is what gets printed
        composited = self._compose_frame(frame, countdown_num=None)
        self.preview_frozen = True
        self._display_pil(composited)

        shot_num   = (BURST_COUNT - self._burst_remaining + 1) if self.burst_enabled.get() else 1
        burst_info = f" ({shot_num}/{BURST_COUNT})" if self.burst_enabled.get() else ""
        self.status_var.set(f"📸  Captured{burst_info} — sending to printer…")

        if self.save_enabled.get():
            try:
                cv2.imwrite(
                    datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg",
                    self.frozen_frame,
                )
            except Exception:
                pass

        self._burst_remaining -= 1

        threading.Thread(
            target=self._do_print,
            args=(composited.copy(), self._burst_remaining),
            daemon=True,
        ).start()

    def _do_print(self, pil_image: Image.Image, burst_remaining: int) -> None:
        try:
            print_image(pil_image)
            self.root.after(0, lambda: self._after_print(burst_remaining))
        except Exception as exc:
            self.root.after(0, lambda: self._print_error(str(exc)))

    def _after_print(self, burst_remaining: int) -> None:
        self.preview_frozen = False
        if burst_remaining > 0:
            self._run_inter_burst_countdown(BURST_DELAY)
        else:
            shots = BURST_COUNT if self.burst_enabled.get() else 1
            msg   = (f"✅  {shots} prints sent — ready for next round!"
                     if shots > 1 else
                     "✅  Print job sent — ready for next shot!")
            self.status_var.set(msg)
            self._end_sequence()

    def _end_sequence(self) -> None:
        self._sequence_active   = False
        self._countdown_overlay = None
        self.preview_frozen     = False
        self.frozen_frame       = None
        self.btn_capture.config(state="normal")

    def _print_error(self, msg: str) -> None:
        self.status_var.set(f"❌  Print failed: {msg}")
        messagebox.showerror("Print Error", f"Could not print:\n\n{msg}")
        self._end_sequence()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
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

def main() -> None:
    root = tk.Tk()
    WebcamPrintApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()