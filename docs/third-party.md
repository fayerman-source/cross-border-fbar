# Third-party notices and license scope

The repository's original code is Apache-2.0. Third-party files and dependencies retain their own licenses; the project license does not relicense them.

## Fonts included in this repository

- Noto Sans: [OFL and copyright](../src/cross_border_fbar/fonts/OFL.txt); Noto Sans KR: [OFL and copyright](../src/cross_border_fbar/fonts/OFL-notosanskr.txt); Noto Sans Thai: [OFL and copyright](../src/cross_border_fbar/fonts/OFL-notosansthai.txt).
- Barlow Condensed: [OFL and copyright](../src/cross_border_fbar/app/static/fonts/OFL-barlowcondensed.txt).
- IBM Plex Mono: [OFL and copyright](../src/cross_border_fbar/app/static/fonts/OFL-ibmplexmono.txt), including reserved name Plex.
- Nanum Pen Script: [OFL and copyright](../src/cross_border_fbar/app/static/fonts/OFL-nanumpenscript.txt), including reserved Nanum names.
- Public Sans: [OFL and copyright](../src/cross_border_fbar/app/static/fonts/OFL-publicsans.txt), [USWDS modified-version license](../src/cross_border_fbar/app/static/fonts/LICENSE-publicsans.md), and [Libre Franklin upstream license](../src/cross_border_fbar/app/static/fonts/OFL-librefranklin.txt).

Embedded font metadata identifies the copyright holders and OFL URLs. On October 30, 2026, SHA-256 comparisons matched all six vendored WOFF2 files byte-for-byte to the corresponding current Google Fonts Latin downloads, including variable-weight Public Sans. These are redistributed as received; their font licenses remain OFL. OFL permits bundling with software subject to its conditions, including reserved-name rules for modifications. Retain the family-specific notices and recheck those rules if modifying the fonts.

Matched Google Fonts files (SHA-256 of the vendored file):

- [BarlowCondensed-500.woff2](https://fonts.gstatic.com/s/barlowcondensed/v13/HTxwL3I-JCGChYJ8VI-L6OO_au7B4-Lwz3bWuQ.woff2): `460f141ec8f6c9a1516bfd2bd9fe71656246d7a9d04a0955faf53158d8970c4c`
- [BarlowCondensed-600.woff2](https://fonts.gstatic.com/s/barlowcondensed/v13/HTxwL3I-JCGChYJ8VI-L6OO_au7B4873z3bWuQ.woff2): `215a93c696f442034a46fbb382958f753fda60e30490683aeea6b235fcbb2b66`
- [IBMPlexMono-400.woff2](https://fonts.gstatic.com/s/ibmplexmono/v20/-F63fjptAgt5VM-kVkqdyU8n1i8q1w.woff2): `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7`
- [IBMPlexMono-500.woff2](https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3twJwlBFgg.woff2): `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22`
- [NanumPenScript-400.woff2](https://fonts.gstatic.com/s/nanumpenscript/v25/daaDSSYiLGqEal3MvdA_FOL_3FkN6zn0aQ.woff2): `899aa0335c147f0574f17a09fce2e301179cb0232bc96e17feded9d1a06ad5aa`
- [PublicSans-400.woff2](https://fonts.gstatic.com/s/publicsans/v21/ijwRs572Xtc6ZYQws9YVwnNGfJ4.woff2): `5ed4d31c988e73b258894244f209069ebe77dc7e564861954b21198b6de90d68`

## Models

Meta's [Muse Glimmer 30B GGUF model card](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF) identifies Meta Superintelligence Lab as author and Apache-2.0 as the model license. This repo contains recorded synthetic responses, not model weights. Anyone redistributing downloaded weights must retain the model's own license and applicable notices. The hosted Muse Spark API is governed by its service terms; the project license does not license the hosted model.

## Installed dependencies

These licenses were checked against installed package metadata for the locked Python 3.10 environment, not every platform-specific wheel in the lockfile. Direct runtime packages: Babel 2.18.0 (BSD-3-Clause), fpdf2 2.8.9 (LGPL-3.0-only), pypdfium2 5.14.0 (BSD-3-Clause/Apache-2.0 plus PDFium and bundled-component licenses), uharfbuzz 0.56.4 (Apache-2.0), Pillow 12.3.0 (MIT-CMU with separately licensed bundled libraries), and pillow-heif 1.8.1 (BSD-3-Clause with separately licensed bundled libraries). Transitive runtime packages include fonttools (MIT) and defusedxml (PSFL). The installed development/test environment also reports pytest, iniconfig, pluggy, Ruff, tomli and exceptiongroup under MIT; Pygments under BSD-2-Clause; packaging under Apache-2.0 OR BSD-2-Clause; and typing-extensions under PSF-2.0. See each distribution's `dist-info/licenses` for its full notices and binary dependencies. fpdf2 is installed as a separate package; redistributing it or bundled binaries can trigger additional license obligations. Other platform wheels, development dependencies and hosted-service terms require review for the intended release package; no blanket legal clearance is claimed.
