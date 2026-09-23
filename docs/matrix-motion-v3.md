# Additional template-video modes

Companion generator release adds `inset-flip-whip`, `fixed-opening-whip`, and
`bilingual-stagger-salon`. Catalog discovery remains server-owned; there are no
hard-coded placeholder entries exposed when the generator lacks these IDs.

The first two use existing template inputs and voice/BGM options. For the
bilingual template the page relabels `bottom_text` as subtitle, locks narration
on and BGM off, and uses the existing owned/public voice selector. It does not
add a persistent bottom CTA or alter the original bilingual typography.

The generation path reuses TTS caching, obtains genuine provider word timestamps
through the existing ASR configuration, and uses the semantic model for exact
Chinese segmentation and English translations. Word timing may be subdivided
within an observed multi-character word; segment/whole-audio estimates are not
accepted. The Chinese script must remain unchanged. The timeline is persisted
before submitting a provider job and bound to the audio hash. Recovery must not
translate/re-align or submit a different timeline after an uncertain submission.

The renderer receives timing/caption JSON, not an audio filesystem path. Final
narration mux copies the original video stream, preserving HDR metadata and the
0.6-second tail. Existing templates keep their previous mux behavior.

The new modes follow the generator's account-upload-only visual policy. When no
explicit assets are supplied, only this account's unexpired video uploads are
eligible. The fixed modes choose distinct sources and valid random offsets;
bilingual mode freezes the candidate hashes/durations before TTS, then freezes
the final sources together with the measured caption plan before submission.
Explicit materials preserve their order/offsets and must cover the narration.
Missing or short account assets fail; no shared-library fallback is added.

Deploy this companion before enabling the new generator catalog, then stage the
new template packs and GPU capabilities on idle workers. No live
service or Skill repository is changed by this PR. ASR/translation/TTS credentials
are not included; live provider calls were not used as part of code validation.
