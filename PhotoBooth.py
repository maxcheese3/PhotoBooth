"""
Photo Booth Application
-----------------------
Captures images from a webcam, overlays an optional caption, and sends
the result directly to the default Windows printer via the GDI API.

Requirements (install with pip):
    pip install opencv-python pillow pywin32

Run:
    python PhotoBooth.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import os
from datetime import datetime

import cv2
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

COUNTDOWN_SECONDS = 3    # seconds before capture when countdown enabled
BURST_COUNT       = 3    # number of shots in a burst
BURST_DELAY       = 2    # seconds between burst shots


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
    """Render multi-line caption text onto the image with a strong outline."""
    if not text.strip():
        return pil_img

    img  = pil_img.copy().convert("RGBA")
    w, h = img.size

    font_file = FONTS.get(font_name, "arial.ttf")
    font      = _load_pil_font(font_file, font_size)

    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font, anchor="mm", align="center")
    tw   = bbox[2] - bbox[0]
    th   = bbox[3] - bbox[1]

    PADDING = 12
    pos = position.lower()

    tx = PADDING if "left" in pos else (w - tw - PADDING if "right" in pos else (w - tw) // 2)
    ty = PADDING if "top"  in pos else (h - th - PADDING * 4 if "bottom" in pos else (h - th) // 2)

    stroke = int(min(font_size / 6, 8))
    draw.text(
        (tx, ty), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 200),
    )

    return img.convert("RGB")


def _apply_countdown(pil_img, number):
    """
    Overlay a large countdown digit centred on the image.
    Used ONLY for the on-screen preview — never called before printing.
    """
    img  = pil_img.copy().convert("RGBA")
    w, h = img.size

    # Try to load a bold font at a very large size
    font_path = os.path.join(WINDOWS_FONTS_DIR, "impact.ttf")
    try:
        font = ImageFont.truetype(font_path, int(h * 0.55))
    except Exception:
        font = ImageFont.load_default()

    text = str(number)
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (w - tw) // 2
    ty = (h - th) // 2

    # Semi-transparent dark circle behind the number
    circle_r = max(tw, th) // 2 + 30
    cx, cy   = w // 2, h // 2
    overlay  = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ov_draw  = ImageDraw.Draw(overlay)
    ov_draw.ellipse(
        [cx - circle_r, cy - circle_r, cx + circle_r, cy + circle_r],
        fill=(0, 0, 0, 140),
    )
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)
    stroke = int(min(int(h * 0.55) / 6, 12))
    draw.text(
        (tx, ty), text, font=font,
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
        self.running        = False
        self.last_frame     = None
        self.frozen_frame   = None
        self.preview_frozen = False
        self._after_id      = None

        # Sequence state
        self._sequence_active  = False   # True during countdown / burst
        self._countdown_val    = 0       # current countdown digit shown on screen
        self._burst_remaining  = 0       # shots left in current burst
        self._sequence_after   = None    # handle for pending after() call

        # ── Caption vars ──────────────────────────────────────────────
        self.caption_enabled  = tk.BooleanVar(value=True)
        self.caption_font     = tk.StringVar(value="Impact")
        self.caption_size     = tk.IntVar(value=72)
        self.caption_position = tk.StringVar(value="Bottom Centre")

        # ── Countdown & burst vars ────────────────────────────────────
        self.countdown_enabled = tk.BooleanVar(value=False)
        self.burst_enabled     = tk.BooleanVar(value=False)

        # ── Extra feature vars ────────────────────────────────────────
        self.mirror_enabled    = tk.BooleanVar(value=True)   # mirror live preview
        self.save_enabled      = tk.BooleanVar(value=True)   # save JPEGs to disk

        self._build_ui()
        self._start_camera()

    # ------------------------------------------------------------------
    # Caption text helper  (tk.Text widget — no StringVar)
    # ------------------------------------------------------------------

    def _get_caption_text(self) -> str:
        """Return current caption text from the Text widget (stripped trailing newline)."""
        return self.entry_caption.get("1.0", "end-1c")

    def _on_caption_key(self, event=None):
        """Enforce 3-line limit and trigger live preview refresh."""
        # Count lines
        content = self.entry_caption.get("1.0", "end-1c")
        lines   = content.split("\n")
        if len(lines) > 3:
            # Remove the character that pushed past 3 lines
            self.entry_caption.delete("1.0", "end")
            self.entry_caption.insert("1.0", "\n".join(lines[:3]))
            # Move cursor to end
            self.entry_caption.mark_set("insert", "end-1c")
        self._refresh_live_preview()
        # Allow the event to propagate normally (return None)

    def _on_caption_return(self, event=None):
        """On Enter key: insert newline only if fewer than 3 lines exist."""
        content = self.entry_caption.get("1.0", "end-1c")
        if content.count("\n") >= 2:
            return "break"   # suppress the keystroke
        # Allow default newline insertion
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

        # ── Caption + controls panel ───────────────────────────────────
        cap_outer = tk.Frame(self.root, bg=CARD, padx=10, pady=6)
        cap_outer.pack(fill="x", padx=20, pady=(4, 0))

        # Row 0 — Caption toggle + multi-line text entry
        row0 = tk.Frame(cap_outer, bg=CARD)
        row0.pack(fill="x")

        self.chk_caption = tk.Checkbutton(
            row0, text="Caption", variable=self.caption_enabled,
            font=("Segoe UI", 10, "bold"), fg=ACCENT, bg=CARD,
            activebackground=CARD, selectcolor=BG, cursor="hand2",
            command=self._refresh_live_preview,
        )
        self.chk_caption.pack(side="left", padx=(0, 8))

        # Text widget — 3 rows tall, fixed height
        self.entry_caption = tk.Text(
            row0,
            font=("Segoe UI", 11), bg=IB, fg=FG,
            insertbackground=FG, relief="flat", bd=4,
            height=3, width=32,
            wrap="word",
        )
        self.entry_caption.pack(side="left")
        self.entry_caption.bind("<KeyRelease>", self._on_caption_key)
        self.entry_caption.bind("<Return>",     self._on_caption_return)

        # Row 1 — Font / Size / Position / Countdown / Burst
        row1 = tk.Frame(cap_outer, bg=CARD, pady=4)
        row1.pack(fill="x")

        def lbl(parent, text):
            tk.Label(parent, text=text,
                     font=("Segoe UI", 9), fg=MUTED, bg=CARD,
                     ).pack(side="left", padx=(8, 2))

        # Style comboboxes
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                         fieldbackground=IB, background=IB,
                         foreground=FG, selectbackground=IB,
                         selectforeground=FG, arrowcolor=FG)

        combo_cfg = dict(state="readonly", font=("Segoe UI", 10),
                         background=IB, foreground=CARD)

        lbl(row1, "Font")
        self.cmb_font = ttk.Combobox(
            row1, textvariable=self.caption_font,
            values=sorted(FONTS.keys()), width=14, **combo_cfg)
        self.cmb_font.pack(side="left")
        self.cmb_font.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_preview())

        lbl(row1, "Size")
        self.cmb_size = ttk.Combobox(
            row1, textvariable=self.caption_size,
            values=FONT_SIZES, width=5, **combo_cfg)
        self.cmb_size.pack(side="left")
        self.cmb_size.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_preview())

        lbl(row1, "Position")
        self.cmb_pos = ttk.Combobox(
            row1, textvariable=self.caption_position,
            values=CAPTION_POSITIONS, width=14, **combo_cfg)
        self.cmb_pos.pack(side="left")
        self.cmb_pos.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_preview())

        # Separator
        tk.Frame(row1, bg=MUTED, width=1, height=22).pack(side="left", padx=10)

        # Countdown toggle
        chk_cfg = dict(bg=CARD, activebackground=CARD, selectcolor=BG,
                       font=("Segoe UI", 10, "bold"), cursor="hand2")
        tk.Checkbutton(
            row1, text=f"⏱ Countdown ({COUNTDOWN_SECONDS}s)",
            variable=self.countdown_enabled,
            fg=GREEN, **chk_cfg,
        ).pack(side="left", padx=(4, 0))

        # Burst toggle
        tk.Checkbutton(
            row1, text=f"💥 Burst ({BURST_COUNT} shots)",
            variable=self.burst_enabled,
            fg=ACCENT, **chk_cfg,
        ).pack(side="left", padx=(12, 0))

        # Row 2 — Extra options (mirror, save)
        #row1 = tk.Frame(cap_outer, bg=CARD, pady=2)
        #row1.pack(fill="x")

        tk.Checkbutton(
            row1, text="🪞 Mirror preview",
            variable=self.mirror_enabled,
            fg=FG, **chk_cfg,
            command=self._refresh_live_preview,
        ).pack(side="left", padx=(0, 0))

        # tk.Checkbutton(
        #     row2, text="💾 Save photos to disk",
        #     variable=self.save_enabled,
        #     fg=FG, **chk_cfg,
        # ).pack(side="left", padx=(16, 0))

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

    # ------------------------------------------------------------------
    # Caption / frame composition
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
        Convert BGR → PIL RGB, optionally mirror, apply caption.
        If countdown_num is not None, overlay the countdown digit (preview only).
        """
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)

        if self.mirror_enabled.get():
            pil = pil.transpose(Image.FLIP_LEFT_RIGHT)

        p = self._caption_params()
        if p["text"].strip():
            pil = _apply_caption(pil, **p)

        # Countdown overlay — preview only, never baked into the printed image
        if countdown_num is not None:
            pil = _apply_countdown(pil, countdown_num)

        return pil

    def _refresh_live_preview(self):
        """Force-redraw the canvas with the latest frame (live or frozen)."""
        frame = self.frozen_frame if self.preview_frozen else self.last_frame
        if frame is not None:
            self._display_pil(self._compose_frame(frame))

    # ------------------------------------------------------------------
    # Camera
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
        self.cap     = cap
        self.running = True
        self.root.after(0, self._schedule_frame)
        self.root.after(0, lambda: self.status_var.set(
            "✅  Camera ready — press 📸 to capture & print"))

    def _schedule_frame(self) -> None:
        if not self.running:
            return
        if not self.preview_frozen and not self._sequence_active:
            self._read_frame()
        self._after_id = self.root.after(33, self._schedule_frame)

    def _read_frame(self) -> None:
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        self.last_frame = frame
        self._display_pil(self._compose_frame(frame))

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
    # Capture / countdown / burst logic
    # ------------------------------------------------------------------

    def _on_capture_pressed(self) -> None:
        if self.last_frame is None:
            messagebox.showwarning("No frame", "Camera is not ready yet.")
            return
        if self._sequence_active:
            return  # ignore if already running

        self._sequence_active = True
        self.btn_capture.config(state="disabled")

        if self.countdown_enabled.get():
            self._burst_remaining = BURST_COUNT if self.burst_enabled.get() else 1
            self._run_countdown(COUNTDOWN_SECONDS)
        else:
            self._burst_remaining = BURST_COUNT if self.burst_enabled.get() else 1
            self._do_capture_and_print()

    def _run_countdown(self, seconds_left: int) -> None:
        """Display countdown on the live feed, then fire capture when done."""
        if seconds_left <= 0:
            self._do_capture_and_print()
            return

        self._countdown_val = seconds_left

        # Grab the latest live frame and overlay the digit
        if self.cap is not None:
            ret, frame = self.cap.read()
            if ret:
                self.last_frame = frame
                preview = self._compose_frame(frame, countdown_num=seconds_left)
                self._display_pil(preview)

        self.status_var.set(f"⏱  Get ready… {seconds_left}")
        self._sequence_after = self.root.after(
            1000, lambda: self._run_countdown(seconds_left - 1)
        )

    def _do_capture_and_print(self) -> None:
        """Freeze the current frame, composite it, save & print."""
        if self.cap is None:
            self._end_sequence()
            return

        ret, frame = self.cap.read()
        if not ret:
            frame = self.last_frame
        if frame is None:
            self._end_sequence()
            return

        self.last_frame   = frame
        self.frozen_frame = frame.copy()

        # Compose WITHOUT countdown overlay — this is the printable image
        composited = self._compose_frame(frame)
        self._display_pil(composited)

        shot_num   = (BURST_COUNT - self._burst_remaining + 1) if self.burst_enabled.get() else 1
        burst_info = f" ({shot_num}/{BURST_COUNT})" if self.burst_enabled.get() else ""
        self.status_var.set(f"📸  Captured{burst_info} — sending to printer…")

        # Save to disk
        if self.save_enabled.get():
            try:
                cv2.imwrite(
                    datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg",
                    self.frozen_frame,
                )
            except Exception:
                pass

        self._burst_remaining -= 1

        # Print in background; schedule next burst shot if needed
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
        if burst_remaining > 0:
            # More burst shots to take — show inter-shot countdown
            self.preview_frozen = False
            self._run_inter_burst_countdown(BURST_DELAY, burst_remaining)
        else:
            shots = BURST_COUNT if self.burst_enabled.get() else 1
            msg   = f"✅  {shots} print job(s) sent — ready for next round!" if shots > 1 else "✅  Print job sent — ready for next shot!"
            self.status_var.set(msg)
            self._end_sequence()

    def _run_inter_burst_countdown(self, seconds_left: int, burst_remaining: int) -> None:
        """Show a countdown between burst shots."""
        if seconds_left <= 0:
            self._do_capture_and_print()
            return

        if self.cap is not None:
            ret, frame = self.cap.read()
            if ret:
                self.last_frame = frame
                preview = self._compose_frame(frame, countdown_num=seconds_left)
                self._display_pil(preview)

        self.status_var.set(f"💥  Next burst shot in {seconds_left}…")
        self._sequence_after = self.root.after(
            1000,
            lambda: self._run_inter_burst_countdown(seconds_left - 1, burst_remaining),
        )

    def _end_sequence(self) -> None:
        self._sequence_active = False
        self.preview_frozen   = False
        self.frozen_frame     = None
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