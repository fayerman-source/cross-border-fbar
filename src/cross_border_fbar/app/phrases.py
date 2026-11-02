"""Gettext catalogs of fixed client phrases, with explicit human review status.

    python -m cross_border_fbar.app.phrases --refresh --printout

Last-Translator names the human reviewer. Clear each entry's fuzzy flag only after
reviewing it. A changed English msgid is always unreviewed, even with that header.
"""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po

PHRASES_DIR = Path(__file__).parent / "translations"
LANGUAGES = {"ko": "Korean"}
TABLE_FILES = {code: f"{code}.po" for code in LANGUAGES}
PRINTOUT_FILES = {"ko": Path("docs/korean-review.txt")}
GLOSSARY = "https://www.irs.gov/pub/irs-prior/p850enkr--2023.pdf"


def table_path(language: str) -> Path:
    name = TABLE_FILES.get(language)
    if name is None:
        raise ValueError(f"no client language {language!r}; known: {sorted(LANGUAGES)}")
    return PHRASES_DIR / name


def current_phrases() -> dict[str, str]:
    from .review import CLIENT_LABELS, SUMMARY_PHRASES

    return {**SUMMARY_PHRASES, **CLIENT_LABELS}


def load(language: str) -> Catalog:
    path = table_path(language)
    if path.exists():
        with path.open("rb") as f:
            return read_po(f, locale=language)
    return Catalog(
        locale=language,
        project="cross-border-fbar",
        last_translator="",
        fuzzy=False,
        header_comment="# Client summary phrases. Last-Translator names the human reviewer.\n"
        "# Remove fuzzy only after human review. Empty Last-Translator means unreviewed.\n"
        f"# Terminology: IRS Pub 850 (EN-KR), September 2023: {GLOSSARY}\n",
    )


def save(language: str, catalog: Catalog) -> Path:
    path = table_path(language)
    path.parent.mkdir(exist_ok=True)
    stream = BytesIO()
    write_po(stream, catalog, width=110, include_previous=True, sort_output=True)
    path.write_bytes(stream.getvalue().rstrip(b"\n") + b"\n")
    return path


def reviewed(language: str, phrases: dict[str, str]) -> dict[str, str]:
    catalog = load(language)
    reviewer = catalog.last_translator.strip()
    if not reviewer or reviewer.startswith("FULL NAME") or catalog.fuzzy:
        return {}
    out = {}
    for key, english in phrases.items():
        message = catalog.get(english, context=key)
        if message and message.string and not message.fuzzy:
            out[key] = message.string
    return out


def drafts(language: str, phrases: dict[str, str]) -> dict[str, str]:
    """Current catalog text, including fuzzy drafts, which still requires the preparer's confirmation."""
    catalog = load(language)
    return {
        key: message.string
        for key, english in phrases.items()
        if (message := catalog.get(english, context=key)) is not None and message.string
    }


def sync(language: str, phrases: dict[str, str] | None = None) -> Catalog:
    """Match by stable context. Stale English retains its draft as fuzzy, never as reviewed."""
    phrases = current_phrases() if phrases is None else phrases
    catalog = load(language)
    by_context = {m.context: m for m in catalog if m.id}
    for key, english in phrases.items():
        old = by_context.get(key)
        if old and old.id == english:
            continue
        if old:
            catalog.delete(old.id, context=key)
        catalog.add(
            english,
            old.string if old else "",
            context=key,
            flags={"fuzzy"},
            previous_id=[old.id] if old else [],
            auto_comments=["label: noun phrase, plural-safe, no semicolon"],
        )
    obsolete = [
        (message.id, message.context) for message in catalog if message.id and message.context not in phrases
    ]
    for message_id, context in obsolete:
        catalog.delete(message_id, context=context)
    return catalog


def build(language: str) -> Path:
    """Re-record all phrases in one call and mark every result unreviewed."""
    from .review import _translation_problem
    from .server import translate_phrases

    phrases = current_phrases()
    translations, _ = translate_phrases(phrases, LANGUAGES[language], fresh=True)
    problem = _translation_problem(phrases, translations)
    if problem:
        raise SystemExit(f"translation not usable: it {problem}")
    catalog = sync(language, phrases)
    catalog.last_translator = ""
    for key, english in phrases.items():
        message = catalog.get(english, context=key)
        message.string = translations[key]
        message.flags.add("fuzzy")
    return save(language, catalog)


def printout(language: str) -> str:
    catalog = load(language)
    phrases = current_phrases()
    known = reviewed(language, phrases)
    reviewer = catalog.last_translator.strip() or "____________________"
    review_date = catalog.revision_date.strftime("%Y-%m-%d") if known else "____________________"
    lines = [
        "Client summary phrase review: English | Korean",
        "",
        f"Reviewer: {reviewer}    Date: {review_date}",
        "Record the reviewer in Last-Translator and remove fuzzy only for approved entries.",
        f"Terminology source: IRS Pub 850 (EN-KR), September 2023: {GLOSSARY}",
        "",
    ]
    for n, (key, english) in enumerate(phrases.items(), 1):
        message = catalog.get(english, context=key)
        text = message.string if message else "[translation needed]"
        status = "reviewed" if key in known else "UNREVIEWED"
        lines += [f"{n}. {key} ({status}, noun phrase)", f"   {english} | {text}"]
        if message:
            lines.extend(f"   {note}" for note in message.user_comments if note.startswith("Review note:"))
        lines.append("")
    return "\n".join(lines)


def write_printout(language: str, root: Path | None = None) -> Path:
    """Write only the language's fixed project document, including symlink confinement."""
    from .review import confined

    relative = PRINTOUT_FILES.get(language)
    if relative is None:
        raise ValueError(f"no printout for client language {language!r}")
    path = confined(relative, root or Path.cwd())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(printout(language))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", default="ko", choices=LANGUAGES)
    parser.add_argument("--refresh", action="store_true", help="one live translation call")
    parser.add_argument("--printout", action="store_true", help="write the fixed review document under docs/")
    args = parser.parse_args()
    if args.refresh:
        build(args.language)
    else:
        save(args.language, sync(args.language))
    if args.printout:
        write_printout(args.language)
    else:
        print(printout(args.language))


if __name__ == "__main__":
    main()
