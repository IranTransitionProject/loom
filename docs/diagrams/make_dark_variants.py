#!/usr/bin/env python3
"""Generate dark-mode variants of every diagram SVG under ``docs/images/``.

For each ``<name>.svg`` (light, source-of-truth), produces
``<name>-dark.svg`` with a palette tuned for Material's ``scheme: slate``
backdrop.  The transform is a deterministic colour-by-colour swap:

- Backgrounds (the brightest light tones) become dark surface tones.
- Light pastel fills used by the drawio diagrams become darker
  but-still-distinct tones, so the colour-coding survives.
- Dark text becomes light text; light-grey labels become dimmer
  light text; mid-grey strokes are inverted to lighter strokes.

The swap operates on the rendered SVG, so the same script handles
both the Python-generated diagrams (``docs/generate_diagrams.py``)
and the drawio-exported diagrams (``docs/diagrams/*.drawio`` → SVG via
``.github/workflows/build-diagrams.yml``).  Source files are
untouched.

Usage:
    python docs/diagrams/make_dark_variants.py

Output:
    docs/images/<name>-dark.svg  (one per non-dark ``<name>.svg``)

Pin: any ``<name>.svg`` whose contents are unchanged on the next run
produce a bit-identical ``<name>-dark.svg`` — the transform is
deterministic and idempotent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
# Order matters: longer / more-specific keys come first so that, e.g.,
# ``#fafafb`` doesn't accidentally also match ``#fafafb-suffix``.  Each entry
# is (light, dark); the replacer matches ``#rrggbb`` case-insensitively and
# preserves case-insensitivity of the originals.

# Background tones (page / canvas).
_BG_SWAPS: list[tuple[str, str]] = [
    ("#fafafb", "#1e1e26"),
    ("#fafbff", "#1e1e26"),
    ("#fffaee", "#2a2715"),  # warm message-box bg
    ("#f4f0ff", "#28223a"),  # purple note bg
    ("#e0e8ff", "#1e2a40"),  # blue layer bg
    ("#e8f5e9", "#1d2a1d"),  # green layer bg
    ("#fff0e6", "#2e2218"),  # orange layer bg
    ("#fff3e0", "#2c241a"),  # workshop UI panels
    ("#fff8e1", "#2a2618"),  # similar warm
    ("#fffffe", "#1e1e26"),
    ("#ffffff", "#1e1e26"),
]

# Border / stroke tones.
_STROKE_SWAPS: list[tuple[str, str]] = [
    ("#e6dbb5", "#5a4f2a"),
    ("#d4c8ee", "#5c4d8a"),
    ("#bbb", "#888"),
    ("#999", "#999"),
    ("#ccc", "#666"),
    ("#ddd", "#555"),
    ("#eee", "#444"),
    ("#e0e0e0", "#3a3a44"),
]

# Text colours.
_TEXT_SWAPS: list[tuple[str, str]] = [
    ("#1e1e26", "#e6e6ec"),  # primary heading text
    ("#000000", "#e6e6ec"),
    ("#000", "#e6e6ec"),
    ("#222", "#e6e6ec"),
    ("#333", "#d0d0d6"),
    ("#444", "#c0c0c8"),
    ("#555", "#a0a0ab"),
    ("#666", "#9aa0aa"),
    ("#777", "#8a909a"),
    ("#888", "#8a909a"),
    ("#8a7a55", "#bfa961"),  # warm message-box label
    ("#6b50a0", "#b8a7e8"),  # purple note text
]

# Drawio "soft pastel" fills — common defaults from the drawio shape
# library.  Keep the same hue, darken the value.
_DRAWIO_FILL_SWAPS: list[tuple[str, str]] = [
    ("#f8cecc", "#5a2e2c"),  # red
    ("#b85450", "#e88a85"),  # red stroke (lighter for contrast)
    ("#d5e8d4", "#2c4a2a"),  # green
    ("#82b366", "#9ad075"),  # green stroke
    ("#dae8fc", "#283a55"),  # blue
    ("#6c8ebf", "#8aaee0"),  # blue stroke
    ("#fff2cc", "#4a3e15"),  # yellow
    ("#d6b656", "#e0c870"),  # yellow stroke
    ("#e1d5e7", "#3a2d4a"),  # purple
    ("#9673a6", "#b894c8"),  # purple stroke
    ("#ffe6cc", "#4a3618"),  # orange
    ("#d79b00", "#e8b440"),  # orange stroke
    ("#f5f5f5", "#2a2a30"),  # light grey
    ("#666666", "#aaaaaa"),  # grey stroke
]

# Drawio's color-scheme hint: light → dark.
_COLOR_SCHEME_SWAP = ("color-scheme: light", "color-scheme: dark")

# Heddle-brand accents used by the Python generator.  Keep saturation,
# nudge brightness so the swatch reads against dark.
_BRAND_SWAPS: list[tuple[str, str]] = [
    ("#4278d9", "#5b94f0"),  # MCP/Workshop/CLI accent
    ("#5a8fdb", "#7aabec"),
    ("#2e7d32", "#4a9c4e"),  # success green
    ("#c62828", "#e87878"),  # danger red
    ("#f57c00", "#ffa040"),  # warn orange
    ("#6750a4", "#9a85c8"),  # purple primary
    ("#1976d2", "#5aaeec"),  # blue primary
]

ALL_SWAPS: list[tuple[str, str]] = (
    _BG_SWAPS
    + _STROKE_SWAPS
    + _TEXT_SWAPS
    + _DRAWIO_FILL_SWAPS
    + _BRAND_SWAPS
)


def _swap_colors(svg: str) -> str:
    """Apply the deterministic light→dark palette swap to an SVG string."""
    out = svg
    for light, dark in ALL_SWAPS:
        # Match the colour as a hex literal that's bounded by either the
        # opening quote/whitespace of an attribute value or a non-hex
        # character.  ``re.escape`` keeps ``#`` literal.
        pattern = re.compile(re.escape(light) + r"(?![0-9a-fA-F])", re.IGNORECASE)
        out = pattern.sub(dark, out)
    out = out.replace(_COLOR_SCHEME_SWAP[0], _COLOR_SCHEME_SWAP[1])
    return out


def _is_dark_variant(p: Path) -> bool:
    return p.stem.endswith("-dark")


def main() -> int:
    if not IMAGES_DIR.exists():
        print(f"images dir not found: {IMAGES_DIR}", file=sys.stderr)
        return 1

    light_svgs = sorted(p for p in IMAGES_DIR.glob("*.svg") if not _is_dark_variant(p))
    if not light_svgs:
        print("no light SVGs found", file=sys.stderr)
        return 1

    for light_path in light_svgs:
        dark_path = light_path.with_name(f"{light_path.stem}-dark.svg")
        light_svg = light_path.read_text(encoding="utf-8")
        dark_svg = _swap_colors(light_svg)
        dark_path.write_text(dark_svg, encoding="utf-8")
        print(f"  wrote {dark_path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
