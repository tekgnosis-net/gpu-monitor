#!/usr/bin/env python3
"""Generate the gpu-monitor favicon set.

Produces:
  src/web/images/favicon-16x16.png
  src/web/images/favicon-32x32.png
  src/web/images/favicon.ico  (multi-resolution: 16, 32, 48)

Design rationale:
  * Solid NVIDIA-green (#76B900) rounded-square background — instantly
    recognizable as GPU-related at any size, and distinct from the
    sea of blue/purple favicons in the average browser tab strip.
  * White cooling-fan motif centered on the green field: outer ring,
    central hub, 4 curved blades radiating outward. The fan is the
    most universally-recognized "this is a GPU" visual cue and is
    what nvidia-smi-the-CLI's actual heatsink reports.
  * Drawn at 256×256 native then downsampled with LANCZOS resampling
    for crisp results at small sizes.
  * No text — letterforms are illegible below 24px and the fan motif
    is more identifiable than any 2-3 character abbreviation.

Re-run from the repo root:
    python3 scripts/generate_favicon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


# ─── Design tokens ─────────────────────────────────────────────────────────

NATIVE = 256                       # design resolution; downsampled for output
NV_GREEN = (118, 185, 0, 255)      # #76B900 — NVIDIA brand green
WHITE    = (255, 255, 255, 255)
HUB      = (30, 41, 59, 255)       # slate-800 — fan hub for contrast


def _draw_native() -> Image.Image:
    """Render the favicon at NATIVE (256px) for downsampling."""
    img = Image.new("RGBA", (NATIVE, NATIVE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background — the green field that screams "GPU".
    pad = int(NATIVE * 0.04)
    d.rounded_rectangle(
        [pad, pad, NATIVE - pad, NATIVE - pad],
        radius=int(NATIVE * 0.18),
        fill=NV_GREEN,
    )

    cx, cy = NATIVE // 2, NATIVE // 2

    # Outer ring of the cooling-fan housing — defines the silhouette.
    outer_r = int(NATIVE * 0.36)
    ring_w = int(NATIVE * 0.04)
    d.ellipse(
        [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
        outline=WHITE, width=ring_w,
    )

    # Four curved fan blades. Each blade is one thick arc segment swept
    # through ~60° at radius blade_r, rotated 90° apart. Drawing arcs
    # (rather than filled polygons) keeps the design lightweight and
    # renders crisply at any size after LANCZOS downsampling. A single
    # arc per blade is sufficient — adding an inner stroke at small
    # radii made the 16×16 output too busy.
    blade_r = int(NATIVE * 0.28)
    blade_w = int(NATIVE * 0.075)

    for angle_deg in (0, 90, 180, 270):
        sweep_start = angle_deg - 30
        sweep_end = angle_deg + 30
        d.arc(
            [cx - blade_r, cy - blade_r, cx + blade_r, cy + blade_r],
            sweep_start, sweep_end, fill=WHITE, width=blade_w,
        )

    # Central hub — a small dark circle that anchors the fan visually
    # and contrasts against the surrounding white blades.
    hub_r = int(NATIVE * 0.07)
    d.ellipse(
        [cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
        fill=HUB,
    )

    return img


def _save_png(img: Image.Image, path: Path, size: int) -> None:
    """Downsample the native image to `size` × `size` and save as PNG."""
    out = img.resize((size, size), Image.Resampling.LANCZOS)
    out.save(path, format="PNG", optimize=True)
    print(f"  wrote {path} ({size}×{size})")


def _save_ico(img: Image.Image, path: Path, sizes: tuple[int, ...]) -> None:
    """Save a multi-resolution .ico file. Pillow's ICO encoder takes a
    list of sizes and embeds each as a separate image record so the
    browser picks the best match at runtime (favicon, taskbar, etc.)."""
    img.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"  wrote {path} (multi: {sizes})")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    images_dir = repo_root / "src" / "web" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    print("Generating favicon set…")
    native = _draw_native()

    _save_png(native, images_dir / "favicon-16x16.png", 16)
    _save_png(native, images_dir / "favicon-32x32.png", 32)
    _save_ico(native, images_dir / "favicon.ico", (16, 32, 48))

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
