# provgate brand assets

`provgate` is a companion tool to [Provenance](https://github.com/itsgeagle/provenance), so
its mark deliberately borrows Provenance's visual language: the same rounded-square
**chain link** (chain-of-custody) and the same ink + orange palette. The difference is the
**submission arrow** threading through the link — `provgate` is the *gateway* that passes new
Gradescope submissions into the Provenance chain. The tail passes behind the link on the
left; the arrow exits in front on the right.

## Color tokens

| Token      | Light surface | Dark surface |
| ---------- | ------------- | ------------ |
| Ink / line | `#18181b`     | `#fafafa`    |
| Accent     | `#EA580C`     | `#F97316`    |

Same tokens as Provenance; the accent brightens on dark surfaces for contrast.

The wordmark is set in the Tailwind default system-sans stack at weight 700, matching the
Provenance wordmark.

## Source masters (SVG)

| File                            | Use                              |
| ------------------------------- | -------------------------------- |
| `provgate-mark.svg` / `-dark`   | symbol only                      |
| `provgate-lockup.svg` / `-dark` | symbol + wordmark                |
| `architecture.svg` / `-dark`    | system diagram                   |

## Exports (PNG)

The README embeds the **PNGs**, not the SVGs. System fonts render per-machine, so the
wordmark and diagram labels are rasterized for portable, predictable sizing; the SVGs stay
the editable masters. PNGs have transparent backgrounds so they blend into GitHub's light or
dark theme. Light/dark selection is handled with a `<picture>` + `prefers-color-scheme` element.

| File                                 | Where it's wired            |
| ------------------------------------ | --------------------------- |
| `exports/lockup-light.png` / `-dark` | README header               |
| `exports/architecture-light.png` / `-dark` | README "How it works" |
| `exports/mark-light.png` / `-dark`   | icon / favicon source       |

## Regenerating exports

Rendered with [`rsvg-convert`](https://gitlab.gnome.org/GNOME/librsvg) (`brew install librsvg`).
From this directory:

```sh
rsvg-convert -w 1240 provgate-lockup.svg      -o exports/lockup-light.png
rsvg-convert -w 1240 provgate-lockup-dark.svg -o exports/lockup-dark.png
rsvg-convert -w 1680 architecture.svg         -o exports/architecture-light.png
rsvg-convert -w 1680 architecture-dark.svg    -o exports/architecture-dark.png
rsvg-convert -w 256  provgate-mark.svg        -o exports/mark-light.png
rsvg-convert -w 256  provgate-mark-dark.svg   -o exports/mark-dark.png
```
