#!/usr/bin/env python3
"""Draw a box-and-arrow figure in one of six styles taken from well-read technical blogs.

    python3 figkit.py spec.json --style excalidraw-sketch --out ~/tmp/fig/arch
    python3 figkit.py spec.json --all --out ~/tmp/fig/arch        # every style, for choosing
    python3 figkit.py --list

writes `<out>.svg` and `<out>.png` (2x, opaque background). The styles, the blogs each one comes from
and its measured colours are in the module below. Without `--style` the style is
`excalidraw-sketch`, the one the Hugging Face blog itself uses.

Every role also carries a faint pattern, so that a reader who cannot tell the colours apart can
still tell the roles apart.

The spec (see architecture.json):

    {"title": "optional heading drawn in the figure",
     "nodes": [{"id": "prefill", "label": "Prefill GPUs", "role": "primary", "col": 2, "row": 0,
                "stack": true}],
     "edges": [{"from": "prefill", "to": "decode", "label": "KV cache", "emph": true}],
     "legend": {"primary": "prefill pool", "secondary": "decode pool"}}

`role` is one of neutral, primary, secondary, tertiary, accent. `col` and `row` place the box on a
grid; `w` and `h` (default 1) let a box span several columns or rows, for a box that lists the
parts of one step. `align: "left"` left-aligns a multi-line label. `stack` draws the box as
several copies, for a pool of machines. `dashed` (on a node or an edge) draws it with a dashed
line, for a part that is off by default. `emph` draws the one edge the figure is about in the
accent colour, thicker.
"""
import argparse
import html
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROLES = ("neutral", "primary", "secondary", "tertiary", "accent")
# One pattern per role; neutral has none.
PATTERN = {"primary": "diagonal", "secondary": "cross", "tertiary": "grid", "accent": "dots"}

STYLES = {
    # Excalidraw defaults, as in huggingface.co/blog/continuous_batching.
    "excalidraw-sketch": dict(
        bg="#ffffff", ink="#1e1e1e", accent="#e03131", font="Virgil, 'Comic Sans MS', 'Segoe Print', cursive",
        size=21, sw=2.2, radius=12, rough=3.0, weight="normal", label_ink=None,
        fill={"neutral": "#ffffff", "primary": "#a5d8ff", "secondary": "#b2f2bb",
              "tertiary": "#ffec99", "accent": "#ffc9c9"}),
    # Jay Alammar's Illustrated series: pastel boxes, bold coloured labels, pill corners.
    "illustrated-flat": dict(
        bg="#ffffff", ink="#3a3a3a", accent="#d6336c", font="'Helvetica Neue', Arial, sans-serif",
        size=19, sw=2.0, radius=26, rough=0, weight="bold",
        label_ink={"neutral": "#3a3a3a", "primary": "#1f6feb", "secondary": "#2f9e44",
                   "tertiary": "#c2410c", "accent": "#d6336c"},
        fill={"neutral": "#f8f9fa", "primary": "#e4f1fe", "secondary": "#e6f6e3",
              "tertiary": "#ffefde", "accent": "#ffdeeb"}),
    # horace.io: black marker on paper, one highlight colour.
    "whiteboard-sketch": dict(
        bg="#fffef9", ink="#1a1a1a", accent="#17b2d1", font="'Marker Felt', 'Bradley Hand', 'Comic Sans MS', cursive",
        size=21, sw=3.6, radius=4, rough=4.5, weight="normal", label_ink=None,
        fill={"neutral": "#fffef9", "primary": "#fffef9", "secondary": "#fffef9",
              "tertiary": "#fffef9", "accent": "#e3f7fb"}),
    # Distill: serif, thin rules, a ColorBrewer sequential scale.
    "academic-serif": dict(
        bg="#ffffff", ink="#333333", accent="#c51b8a", font="Georgia, 'Times New Roman', serif",
        size=19, sw=1.2, radius=3, rough=0, weight="normal", label_ink=None,
        fill={"neutral": "#ffffff", "primary": "#fde0dd", "secondary": "#fa9fb5",
              "tertiary": "#f7f7f7", "accent": "#c51b8a"}),
    # NVIDIA Technical Blog: near-black on white, one brand colour for the path that matters.
    "corporate-technical": dict(
        bg="#ffffff", ink="#242424", accent="#76b900", font="'Helvetica Neue', Arial, sans-serif",
        size=18, sw=2.0, radius=5, rough=0, weight="600", label_ink=None,
        fill={"neutral": "#ffffff", "primary": "#f2f8e6", "secondary": "#f6f6f6",
              "tertiary": "#e9eef3", "accent": "#d4ebb0"}),
    # Hazy Research's matplotlib xkcd charts.
    "matplotlib-xkcd": dict(
        bg="#ffffff", ink="#333333", accent="#b5179e", font="'Humor Sans', 'Comic Sans MS', cursive",
        size=21, sw=2.6, radius=10, rough=3.6, weight="normal", label_ink=None,
        fill={"neutral": "#ffffff", "primary": "#9a7fc7", "secondary": "#74c3c4",
              "tertiary": "#f2d06b", "accent": "#f4a3c0"}),
}
DEFAULT = "excalidraw-sketch"

BW, BH, GY, PAD, TOP = 190, 92, 70, 40, 74
STACK = 7  # offset of each copy behind a stacked box, drawn upward so it never covers an arrow


def _pattern_defs(st):
    """Faint pattern tiles drawn in the ink colour at low opacity, one per pattern name."""
    ink, op = st["ink"], 0.16
    tiles = {
        "diagonal": f'<path d="M-2,2 l4,-4 M0,10 l10,-10 M8,12 l4,-4" stroke="{ink}" stroke-width="1.2" opacity="{op}"/>',
        "backdiagonal": f'<path d="M-2,8 l4,4 M0,0 l10,10 M8,-2 l4,4" stroke="{ink}" stroke-width="1.2" opacity="{op}"/>',
        "cross": f'<path d="M0,10 l10,-10 M0,0 l10,10" stroke="{ink}" stroke-width="1" opacity="{op}"/>',
        "dots": f'<circle cx="2.5" cy="2.5" r="1.3" fill="{ink}" opacity="{op + 0.1}"/><circle cx="7.5" cy="7.5" r="1.3" fill="{ink}" opacity="{op + 0.1}"/>',
        "grid": f'<path d="M0,0.5 h10 M0.5,0 v10" stroke="{ink}" stroke-width="1" stroke-dasharray="1.5 1.5" opacity="{op + 0.06}"/>',
    }
    return "".join(f'<pattern id="pat-{k}" width="10" height="10" patternUnits="userSpaceOnUse">{v}</pattern>'
                   for k, v in tiles.items())


def _gap(spec, st):
    """Horizontal gap between boxes: wide enough for the longest edge label plus its arrow."""
    # Only an edge between boxes on the same row runs through the gap; a vertical edge puts its
    # label beside itself and needs no width from it.
    row = {n["id"]: n.get("row", 0) for n in spec["nodes"]}
    longest = max([max(map(len, e.get("label", "").split("\n")))
                   for e in spec.get("edges", []) if row[e["from"]] == row[e["to"]]] + [0])
    return max(80, int(longest * (st["size"] - 4) * 0.7) + 48)


def _size(n, GX):
    """Width and height of a box that spans `w` columns and `h` rows (both default 1)."""
    w, h = n.get("w", 1), n.get("h", 1)
    return BW * w + GX * (w - 1), BH * h + GY * (h - 1)


def _layout(spec, GX):
    pos = {}
    top = TOP if spec.get("title") else PAD + 2 * STACK
    for n in spec["nodes"]:
        x = PAD + n.get("col", 0) * (BW + GX)
        y = top + n.get("row", 0) * (BH + GY)
        pos[n["id"]] = (x, y) + _size(n, GX)
    cols = max(n.get("col", 0) + n.get("w", 1) for n in spec["nodes"])
    rows = max(n.get("row", 0) + n.get("h", 1) for n in spec["nodes"])
    w = PAD * 2 + cols * BW + (cols - 1) * GX
    h = top + rows * BH + (rows - 1) * GY + PAD + (54 if spec.get("legend") else 0)
    return pos, w, h


def _edge_points(a, b, a_stack=False, b_stack=False):
    """Start and end on the facing sides of two boxes at top-left a and b.

    A stacked box's copies rise above it, so an edge that leaves or enters its top side starts or
    ends above the copies instead of under them.
    """
    (ax, ay, aw, ah), (bx, by, bw, bh) = a, b
    ay_top = ay - 2 * STACK if a_stack else ay
    by_top = by - 2 * STACK if b_stack else by
    acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
    # Boxes that overlap in one axis are joined straight across that axis, at the middle of the
    # overlap, so an edge to a tall or wide box stays horizontal or vertical.
    ox0, ox1 = max(ax, bx), min(ax + aw, bx + bw)
    oy0, oy1 = max(ay, by), min(ay + ah, by + bh)
    if ox1 - ox0 > 0 and not oy1 - oy0 > 0:
        cx = (ox0 + ox1) / 2
        sy, ey = (ay + ah, by_top) if bcy > acy else (ay_top, by + bh)
        return (cx, sy), (cx, ey)
    if oy1 - oy0 > 0 and not ox1 - ox0 > 0:
        cy = (oy0 + oy1) / 2
        sx, ex = (ax + aw, bx) if bcx > acx else (ax, bx + bw)
        return (sx, cy), (ex, cy)
    if abs(acy - bcy) >= abs(acx - bcx):
        sy, ey = (ay + ah, by_top) if bcy > acy else (ay_top, by + bh)
        return (acx, sy), (bcx, ey)
    sx, ex = (ax + aw, bx) if bcx > acx else (ax, bx + bw)
    return (sx, acy), (ex, bcy)


def _text(x, y, label, st, ink, size=None, weight=None, anchor="middle", halo=False):
    size = size or st["size"]
    lines = label.split("\n")
    out = []
    y0 = y - (len(lines) - 1) * size * 0.6
    for i, ln in enumerate(lines):
        extra = (f' stroke="{st["bg"]}" stroke-width="4" paint-order="stroke"' if halo else "")
        out.append(f'<text x="{x:.1f}" y="{y0 + i * size * 1.2:.1f}" text-anchor="{anchor}" '
                   f'dominant-baseline="middle" font-family="{html.escape(st["font"], quote=True)}" '
                   f'font-size="{size}" font-weight="{weight or st["weight"]}" fill="{ink}"{extra}>'
                   f'{html.escape(ln)}</text>')
    return "".join(out)


def render(spec, style=DEFAULT):
    st = STYLES[style]
    GX = _gap(spec, st)
    pos, w, h = _layout(spec, GX)
    # The filter region is the whole canvas in user space: a region relative to the element's own
    # bounding box is zero high for a horizontal arrow, and the arrow then disappears.
    rough = (f'<filter id="rough" filterUnits="userSpaceOnUse" x="0" y="0" width="{w}" height="{h}">'
             f'<feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2" seed="5"/>'
             f'<feDisplacementMap in="SourceGraphic" scale="{st["rough"]}" xChannelSelector="R" '
             f'yChannelSelector="G"/></filter>') if st["rough"] else ""
    flt = ' filter="url(#rough)"' if st["rough"] else ""
    marker = lambda mid, col, size=8: (  # noqa: E731
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="{size}" markerHeight="{size}" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{col}"/></marker>')
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<defs>{_pattern_defs(st)}{rough}{marker("ah", st["ink"])}{marker("ahx", st["accent"], 4.6)}</defs>',
             f'<rect width="{w}" height="{h}" fill="{st["bg"]}"/>']
    if spec.get("title"):
        parts.append(_text(PAD, 38, spec["title"], st, st["ink"], size=st["size"] + 3, anchor="start",
                           weight="bold" if style != "excalidraw-sketch" else "normal"))

    def box(x, y, role, bw=BW, bh=BH, dashed=False, layer_flt=True):
        fill = st["fill"][role]
        r = st["radius"]
        dash = ' stroke-dasharray="9 6"' if dashed else ""
        out = (f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="{r}" fill="{fill}" '
               f'stroke="{st["ink"]}" stroke-width="{st["sw"]}"{dash}{flt if layer_flt else ""}/>')
        if role in PATTERN:
            out += (f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="{r}" '
                    f'fill="url(#pat-{PATTERN[role]})" stroke="none"/>')
        return out

    for e in spec.get("edges", []):
        stacked = {n["id"] for n in spec["nodes"] if n.get("stack")}
        (sx, sy), (ex, ey) = _edge_points(pos[e["from"]], pos[e["to"]],
                                          e["from"] in stacked, e["to"] in stacked)
        col = st["accent"] if e.get("emph") else st["ink"]
        sw = st["sw"] * (1.9 if e.get("emph") else 1.0)
        dash = ' stroke-dasharray="7 5"' if e.get("dashed") else ""
        parts.append(f'<path d="M{sx:.1f},{sy:.1f} L{ex:.1f},{ey:.1f}" stroke="{col}" stroke-width="{sw}" '
                     f'fill="none" marker-end="url(#{"ahx" if e.get("emph") else "ah"})"{dash}{flt}/>')
        if e.get("label"):
            mx, my = (sx + ex) / 2, (sy + ey) / 2 - (16 if sy == ey else 0)
            parts.append(_text(mx + (0 if sy == ey else 12), my, e["label"], st, col,
                               size=st["size"] - 4, anchor="middle" if sy == ey else "start", halo=True))
    for n in spec["nodes"]:
        x, y, bw, bh = pos[n["id"]]
        role = n.get("role", "neutral")
        if n.get("stack"):
            for k in (2, 1):
                parts.append(box(x, y - STACK * k, role, bw, bh))
        parts.append(box(x, y, role, bw, bh, n.get("dashed", False)))
        ink = (st["label_ink"] or {}).get(role, st["ink"])
        if style == "academic-serif" and role == "accent":
            ink = "#ffffff"
        left = n.get("align") == "left"
        parts.append(_text(x + 22 if left else x + bw / 2, y + bh / 2, n["label"], st, ink,
                           anchor="start" if left else "middle", halo=style == "matplotlib-xkcd"))
    if spec.get("legend"):
        lx, ly = PAD, h - PAD - 20
        for role, name in spec["legend"].items():
            parts.append(f'<rect x="{lx}" y="{ly - 11}" width="30" height="22" rx="4" fill="{st["fill"][role]}" '
                         f'stroke="{st["ink"]}" stroke-width="1.2"/>')
            if role in PATTERN:
                parts.append(f'<rect x="{lx}" y="{ly - 11}" width="30" height="22" rx="4" '
                             f'fill="url(#pat-{PATTERN[role]})"/>')
            parts.append(_text(lx + 40, ly, name, st, st["ink"], size=st["size"] - 4, anchor="start"))
            lx += 40 + int(len(name) * (st["size"] - 4) * 0.58) + 34
    parts.append("</svg>")
    return "".join(parts), w, h


def to_png(svg_path, png_path, w, h):
    """Rasterise at 2x. Chrome renders the system fonts the styles name; rsvg-convert is the fallback."""
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome).exists():
        with tempfile.TemporaryDirectory() as td:
            page = Path(td) / "p.html"
            page.write_text(f'<html><body style="margin:0">{Path(svg_path).read_text()}</body></html>')
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--window-size={w},{h}", "--force-device-scale-factor=2",
                            f"--screenshot={png_path}", page.as_uri()],
                           check=True, capture_output=True)
        return
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", "2", "-o", str(png_path), str(svg_path)], check=True)
        return
    raise SystemExit("[ERROR] no rasteriser: install Google Chrome or rsvg-convert")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--style", default=DEFAULT, choices=sorted(STYLES))
    ap.add_argument("--all", action="store_true", help="draw every style, as <out>-<style>")
    ap.add_argument("--out", help="output path without extension")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k in STYLES:
            print(k + ("  (default)" if k == DEFAULT else ""))
        return
    if not a.spec or not a.out:
        ap.error("spec and --out are required")
    spec = json.loads(Path(a.spec).read_text())
    bad = {n.get("role", "neutral") for n in spec["nodes"]} - set(ROLES)
    if bad:
        raise SystemExit(f"[ERROR] unknown role {sorted(bad)}; use one of {', '.join(ROLES)}")
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    for style in (STYLES if a.all else [a.style]):
        base = out.with_name(f"{out.name}-{style}") if a.all else out
        svg, w, h = render(spec, style)
        base.with_suffix(".svg").write_text(svg)
        to_png(base.with_suffix(".svg"), base.with_suffix(".png"), w, h)
        print(base.with_suffix(".png"))


if __name__ == "__main__":
    main()
