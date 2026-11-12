# Diagram sources

Each `docs/diagrams/<name>.png` is drawn from `<name>.json` in this directory by `figkit.py`, a single-file renderer with no dependencies beyond Python 3 and a headless Google Chrome (or `rsvg-convert`) for the PNG. The default style is the hand-drawn Excalidraw look, with a faint pattern per role so the roles stay distinguishable without colour.

Redraw every figure from the repository root:

```bash
for f in docs/diagrams/src/*.json; do python3 docs/diagrams/src/figkit.py "$f" --out "docs/diagrams/$(basename "$f" .json)"; done
rm -f docs/diagrams/*.svg
```

The spec format is described at the top of `figkit.py`: boxes on a grid (`col`, `row`, optional `w` and `h` spans), one of five roles (`neutral`, `primary`, `secondary`, `tertiary`, `accent`), and edges between box ids (`label`, `dashed`, `emph` for the one edge a figure is about). Keep one step per figure; a figure that needs a paragraph of text inside its boxes should be split.
