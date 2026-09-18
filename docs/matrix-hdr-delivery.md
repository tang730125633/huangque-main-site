# Template HDR master delivery

Template workers may return an H.264 SDR video or an HEVC Main 10 HLG/PQ HDR
master. Voiceover composition continues to use `-c:v copy`: only audio is
encoded. Both voice-only and voice-with-BGM paths verify that codec, pixel
format, primaries, transfer, matrix and range are unchanged by muxing.

Eight-bit H.264 tagged as HLG/PQ is rejected, as is HEVC without a supported
ten-bit BT.2020 HDR profile. Existing H.264 SDR jobs remain supported.
The relay forwards the worker's measured `color_profile` while retaining its
own delivery URLs, and the main job result carries that metadata forward.

Deploy this backward-compatible delivery change before enabling HDR output
on generation workers. Each worker must receive the generation color fix;
this main-site change alone does not restore color lost in source preparation.
No second compatibility video or new front-end controls are added. HDR playback
depends on the client decoder/display. Masters do not retain Dolby Vision
dynamic metadata after composition.
