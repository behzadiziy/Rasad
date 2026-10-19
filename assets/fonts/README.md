# Fonts

Resume PDFs use **Vazirmatn**, a Persian/Arabic typeface that also includes Latin glyphs.

| File | Source in the release archive |
| --- | --- |
| `Vazirmatn-Regular.woff2` | `fonts/webfonts/Vazirmatn-Regular.woff2` |
| `Vazirmatn-Bold.woff2` | `fonts/webfonts/Vazirmatn-Bold.woff2` |
| `OFL.txt` | `OFL.txt` (the license) |

- **Project:** https://github.com/rastikerdar/vazirmatn
- **Release:** v33.003 (published 2022-06-22), the latest release when checked on 2026-10-19:
  https://github.com/rastikerdar/vazirmatn/releases/tag/v33.003
- **Archive:** `vazirmatn-v33.003.zip`, SHA-256 `0a9afd41967e6f57096a56a181a23f81a2b999b62f1f2a4e4b26736580854fdb`
- **Font version** (in the font files): 33.197
- **License:** SIL Open Font License 1.1, see `OFL.txt`. The fonts are redistributed unmodified.

`src/resume/render.py` embeds both files as base64 data URIs in the CSS at
render time, so rendering a PDF needs no file or network access from the browser.
