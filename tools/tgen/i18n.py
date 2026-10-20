# SPDX-License-Identifier: MIT
"""UI labels: ui/i18n/labels.csv, one row per text, one column per language.

Columns: key, type, then one column per language (BCP 47 tag, e.g. en, sr-Cyrl).
Rows starting with "_lang." describe each language (name, flag, html lang, number
format). An empty cell falls back to English. Placeholders like {n} must appear
in every translation of a row.

    python3 tools/tgen/i18n.py            # check the file, list untranslated cells
    python3 tools/tgen/i18n.py --json DIR # write DIR/<lang>.json for the web UI
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

LABELS = Path(__file__).resolve().parents[2] / "ui" / "i18n" / "labels.csv"
FALLBACK = "en"


def load(path: Path = LABELS) -> tuple[list[str], dict[str, dict[str, str]], dict[str, str]]:
    """-> (languages, {lang: {key: text}} with fallback applied, {key: type})"""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    head = rows[0]
    if head[:3] != ["key", "type", FALLBACK]:
        raise ValueError(f"{path}: header must start with key,type,{FALLBACK}")
    langs = head[2:]
    table: dict[str, dict[str, str]] = {lang: {} for lang in langs}
    types: dict[str, str] = {}
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        row += [""] * (len(head) - len(row))
        key, typ = row[0], row[1]
        if key in types:
            raise ValueError(f"{path}:{i}: duplicate key {key}")
        types[key] = typ
        for j, lang in enumerate(langs):
            table[lang][key] = row[2 + j] or row[2]     # fallback: English
    return langs, table, types


def check(path: Path = LABELS) -> list[str]:
    langs, table, _ = load(path)
    with open(path, newline="", encoding="utf-8") as f:
        raw = {r[0]: r for r in csv.reader(f) if r}
    problems = []
    for key, en in table[FALLBACK].items():
        if not en.strip() and not key.startswith("_lang."):
            problems.append(f"{key}: empty English text")
        want = sorted(set(re.findall(r"\{\w+\}", en)))
        for j, lang in enumerate(langs[1:], start=3):
            cell = raw[key][j] if len(raw[key]) > j else ""
            if not cell:
                problems.append(f"{key}: no {lang} translation (English is shown)")
            elif sorted(set(re.findall(r"\{\w+\}", cell))) != want:
                problems.append(f"{key}: {lang} placeholders differ from English {want}")
    return problems


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--json":
        langs, table, _ = load()
        out = Path(sys.argv[2])
        out.mkdir(parents=True, exist_ok=True)
        for lang in langs:
            (out / f"{lang}.json").write_text(json.dumps(table[lang], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {', '.join(f'{lang}.json' for lang in langs)} to {out}")
        return 0
    langs, table, _ = load()
    problems = check()
    print(f"{LABELS.name}: {len(table[FALLBACK])} keys, languages: "
          + ", ".join(f"{lang} ({table[lang]['_lang.name']})" for lang in langs))
    for p in problems:
        print("  " + p)
    return 1 if any("placeholders" in p or "empty" in p for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
