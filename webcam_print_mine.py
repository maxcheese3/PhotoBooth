"""
Webcam Capture & Print Application
-----------------------------------
Captures images from a webcam and sends them directly to the default
Windows printer using the Windows GDI printing API (win32print / win32ui).

Requirements (install with pip):
    pip install opencv-python pillow pywin32

Run:
    python webcam_print.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import io

import cv2
from PIL import Image, ImageTk

# Windows printing via pywin32
import win32print
import win32ui
import win32con
from PIL import ImageWin
from datetime import datetime


# ---------------------------------------------------------------------------
# Printer helper
# ---------------------------------------------------------------------------

def print_image(pil_image: Image.Image) -> None:
    """Send a PIL Image to the default Windows printer using GDI."""

    # Rotate 90 degrees clockwise before printing
    pil_image = pil_image.rotate(-90, expand=True)

    printer_name = win32print.GetDefaultPrinter()

    # Open printer and start a document
    hprinter = win32print.OpenPrinter(printer_name)
    hdc = None
    try:

        #devmode_buf = _get_fresh_devmode(printer_name, hprinter)
 
        # 2. Create the printer DC with that DEVMODE explicitly supplied.
        #    win32gui.CreateDC(driver, device, output, initData)
        #hdc_handle = win32gui.CreateDC("WINSPOOL", printer_name, None, devmode_buf)
        #if not hdc_handle:
        #    raise RuntimeError("CreateDC returned NULL - cannot open printer DC.")

        #hdc = win32ui.CreateDCFromHandle(hdc_handle)
        hdc = win32ui.CreateDC()
        #hdc.DeleteDC()
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

def _get_fresh_devmode(printer_name: str, hprinter) -> bytes:
    """
    Call DocumentProperties twice:
      - first pass (size=0) returns the required buffer size
      - second pass (DM_OUT_BUFFER=2) fills the buffer with the printer's
        current, fully-initialised DEVMODE
 
    This guarantees we never hand a stale / zero-filled DEVMODE to CreateDC,
    which is the root cause of the alternating blank/good page bug.
    """
    DM_OUT_BUFFER = 2
    size = win32print.DocumentProperties(
        0, hprinter, printer_name, None, None, 0
    )
    devmode_buf = b"\x00" * size
    win32print.DocumentProperties(
        0, hprinter, printer_name, devmode_buf, None, DM_OUT_BUFFER
    )
    return devmode_buf

# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class WebcamPrintApp:
    PREVIEW_W = 1280
    PREVIEW_H = 720

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Booth")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")

        self.cap: cv2.VideoCapture | None = None
        self.running = False
        self.last_frame = None           # raw BGR frame from OpenCV
        self.frozen_frame = None         # BGR frame that was captured
        self.preview_frozen = False      # True while showing captured still
        self._after_id = None

        self._build_ui()
        self._start_camera()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ACCENT   = "#cba6f7"   # lavender purple
        BG       = "#1e1e2e"
        CARD     = "#313244"
        FG       = "#cdd6f4"
        BTN_CAP  = "#a6e3a1"   # green
        BTN_PRT  = "#89b4fa"   # blue
        BTN_RES  = "#f38ba8"   # red

        # ---- header ----
        header = tk.Frame(self.root, bg=BG, pady=10)
        header.pack(fill="x")
        tk.Label(
            header, text="📷  Photo Booth  📷",
            font=("Segoe UI", 18, "bold"),
            fg=ACCENT, bg=BG
        ).pack()

        # ---- video canvas ----
        canvas_frame = tk.Frame(self.root, bg=CARD, padx=4, pady=4)
        canvas_frame.pack(padx=20, pady=(0, 10))

        self.canvas = tk.Canvas(
            canvas_frame,
            width=self.PREVIEW_W, height=self.PREVIEW_H,
            bg="#11111b", highlightthickness=0
        )
        self.canvas.pack()

        # Placeholder text on canvas
        self.canvas.create_text(
            self.PREVIEW_W // 2, self.PREVIEW_H // 2,
            text="Connecting to camera…",
            fill="#585b70", font=("Segoe UI", 16), tag="placeholder"
        )

        # ---- status bar ----
        self.status_var = tk.StringVar(value="Starting camera…")
        status_bar = tk.Label(
            self.root, textvariable=self.status_var,
            font=("Segoe UI", 10), fg=FG, bg="#181825", anchor="w", padx=10
        )
        status_bar.pack(fill="x")

        # ---- button row ----
        btn_frame = tk.Frame(self.root, bg=BG, pady=14)
        btn_frame.pack()

        btn_cfg = dict(font=("Segoe UI", 22, "normal"), relief="flat",
                       cursor="hand2", padx=5, pady=0, bd=0)

        self.btn_capture = tk.Button(
            btn_frame, text="📸",
            bg=BTN_RES, fg="#1e1e2e",
            command=self._capture,
            **btn_cfg
        )
        self.btn_capture.grid(row=0, column=0, padx=10)

        # self.btn_print = tk.Button(
        #     btn_frame, text="🖨  Print",
        #     bg=BTN_PRT, fg="#1e1e2e",
        #     command=self._print,
        #     state="disabled",
        #     **btn_cfg
        # )
        # self.btn_print.grid(row=0, column=1, padx=10)

        # self.btn_resume = tk.Button(
        #     btn_frame, text="🔄  Resume",
        #     bg=BTN_RES, fg="#1e1e2e",
        #     command=self._resume,
        #     state="disabled",
        #     **btn_cfg
        # )
        # self.btn_resume.grid(row=0, column=2, padx=10)

        # ---- printer info label ----
        try:
            default_printer = win32print.GetDefaultPrinter()
        except Exception:
            default_printer = "Unknown"

        info_frame = tk.Frame(self.root, bg=BG, pady=6)
        info_frame.pack()
        tk.Label(
            info_frame,
            text=f"Default printer:  {default_printer}",
            font=("Segoe UI", 9), fg="#585b70", bg=BG
        ).pack()

        # Bind window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    def _start_camera(self) -> None:
        """Open the default webcam (index 0) in a background thread."""
        threading.Thread(target=self._open_camera, daemon=True).start()

    def _open_camera(self) -> None:
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)   # CAP_DSHOW = faster init on Windows
        if not cap.isOpened():
            self.root.after(0, lambda: self.status_var.set(
                "❌  No webcam found. Connect a camera and restart."
            ))
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap = cap
        self.running = True
        self.root.after(0, self._schedule_frame)
        self.root.after(0, lambda: self.status_var.set("✅  Camera ready — press Capture"))

    def _schedule_frame(self) -> None:
        """Poll the camera at ~30 fps via tkinter's event loop."""
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
        self._display_frame(frame)

    def _display_frame(self, bgr_frame) -> None:
        """Convert BGR frame → Tk PhotoImage and draw on canvas."""
        h, w = bgr_frame.shape[:2]
        scale = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = int(w * scale), int(h * scale)

        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
        pil_img = pil_img.transpose(Image.FLIP_LEFT_RIGHT)
        tk_img = ImageTk.PhotoImage(pil_img)

        # Must keep a reference or it gets GC'd
        self.canvas._tk_img = tk_img  # type: ignore[attr-defined]
        self.canvas.delete("all")
        cx = self.PREVIEW_W // 2
        cy = self.PREVIEW_H // 2
        self.canvas.create_image(cx, cy, anchor="center", image=tk_img)

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def _capture(self) -> None:
        if self.last_frame is None:
            messagebox.showwarning("No frame", "Camera is not ready yet.")
            return

        self.frozen_frame = self.last_frame.copy()
        self.preview_frozen = True
        self._display_frame(self.frozen_frame)

        #self.btn_capture.config(state="disabled")
        #self.btn_print.config(state="normal")
        #self.btn_resume.config(state="normal")
        self.status_var.set("📸  Image captured — press Print to send to printer.")
        
        self._print()

    def _resume(self) -> None:
        self.frozen_frame = None
        self.preview_frozen = False
        self.btn_capture.config(state="normal")
        #self.btn_print.config(state="disabled")
        #self.btn_resume.config(state="disabled")
        self.status_var.set("✅  Live preview resumed — press Capture for a new shot.")

    def _print(self) -> None:
        if self.frozen_frame is None:
            messagebox.showwarning("Nothing to print", "Capture an image first.")
            return

        #self.btn_print.config(state="disabled")
        #self.btn_resume.config(state="disabled")
        self.status_var.set("🖨  Sending to printer…")

        # Convert captured BGR frame to PIL RGB
        rgb = cv2.cvtColor(self.frozen_frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb)
        ret, frame = self.cap.read()
        cv2.imwrite(datetime.now().strftime("%Y%m%d-%H%M%S")+".jpg", frame)

        # Run in background so the UI doesn't freeze
        threading.Thread(target=self._do_print, args=(pil_image,), daemon=True).start()

    def _do_print(self, pil_image: Image.Image) -> None:
        try:
            print_image(pil_image)
            self.root.after(0, self._print_success)
        except Exception as exc:
            self._print_error(str(exc))


    def _print_success(self) -> None:
        self.status_var.set("✅  Print job sent successfully!")
        #messagebox.showinfo("Print", "Image sent to printer successfully.")
        self._resume()

    def _print_error(self, msg: str) -> None:
        self.status_var.set(f"❌  Print failed: {msg}")
        messagebox.showerror("Print Error", f"Could not print:\n\n{msg}")
        #self.btn_print.config(state="normal")
        #self.btn_resume.config(state="normal")

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
    app = WebcamPrintApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()