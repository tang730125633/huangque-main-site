# IP12 Demo On Zelong

Public entry: https://zelong.huangquechuanmei.com/ip12-demo/

The tracked entry embeds an immutable presentation release on the same origin.
`deploy/zelong/ip12-demo-release.json` pins every artifact by relative path,
byte size, and SHA-256. The generated HTML and video assets are intentionally
not committed: they contain user-approved presentation media, not site source.
Internal notes, ZIP files, local paths, and raw conversations are not published.

## Release Order

1. Prepare the approved presentation and exactly the files in the manifest.
2. Merge the entry and manifest through a passing PR to `main`.
3. Upload the artifact directory to a private staging directory on `ssh zelong`.
4. Verify all hashes, sizes, and the exact file set against the merged manifest.
5. Install into the manifest's previously unused release directory under
   `/var/www/huangquechuanmei`. Never overwrite an existing immutable release.
6. Deploy only `site/ip12-demo/index.html` from the merged commit using the
   existing `ship-zelong` dry-run and apply workflow.
7. Verify HTTPS entry, embedded page, every MP4, range requests, and browser UI.

No nginx change, service restart, homepage replacement, or production-host
deployment is required. Existing workbench routes remain unchanged.

## Presentation Regression

Run `node tests/ip12_demo_navigation.mjs /path/to/showcase.html` against the
exact release artifact before publishing. Back/forward navigation follows the
viewing trail without collapsing the tree; explicit collapse/reset controls
remain separate. Audio-bearing videos expose a sound toggle, and the subtitle
GIF links to its original audio-bearing recording. GIF-derived recordings do
not acquire an audio track through conversion.

To roll back, restore the previous tracked entry through the normal deployment
flow. Retain immutable releases so old links remain usable. For the initial
release, there is no previous demo entry to restore.
