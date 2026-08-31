# Bixolon AirPrint Gateway

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Turns a Bixolon XD5-43T thermal label printer into two proper AirPrint
targets on a Raspberry Pi (or any Docker host), without the
"convert-on-a-Windows-PC" workaround:

- **DHL label queue** — crops the actual 4x6in shipping label out of a
  DHL "Paketmarke" PDF (a full A4 sheet with the label rotated 90° and a
  receipt copy below it) and rotates it upright.
- **Scale-to-fit queue** — for normal documents: just shrinks whatever
  A4/A5 page you print down to fit the label, unmodified otherwise.

Both are independently configurable (printer IP, AirPrint display name)
from a small web UI, and both point at the same physical printer.

## Why this exists

Printing a label-sized page straight through a generic driver just scales
the *whole* source page down onto the label stock — wrong size, wrong
position, wrong content. This container sits between your Mac and the
printer as a CUPS+Avahi AirPrint bridge with two custom queues, each with
its own filter that does the real work before handing raw bytes to the
printer.

## How it works

- **CUPS + Avahi**, running with host networking (required for mDNS/Bonjour
  and direct IPP), advertise two queues over Bonjour so they show up in
  macOS's AirPrint picker.
- Two custom PPDs (`cups/ppd/*.ppd`) each declare page sizes matching the
  *source* document (A4/A5/Letter) rather than the physical label. This
  matters: if a PPD only offered the 4x6in label size, macOS's AirPrint
  client would pre-scale the entire source page down onto that tiny paper
  size itself, before the job ever reaches the filter — crushing whatever
  was on the page. Declaring the document's real size lets macOS pass pages
  through unscaled so the filter can do the actual fitting.
- Each PPD routes `application/pdf`, `application/postscript`, and
  `application/vnd.cups-postscript` jobs through its filter (macOS's print
  pipeline, Preview.app in particular, sometimes sends PostScript instead
  of PDF regardless of what's advertised — both are normalized to PDF via
  Ghostscript as the filter's first step).
- **`cups/filters/bixolonzpl`** (DHL queue): detects a full A4/Letter sheet,
  splits it at the midpoint, rotates the top half upright (the actual
  label; the bottom half is DHL's "for your records" receipt copy and is
  discarded); for anything else, crops to the ink bounding box instead.
- **`cups/filters/bixolonplain`** (scale-to-fit queue): no cropping or
  rotation at all — every page is rasterized as-is and scaled down
  (preserving aspect ratio) to fit the label.
- Both filters share `cups/filters/_common.py` for PDF/PostScript input
  handling, rasterization (`pdftoppm` at 300dpi), and fitting onto a
  1200x1800px (4x6in) canvas.
- Output is the printer's **native SBPL** command language (`CB` clear
  buffer, `LD` raw-bitmap-draw, `P1` print) — **not** ZPL. See "Why SBPL,
  not ZPL" below; this is load-bearing, not a style choice.
- CUPS sends the resulting bytes straight to the printer's raw/JetDirect
  port (`socket://<ip>:9100`).
- A small Flask web UI (port 8080) lets you set the printer's IP and each
  queue's AirPrint display name, and includes test buttons per queue (TCP
  check, a synthetic ZPL test label, and "convert & print" for a real PDF
  through either pipeline).

## Why SBPL, not ZPL

The XD5-43T advertises ZPL II emulation, and small text-only ZPL jobs
print fine. But sending a full-size (1200x1800px) image as a ZPL `^GF`
graphic field — tried both binary (`^GFB`) and ASCII-hex (`^GFA`) — reliably
**hard-locked the printer** during testing on this unit: even the physical
feed button stopped responding, requiring a power cycle to recover. The
identical image size printed correctly every time via the printer's native
SBPL `LD` command (documented in Bixolon's "Programming(SLCS) Manual").
The ZPL *emulation* layer appears to have a real firmware bug with large
graphics; the native command set does not. Do not switch the image path
back to ZPL on this hardware without re-testing carefully (small payload
first, physically check the printer survives, then scale up).

## One-time printer setup

None needed for the graphics path (native SBPL is used, not an emulation
mode that needs enabling). If a *test label* doesn't print, first confirm
you can reach the printer with `nc -zv <printer-ip> 9100` from the Pi.

## Deploy

No `docker compose` plugin required — plain Docker works fine:

```bash
docker build -t bixolon-airprint-gateway:latest .
docker rm -f bixolon-airprint-gateway 2>/dev/null
docker run -d \
  --name bixolon-airprint-gateway \
  --restart unless-stopped \
  --network host \
  --cap-add NET_RAW \
  -v "$(pwd)/config:/config" \
  bixolon-airprint-gateway:latest
```

**Important:** if the host already runs its own `avahi-daemon` (common on
Raspberry Pi OS), disable it first — it'll conflict with the one inside the
container under host networking:

```bash
sudo systemctl disable --now avahi-daemon avahi-daemon.socket
```

**After any code change**, rebuild *and recreate* the container — `docker
restart` reuses the original image snapshot, not a freshly built one with
the same tag, so edits silently don't take effect otherwise:

```bash
docker build -t bixolon-airprint-gateway:latest .
docker rm -f bixolon-airprint-gateway
docker run -d ... # same as above
```

Then open `http://<host-ip>:8080`, set the printer's IP and both queues'
AirPrint names, and hit **Save & apply**. Use the **test buttons** on that
page before trying either printer from macOS:

1. *Check connection* — confirms the host can reach the printer on port 9100.
2. *Send test label* — sends a small synthetic ZPL text label; if this
   doesn't print, the IP/port is probably wrong (this is not the
   SBPL-vs-ZPL graphics issue — see above — since it's text-only).
3. *Convert & print* (per queue) — upload a real PDF; runs the actual
   pipeline for that queue and prints it, so you can check crop/scale
   before ever touching AirPrint.

Once those work, both printers should appear on your Mac under **System
Settings → Printers & Scanners** within a minute, named whatever you set.
Print to either like any normal AirPrint printer.

## Config persistence

Settings are stored in `./config/config.json` on the host (bind-mounted
into the container — see `config/config.example.json` for the shape), so
they survive rebuilds/restarts. This file is gitignored since it holds a
specific deployment's real printer IP.

## Known gotcha: renaming an already-added printer

Changing a queue's AirPrint name in the web UI changes what's *broadcast*
going forward — it does not rename a printer macOS has already added under
System Settings → Printers & Scanners (macOS keeps its own local copy of
that entry under whatever name it had when added). If a leftover entry
under the old name still shows up in the **Add Printer** dialog after a
rename, that's a stale mDNS cache on the client, not a server-side issue —
flush it with:

```bash
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

## Notes / things to double check on your actual PDFs

- The DHL filter processes **every page** as a separate label. If a DHL
  export includes extra pages you don't want printed (e.g. a customs
  invoice on its own page rather than sharing a sheet with the label),
  that page will currently also come out as a label.
- Full-sheet detection (for the DHL queue's split-and-rotate path) matches
  A4 and US Letter dimensions, ±5pt tolerance. A DHL label delivered at a
  different page size won't trigger the split and will fall back to
  ink-bbox cropping instead.
