# Huangque Local Fonts

These files self-host the UI fonts used by the site so workbench pages do not
depend on Google Fonts at runtime.

- `noto-sans-sc-*.woff2`: subsetted from Noto Sans SC, using the characters
  present in the site UI source plus common punctuation and ASCII.
- `jetbrains-mono-*.woff2`: subsetted from JetBrains Mono for ASCII, numbers,
  and common UI symbols.

Both upstream font families are distributed under the SIL Open Font License.

Subtitle display fonts:

- `smiley-sans-oblique.woff2`: Smiley Sans v2.0.1 from the official
  `atelier-anchor/smiley-sans` release. The font is distributed under the SIL
  Open Font License; see `OFL-Smiley-Sans.txt`.
- `zcool-kuaile-regular.woff2`: ZCOOL KuaiLe from the official Google Fonts
  repository, converted losslessly from the upstream TTF to WOFF2. The font is
  distributed under the SIL Open Font License; see `OFL-ZCOOL-KuaiLe.txt`.

The subtitle fonts are loaded only when selected. The same WOFF2 files can be
installed through fontconfig for libass subtitle rendering, keeping browser
preview and generated video typography consistent.
