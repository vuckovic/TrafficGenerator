# UI labels and languages

`labels.csv` holds every text of the web interface: one row per text, one column per language.

| Column | Meaning |
|---|---|
| `key` | stable identifier used by the UI code (`area.name`, e.g. `dutcfg.btn.push`) |
| `type` | what the text is: label, button, option, tooltip, text, heading, column, state, metric, aria, unit, meta |
| `en` | English: the reference and the fallback |
| `sr-Cyrl` | Serbian, Cyrillic script |

**Rules:**
- An empty cell shows the English text.
- `{placeholders}` (e.g. `{n}`) must appear in every language of a row; `tools/tgen/i18n.py` checks this.
- Edit with any spreadsheet program, saved as **CSV UTF‑8, comma‑separated**.

**Adding a language:**
1. Add a column with the language's BCP 47 tag, e.g. `sr-Latn`, `de`.
2. Fill the `_lang.*` rows:
   - `_lang.name`: the language's name in that language, shown in the selector;
   - `_lang.flag`: the flag file name in `flags/` (ISO 3166 code), so add `flags/<code>.svg` too;
   - `_lang.html`: the `lang` attribute;
   - `_lang.number_group` / `_lang.decimal`: number format.
3. Translate the rows and run `python3 tools/tgen/i18n.py`. It lists missing translations and placeholder mismatches.

The language selector (control G9) is the flag in the top‑right corner of every page. The choice is kept per browser. The first visit follows the browser language and falls back to English.

**Flags:**
- `gb.svg` is drawn here.
- `rs.svg` is the state flag of Serbia with the coat of arms, at 3:2. It comes from [flag-icons](https://github.com/lipis/flag-icons) 7.2.3 (MIT, see `LICENSE-flag-icons`), un-cropped from 4:3 to 3:2 and optimised with svgo.
