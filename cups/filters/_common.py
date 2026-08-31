"""
Shared helpers for the Bixolon XD5-43T CUPS filters (bixolonzpl, bixolonplain).

Not a CUPS filter itself -- imported by the ones that are, which live
alongside it in the same directory.
"""

import io
import os
import subprocess
import sys
import tempfile

import numpy as np
import pikepdf
from PIL import Image

DPI = 300
LABEL_W_IN = float(os.environ.get("BIXOLON_LABEL_WIDTH_IN", "4"))
LABEL_H_IN = float(os.environ.get("BIXOLON_LABEL_HEIGHT_IN", "6"))
CANVAS_W = int(round(LABEL_W_IN * DPI))
CANVAS_H = int(round(LABEL_H_IN * DPI))


def read_input_pdf():
    # CUPS passes the job file as the last positional arg; falls back to
    # stdin when invoked without one (also how we test standalone).
    if len(sys.argv) >= 7 and sys.argv[6]:
        with open(sys.argv[6], "rb") as f:
            data = f.read()
    else:
        data = sys.stdin.buffer.read()

    # macOS's print pipeline (Preview.app in particular) sometimes sends
    # PostScript instead of the original PDF, regardless of the PDLs
    # advertised over AirPrint. Normalize to PDF so the rest of the
    # pipeline only ever has to deal with one format.
    if data[:2] == b"%!":
        with tempfile.NamedTemporaryFile(suffix=".ps", delete=False) as ps_tmp:
            ps_tmp.write(data)
            ps_path = ps_tmp.name
        pdf_path = ps_path + ".pdf"
        try:
            subprocess.run(
                ["gs", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-sDEVICE=pdfwrite",
                 f"-o{pdf_path}", ps_path],
                check=True, capture_output=True, timeout=60,
            )
            with open(pdf_path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(ps_path)
            if os.path.exists(pdf_path):
                os.unlink(pdf_path)

    return data


def page_count(pdf_bytes):
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)


def extract_page(pdf_bytes, page_index):
    """Pull a single page out as its own one-page PDF, unmodified."""
    src = pikepdf.open(io.BytesIO(pdf_bytes))
    dst = pikepdf.new()
    dst.pages.append(src.pages[page_index])
    buf = io.BytesIO()
    dst.save(buf)
    return buf.getvalue()


def rasterize(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    out_prefix = tmp_path + "-out"
    try:
        subprocess.run(
            ["pdftoppm", "-r", str(DPI), "-png", "-singlefile", tmp_path, out_prefix],
            check=True, capture_output=True, timeout=60,
        )
        with open(out_prefix + ".png", "rb") as f:
            return Image.open(io.BytesIO(f.read())).convert("L")
    finally:
        os.unlink(tmp_path)
        if os.path.exists(out_prefix + ".png"):
            os.unlink(out_prefix + ".png")


def fit_to_canvas(img):
    canvas = Image.new("L", (CANVAS_W, CANVAS_H), color=255)
    scale = min(CANVAS_W / img.width, CANVAS_H / img.height)
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    ox = (CANVAS_W - new_w) // 2
    oy = (CANVAS_H - new_h) // 2
    canvas.paste(resized, (ox, oy))
    return canvas


def image_to_sbpl_label(img):
    """SBPL native (CB clear-buffer, LD raw-bitmap-draw, P1 print) -- not
    ZPL: the printer's ZPL II emulation hard-locks on a full-size ^GF
    graphic field (confirmed on this hardware, both binary and hex
    encoding), while the identical image size prints fine every time via
    native SBPL."""
    import struct

    arr = np.array(img)
    ink = arr < 200  # dark pixels -> print dots
    packed = np.packbits(ink, axis=1)  # MSB-first per row
    bytes_per_row = packed.shape[1]
    rows = packed.shape[0]
    bitmap = packed.tobytes()

    ld_header = b"LD" + struct.pack("<HHHH", 0, 0, bytes_per_row, rows)
    return b"CB\r\n" + ld_header + bitmap + b"\r\n" + b"P1\r\n"
