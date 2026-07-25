#!/usr/bin/env python3
"""Build the Windows observer icon from the repository-native SVG source.

This intentionally supports only the simple SVG primitives used by the icon,
keeping icon generation independent from browser or ImageMagick installations.
Pillow is a development-time dependency; the generated ICO is tracked.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "assets" / "observer-console.svg"
DEFAULT_OUTPUT = REPO_ROOT / "assets" / "observer-console.ico"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
RENDER_SIZE = 1024


def _number(element: ET.Element, name: str, default: float = 0) -> float:
    return float(element.attrib.get(name, default))


def _render_svg(source: Path) -> Image.Image:
    root = ET.parse(source).getroot()
    if root.attrib.get("viewBox") != "0 0 1024 1024":
        raise ValueError("observer icon SVG must use viewBox='0 0 1024 1024'")

    image = Image.new("RGBA", (RENDER_SIZE, RENDER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    for element in root:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"title", "desc"}:
            continue

        fill = element.attrib.get("fill")
        if tag == "rect":
            x = _number(element, "x")
            y = _number(element, "y")
            box = (
                x,
                y,
                x + _number(element, "width"),
                y + _number(element, "height"),
            )
            stroke = element.attrib.get("stroke")
            stroke_width = round(_number(element, "stroke-width"))
            draw.rounded_rectangle(
                box,
                radius=_number(element, "rx"),
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif tag == "ellipse":
            cx = _number(element, "cx")
            cy = _number(element, "cy")
            rx = _number(element, "rx")
            ry = _number(element, "ry")
            draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=fill)
        elif tag == "circle":
            cx = _number(element, "cx")
            cy = _number(element, "cy")
            radius = _number(element, "r")
            draw.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                fill=fill,
            )
        else:
            raise ValueError(f"unsupported SVG element: {tag}")

    return image


def build_icon(source: Path, output: Path) -> None:
    source_image = _render_svg(source)
    largest = source_image.resize((256, 256), Image.Resampling.LANCZOS)
    output.parent.mkdir(parents=True, exist_ok=True)
    largest.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build_icon(args.source.resolve(), args.output.resolve())
    print(f"built {args.output} with sizes: {', '.join(map(str, ICON_SIZES))}")


if __name__ == "__main__":
    main()
