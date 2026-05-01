"""
Photo Booth Application
-----------------------
Captures images from a webcam, overlays an optional caption, and sends
the result directly to the default Windows printer via the GDI API.

Requirements (install with pip):
    pip install opencv-python pillow pywin32

Run:
    python webcam_print.py
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
# Font catalogue  (Windows font filenames → friendly display name)
# All live in C:\Windows\Fonts on any standard Windows install.
# ---------------------------------------------------------------------------

FONTS = {
    "Arial":            "arial.ttf",
    "Arial Bold":       "arialbd.ttf",
    "Comic Sans":       "comic.ttf",
    "Courier New":      "cour.ttf",
    "Georgia":          "georgia.ttf",
    "Impact":           "impact.ttf",
    "Ink Free":         "Inkfree.ttf",
    "Segoe UI":         "segoeui.ttf",
    "Segoe Script":     "segoesc.ttf",
    "Times New Roman":  "times.ttf",
    "Trebuchet MS":     "trebuc.ttf",
    "Verdana":          "verdana.ttf",
}

CAPTION_POSITIONS = ["Bottom Centre", "Bottom Left", "Bottom Right",
                     "Top Centre",    "Top Left",    "Top Right",
                     "Centre"]

FONT_SIZES = [18, 24, 32, 40, 48, 60, 72, 90]

WINDOWS_FONTS_DIR = r"C:\Windows\Fonts"


def _load_pil_font(font_filename: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a TTF from the Windows fonts directory, fall back to default."""
    path = os.path.join(WINDOWS_FONTS_DIR, font_filename)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _apply_caption(
    pil_img: Image.Image,
    text: str,
    font_name: str,
    font_size: int,
    position: str,
) -> Image.Image:
    """
    Draw `text` onto a copy of `pil_img` and return the result.
    A semi-transparent dark band is drawn behind the text for readability.
    """
    if not text.strip():
        return pil_img

    img = pil_img.copy().convert("RGBA")
    w, h = img.size

    font_file = FONTS.get(font_name, "arial.ttf")
    font = _load_pil_font(font_file, font_size)

    # Measure text
    dummy = ImageDraw.Draw(img)
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    PADDING = 12
    BAND_H  = th + PADDING * 2

    # Resolve (tx, ty) = top-left corner of the text
    pos = position.lower()
    if "left" in pos:
        tx = PADDING
    elif "right" in pos:
        tx = w - tw - PADDING
    else:                       # centre
        tx = (w - tw) // 2

    if "top" in pos:
        ty = PADDING
    elif "bottom" in pos:
        ty = h - th - PADDING * 2
    else:                       # centre
        ty = (h - th) // 2

    # Band bounding box
    if "top" in pos:
        band_y0, band_y1 = 0, BAND_H
    elif "bottom" in pos:
        band_y0, band_y1 = h - BAND_H, h
    else:
        band_y0 = ty - PADDING
        band_y1 = ty + th + PADDING

    # Draw semi-transparent band
    # overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    # ov_draw = ImageDraw.Draw(overlay)
    # ov_draw.rectangle([0, band_y0, w, band_y1], fill=(0, 0, 0, 160))
    # img = Image.alpha_composite(img, overlay)

    # Draw text with a subtle drop-shadow
    draw = ImageDraw.Draw(img)
    #draw.text((tx + 5, ty + 5), text, font=font, fill=(0, 0, 0, 200))   # shadow
    draw.text((tx,     ty),     text, font=font, fill=(255, 255, 255, 255), stroke_width=min(font_size/6,8), stroke_fill=(0,0,0,200))

    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Printer  (fresh-DEVMODE approach to prevent blank/good alternation)
# ---------------------------------------------------------------------------

# def _get_fresh_devmode(printer_name: str, hprinter) -> bytes:
#     DM_OUT_BUFFER = 2
#     size = win32print.DocumentProperties(0, hprinter, printer_name, None, None, 0)
#     buf  = b"\x00" * size
#     win32print.DocumentProperties(0, hprinter, printer_name, buf, None, DM_OUT_BUFFER)
#     return buf


def print_image(pil_image: Image.Image) -> None:
    """Send a PIL Image to the default Windows printer using GDI."""

    # Rotate 90 degrees clockwise before printing
    pil_image = pil_image.rotate(-90, expand=True)

    printer_name = win32print.GetDefaultPrinter()

    # Open printer and start a document
    hprinter = win32print.OpenPrinter(printer_name)
    hdc = None
    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)

        # Get printable area in device units
        printer_x = hdc.GetDeviceCaps(win32con.HORZRES)   # printable width  (px)
        printer_y = hdc.GetDeviceCaps(win32con.VERTRES)   # printable height (px)

        # Get physical page size and printable origin to detect unprintable margins
        phys_x = hdc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        phys_y = hdc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        offset_x = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETY)

        # Scale image to fill the printable width, preserving aspect ratio
        img_w, img_h = pil_image.size
        scale = min(printer_x / img_w, printer_y / img_h)
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)

        # Anchor to top-left of printable area (offset_x/y already accounted
        # for by the printer DC coordinate system, so start at 0, 0)
        x_offset = 0
        y_offset = 0

        hdc.StartDoc("Webcam Capture")
        hdc.StartPage()

        # Blit the image onto the printer DC
        dib = ImageWin.Dib(pil_image)
        dib.draw(
            hdc.GetHandleOutput(),
            (x_offset, y_offset, x_offset + new_w, y_offset + new_h),
        )

        hdc.EndPage()
        hdc.EndDoc()
        #hdc.AbortDoc()
        hdc.DeleteDC()
    finally:
        #win32print.EndDocPrinter(hprinter)
        win32print.ClosePrinter(hprinter)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class WebcamPrintApp:
    PREVIEW_W = 1280
    PREVIEW_H = 720

    # UI colours
    BG     = "#1e1e2e"
    CARD   = "#313244"
    FG     = "#cdd6f4"
    ACCENT = "#cba6f7"
    RED    = "#f38ba8"
    MUTED  = "#585b70"
    INPUT_BG = "#45475a"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Booth")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.cap              = None
        self.running          = False
        self.last_frame       = None
        self.frozen_frame     = None
        self.preview_frozen   = False
        self._after_id        = None

        # Caption state
        self.caption_enabled  = tk.BooleanVar(value=True)
        self.caption_text     = tk.StringVar(value="")
        self.caption_font     = tk.StringVar(value="Impact")
        self.caption_size     = tk.IntVar(value=48)
        self.caption_position = tk.StringVar(value="Bottom Centre")

        # Trace caption controls → redraw preview immediately
        for var in (self.caption_text, self.caption_font,
                    self.caption_size, self.caption_position,
                    self.caption_enabled):
            var.trace_add("write", lambda *_: self._refresh_frozen_preview())

        self._build_ui()
        self._start_camera()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        BG     = self.BG
        CARD   = self.CARD
        FG     = self.FG
        ACCENT = self.ACCENT
        RED    = self.RED
        MUTED  = self.MUTED
        IB     = self.INPUT_BG

        # ── Header ───────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=BG, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="📷  Photo Booth  📷",
                 font=("Segoe UI", 18, "bold"), fg=ACCENT, bg=BG).pack()

        # ── Camera canvas ────────────────────────────────────────────
        canvas_frame = tk.Frame(self.root, bg=CARD, padx=4, pady=4)
        canvas_frame.pack(padx=20, pady=(0, 0))

        self.canvas = tk.Canvas(canvas_frame,
                                width=self.PREVIEW_W, height=self.PREVIEW_H,
                                bg="#11111b", highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_text(self.PREVIEW_W // 2, self.PREVIEW_H // 2,
                                text="Connecting to camera…",
                                fill=MUTED, font=("Segoe UI", 16),
                                tag="placeholder")

        # ── Caption panel ─────────────────────────────────────────────
        cap_outer = tk.Frame(self.root, bg=CARD, padx=10, pady=8)
        cap_outer.pack(fill="x", padx=20, pady=(4, 0))

        # Row 0 — toggle + text entry
        row0 = tk.Frame(cap_outer, bg=CARD)
        row0.pack(fill="x")

        self.chk_caption = tk.Checkbutton(
            row0, text="Caption", variable=self.caption_enabled,
            font=("Segoe UI", 10, "bold"), fg=ACCENT, bg=CARD,
            activebackground=CARD, selectcolor=BG,
            cursor="hand2"
        )
        self.chk_caption.pack(side="left", padx=(0, 8))

        self.entry_caption = tk.Entry(
            row0, textvariable=self.caption_text,
            font=("Segoe UI", 13), bg=IB, fg=FG,
            insertbackground=FG, relief="flat", bd=4
        )
        self.entry_caption.pack(side="left", fill="x", expand=True)

        # Row 1 — font / size / position selectors
        row1 = tk.Frame(cap_outer, bg=CARD, pady=6)
        row1.pack(fill="x")

        def lbl(parent, text):
            tk.Label(parent, text=text,
                     font=("Segoe UI", 9), fg=MUTED, bg=CARD).pack(side="left", padx=(8, 2))

        combo_cfg = dict(state="readonly", font=("Segoe UI", 10),
                         background=IB, foreground=CARD)

        lbl(row1, "Font")
        self.cmb_font = ttk.Combobox(row1, textvariable=self.caption_font,
                                     values=sorted(FONTS.keys()),
                                     width=16, **combo_cfg)
        self.cmb_font.pack(side="left")

        lbl(row1, "Size")
        self.cmb_size = ttk.Combobox(row1, textvariable=self.caption_size,
                                     values=FONT_SIZES,
                                     width=5, **combo_cfg)
        self.cmb_size.pack(side="left")

        lbl(row1, "Position")
        self.cmb_pos = ttk.Combobox(row1, textvariable=self.caption_position,
                                    values=CAPTION_POSITIONS,
                                    width=14, **combo_cfg)
        self.cmb_pos.pack(side="left")

        # Style the ttk comboboxes to match dark theme
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                         fieldbackground=IB, background=IB,
                         foreground=FG, selectbackground=IB,
                         selectforeground=FG, arrowcolor=FG)

        # ── Status bar ───────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Starting camera…")
        tk.Label(self.root, textvariable=self.status_var,
                 font=("Segoe UI", 10), fg=FG, bg="#181825",
                 anchor="w", padx=10).pack(fill="x", pady=(4, 0))

        # ── Capture button ───────────────────────────────────────────
        btn_frame = tk.Frame(self.root, bg=BG, pady=12)
        btn_frame.pack()

        self.btn_capture = tk.Button(
            btn_frame, text="📸",
            bg=RED, fg=BG,
            font=("Segoe UI", 22), relief="flat",
            cursor="hand2", padx=5, pady=0, bd=0,
            command=self._capture
        )
        self.btn_capture.pack()

        # ── Printer label ─────────────────────────────────────────────
        try:
            printer_name = win32print.GetDefaultPrinter()
        except Exception:
            printer_name = "Unknown"

        info_frame = tk.Frame(self.root, bg=BG, pady=4)
        info_frame.pack()
        tk.Label(info_frame, text=f"Default printer:  {printer_name}",
                 font=("Segoe UI", 9), fg=MUTED, bg=BG).pack()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Caption rendering helpers
    # ------------------------------------------------------------------

    def _caption_params(self):
        """Return current caption settings as a dict."""
        return dict(
            text     = self.caption_text.get()     if self.caption_enabled.get() else "",
            font_name= self.caption_font.get(),
            font_size= int(self.caption_size.get()),
            position = self.caption_position.get(),
        )

    def _frame_with_caption(self, bgr_frame) -> Image.Image:
        """
        Convert a raw BGR camera frame to a PIL RGB image, flip it (mirror),
        then overlay the caption if enabled.  Returns the composited PIL image
        at native camera resolution (for printing).
        """
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).transpose(Image.FLIP_LEFT_RIGHT)
        p   = self._caption_params()
        if p["text"].strip():
            pil = _apply_caption(pil, **p)
        return pil

    def _refresh_frozen_preview(self) -> None:
        """When caption controls change while preview is frozen, redraw canvas."""
        if self.preview_frozen and self.frozen_frame is not None:
            self._display_pil(self._frame_with_caption(self.frozen_frame))

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
        self.root.after(0, lambda: self.status_var.set("✅  Camera ready — press 📸 to capture & print"))

    def _schedule_frame(self) -> None:
        if not self.running:
            return
        if not self.preview_frozen:
            self._read_frame()
        self._after_id = self.root.after(33, self._schedule_frame)

    def _read_frame(self) -> None:
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        self.last_frame = frame
        # Show live frame with live caption overlay
        self._display_pil(self._frame_with_caption(frame))

    def _display_pil(self, pil_img: Image.Image) -> None:
        """Resize a PIL image to fit the canvas and draw it."""
        w, h = pil_img.size
        scale = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = int(w * scale), int(h * scale)
        preview = pil_img.resize((nw, nh), Image.LANCZOS)
        tk_img  = ImageTk.PhotoImage(preview)

        self.canvas._tk_img = tk_img           # keep reference
        self.canvas.delete("all")
        self.canvas.create_image(self.PREVIEW_W // 2, self.PREVIEW_H // 2,
                                 anchor="center", image=tk_img)

    # ------------------------------------------------------------------
    # Capture → Print (single action)
    # ------------------------------------------------------------------

    def _capture(self) -> None:
        if self.last_frame is None:
            messagebox.showwarning("No frame", "Camera is not ready yet.")
            return

        self.frozen_frame   = self.last_frame.copy()
        self.preview_frozen = True

        # Build the final composited image (full resolution, with caption)
        composited = self._frame_with_caption(self.frozen_frame)

        # Show it on canvas
        self._display_pil(composited)
        self.status_var.set("📸  Captured — sending to printer…")

        # Save JPEG snapshot (raw frame, no caption)
        try:
            cv2.imwrite(datetime.now().strftime("%Y%m%d-%H%M%S") + ".jpg",
                        self.frozen_frame)
        except Exception:
            pass

        # Print in background
        threading.Thread(target=self._do_print,
                         args=(composited.copy(),), daemon=True).start()

    def _do_print(self, pil_image: Image.Image) -> None:
        try:
            print_image(pil_image)
            self.root.after(0, self._print_success)
        except Exception as exc:
            self.root.after(0, lambda: self._print_error(str(exc)))

    def _print_success(self) -> None:
        self.status_var.set("✅  Print job sent — ready for next shot!")
        # Resume live preview; caption text intentionally left intact
        self.frozen_frame   = None
        self.preview_frozen = False

    def _print_error(self, msg: str) -> None:
        self.status_var.set(f"❌  Print failed: {msg}")
        messagebox.showerror("Print Error", f"Could not print:\n\n{msg}")
        self.frozen_frame   = None
        self.preview_frozen = False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self.running = False
        if self._after_id:
            self.root.after_cancel(self._after_id)
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