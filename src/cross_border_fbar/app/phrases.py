"""Reviewed translations of the client summary's fixed phrases, one file per language.

    python -m cross_border_fbar.app.phrases ko    # translate every phrase into translations/ko.json

A table counts as reviewed only once a person has read it and set "reviewed_by". Each entry keeps the
English it translates, so a phrase whose English changed is treated as untranslated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PHRASES_DIR = Path(__file__).parent / "translations"
LANGUAGES = {"ko": "Korean"}  # code -> name the model is asked to translate into
TABLE_FILES = {code: f"{code}.json" for code in LANGUAGES}


def table_path(language: str) -> Path:
    """The file name comes from TABLE_FILES, never from the input, so no input can point it elsewhere."""
    name = TABLE_FILES.get(language)
    if name is None:
        raise ValueError(f"no client language {language!r}; known: {sorted(LANGUAGES)}")
    return PHRASES_DIR / name


def reviewed(language: str, phrases: dict[str, str]) -> dict[str, str]:
    """Reviewed translations for these phrases (key -> text); missing or stale keys are left out."""
    path = table_path(language)
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text())
    if not doc.get("reviewed_by"):
        return {}
    entries = doc["phrases"]
    return {k: entries[k]["text"] for k, en in phrases.items() if k in entries and entries[k]["en"] == en}


def build(language: str) -> Path:
    """Translate every phrase into a new, unreviewed table. A person reviews it and sets reviewed_by."""
    from .review import CLIENT_LABELS, SUMMARY_PHRASES, _translation_problem
    from .server import translate_phrases

    path = table_path(language)
    phrases = {**SUMMARY_PHRASES, **CLIENT_LABELS}
    translations, meta = translate_phrases(phrases, LANGUAGES[language])
    problem = _translation_problem(phrases, translations)
    if problem:
        raise SystemExit(f"translation not usable: it {problem}")
    doc = {
        "language": language,
        "translated_by": f"{meta['model']} ({meta['prompt']})",
        "reviewed_by": "",
        "phrases": {k: {"en": en, "text": translations[k]} for k, en in phrases.items()},
    }
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "ko"))
