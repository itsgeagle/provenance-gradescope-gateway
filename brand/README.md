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

## Files

| File                          | Use                                                    |
| ----------------------------- | ------------------------------------------------------ |
| `provgate-mark.svg` / `-dark` | symbol only                                            |
| `provgate-lockup.svg` / `-dark` | symbol + wordmark (README header, via `<picture>`)   |
| `architecture.svg` / `-dark`  | system diagram (README, via `<picture>`)               |

SVGs are the editable masters and are embedded directly in the README. Light/dark selection
is handled with a `<picture>` + `prefers-color-scheme` element. The wordmark uses the
Tailwind default system-sans stack at weight 700, matching the Provenance wordmark.
