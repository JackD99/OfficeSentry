# Office Sentry design

How Office Sentry looks, and why: the colours, type, shapes and the rules the shared components follow.
Read it before you change how a page looks. The values live in
[`v2/officesentry/web/static/tokens.css`](v2/officesentry/web/static/tokens.css), and `/styleguide` in a
running portal shows every token and component in light and dark. Where this file and the tokens disagree,
the tokens win and this file is out of date.

## The idea: a well-kept statement

Office Sentry tells an MSP, and through the MSP its clients, how their Microsoft 365 stands: how many
accounts, how much waste, what needs fixing first. People act on those figures and pass them on, so the
look borrows from documents that people already trust with numbers: an audited statement, a well-set annual
report. Ruled sections instead of floating cards, figures set like a total, quiet labels, ink on paper and
one deep green.

The look comes from type, colour and rules. Layout, navigation and controls stay as people expect them: a
menu on the left, tabs, tables, ordinary buttons and forms. Nothing is decorated for its own sake.

## Principles

1. **Figures lead.** A page's headline figures come first and are the largest text on it, set in the serif
   with lining, tabular numerals so they line up. When figures sit in a row, the first is the one that
   matters most and is set biggest.
2. **Ruled, not floating.** Sections are separated by borders and rules. Cards have a border and no shadow.
   Only what drops over the page (a menu, a notice) has a shadow. One elevation per element.
3. **Ink on paper.** Green-black ink on stone paper, a bottle-green menu, and one accent green for links,
   primary buttons and focus. Dark mode is the same idea at night, not an inverted copy.
4. **Colour means something.** Red, orange, amber and grey are severity; green, amber and red are status.
   They are never decoration, and never the only signal: a severity always carries its word, a check
   square its letter.
5. **Quiet labels.** Labels over a group (table headings, menu groups, report areas) are small capitals,
   never shouted capitals.
6. **Readable everywhere.** Every text colour meets WCAG AA (4.5:1) on every surface it sits on, in both
   themes; no text is smaller than 12px; prose keeps to a readable measure.

## Colour

All colours are tokens; `app.css` and `report.css` define none of their own (`v2/tests/test_styleguide.py`
checks). The values below are light / dark.

| Role | Tokens | Light / dark |
|---|---|---|
| Paper | `--bg`, `--surface`, `--surface-2`, `--sunken` | stone `#f3f4f1` and white / deep green-black `#0e1412`, `#141c19` |
| Ink | `--text`, `--text-2`, `--muted`, `--faint` | `#17201c` to `#5b6660` / `#e7ede9` to `#8a9a92` |
| Lines | `--border`, `--border-strong`, `--decor` | pale green-greys; `--decor` is for lines and icons only, never text |
| The ink rule | `--rule` | `#17201c` / `#c9d3ce`: under table headings, the current tab, and closing a row of figures |
| Accent | `--accent`, `--accent-hover`, `--accent-soft`, `--accent-text` | bottle green `#1d5c4b` / mint `#79c9aa` |
| Menu | `--sidebar*`, `--logo-from`, `--logo-to` | `#12302a` / `#0a1613`, dark in light and dark mode |
| Status | `--ok`, `--warn`, `--bad`, `--info` (and `-soft`) | for states: collected, failing, paused |
| Severity | `--sev-critical`, `--sev-high`, `--sev-medium`, `--sev-low` (and `-soft`, `-fill`) | for findings only |
| Charts | `--chart-series`, `--chart-2`, `--chart-3`, `--chart-grid`, `--chart-track` | three series at most, in that order |

Rules:

- Use the role, not the colour: a link is `--accent`, a muted note `--muted`, a finding's level `--sev-*`.
- Severity text (`--sev-*`) is for words; `--sev-*-fill` is for bars, dots and chart segments.
- Every text colour meets 4.5:1 on `--bg`, `--surface`, `--surface-2` and `--sunken` in both themes
  (`v2/tests/test_ux_fixes.py` measures them). A new text colour gets measured the same way.
- Dark mode follows the device unless the person chose Light or Dark on their Account page
  (`data-theme` on `<html>`). PDFs and print pages always set `<html data-theme="light">`.
- `reports/charts.py` writes the light chart and severity colours into the SVG for PDFs; change both
  together (`test_styleguide.py` checks they match).

### Client reports keep the MSP's colours

Client-facing output (report pages, PDFs, emails, the client's own pages) uses the same tokens for paper,
ink, lines and severity, but its accent is the MSP's brand (`--brand`, `--brand-ink`, ... from
`/brand.css`, inline in PDFs). A client report never shows Office Sentry's green where the MSP has set its
own colour.

Client reports and their PDFs also keep Source Sans 3 for their titles and headline figures: they are the
MSP's documents, and the serif is Office Sentry's own voice, kept for the portal.

### Colour themes

Staff can pick a colour for their own screen on the Account page: bottle green (the default), navy, claret,
graphite or plum (`web/appearance.py`, `static/themes.css`). A theme changes only the Accent and Menu tokens;
paper, ink, lines, status and severity stay the same, so a finding reads the same in every theme. Each
theme meets the same contrast rules in light and dark (`v2/tests/test_appearance.py` measures them). A
theme is never loaded by `report.css`, so it never reaches a client report, a PDF or an email, and client
accounts only choose light or dark.

To add a theme: a block per mode in `themes.css` (light, dark when the device is dark, dark when the person
chose it), an entry in `appearance.THEMES`, and a look at the menu, a primary button and a selected tab in
both modes.

## Type

Two families, both bundled in `v2/officesentry/web/static/fonts/` (Adobe's own files, SIL Open Font
License), so pages and PDFs set text the same way and nothing is fetched from elsewhere:

- **Source Sans 3** (`--font`) for everything people read and use: body text, tables, labels, buttons.
- **Source Serif 4**, its Subhead optical size (`--serif`), for page titles, card titles, empty-state titles
  and headline figures.

| Token | Size | For |
|---|---|---|
| `--fs-2xs` | 12px | the smallest text: tiny labels, the menu's version line |
| `--fs-xs` | 13px | pills, small notes |
| `--fs-sm` | 14px | secondary text, table headings (in small capitals), footnotes |
| `--fs-base` | 15px | body text, table cells, form fields |
| `--fs-md` | 16px | group labels in small capitals |
| `--fs-lg` | 18px | card titles (serif) |
| `--fs-xl` | 22px | page titles on phones |
| `--fs-2xl` | 28px | page titles (serif) |
| `--fs-figure` | 30px | headline figures (serif) |
| `--fs-3xl` | 32px | the largest figure on a page |
| `--fs-lead` | 38px | the lead figure of a statement line |

Rules:

- Nothing under 12px, anywhere a person reads it.
- Figures in tables and headline figures are tabular (`font-variant-numeric: tabular-nums`, with
  `lining-nums` in the serif), so columns of numbers line up.
- Labels over a group use `font-variant-caps: all-small-caps` with a little letter spacing, one step larger
  than the text around them so the small capitals read at the same size. Not `text-transform: uppercase`.
- Prose under a table or a form (`.footnote`) keeps to 70 characters a line.
- Monospace (`--mono`) is for things people copy exactly: commands, IDs, thumbprints, codes. A domain or an
  address is text, not code.
- Weights: 400 for text, 500 for menu items and buttons, 600 for titles, headings and emphasis, 700 for the
  odd word that must stand out. The serif is used at 600.

## Space and shape

- Spacing is a 4px grid (`--space-1` is 4px, `--space-4` 16px), with half steps for tight controls.
- Radii are small: `--radius` 4px for cards and panels, `--radius-sm` 3px for buttons and fields,
  `--radius-xs` 2px for tags and severity keys. `--radius-pill` is for the few things that are truly
  round-ended.
- Cards and tiles have a border and no shadow (`--shadow` is `none`). Menus that drop over the page use
  `--shadow-menu`, floating notices `--shadow-lg`.
- A selected segment, a pinned header or a current tab is marked with a line or a border, not a shadow.

## Components

`/styleguide` shows each one; `v2/officesentry/web/templates/_macros.html` builds most of them.

- **Headline figures** (`.tiles`): one statement line. The figures sit side by side, separated by thin
  rules, an ink rule above and a double rule in `--rule` beneath; the first figure is the lead, set in
  `--fs-lead`, so put the figure that answers the page's question first. Labels in small capitals above,
  the change since last time below. Inside a card, a few figures (`.mini-stats`) are the same line without
  the double rule: never boxes inside a box.
- **Cards** (`.card`): a ruled section with a serif title, a border and no shadow.
- **Tables**: headings in small capitals on the surface, with a `--rule` line under them; tabular figures;
  a flagged row carries its severity (key and word) beside its first value, and a thin band in the same
  colour down its left edge so a long table can be scanned.
- **Tabs** (`.tabs`, and the settings tabs): the current tab is underlined in `--rule` and set at 600.
- **Severity**: one shape everywhere, a small square key in the severity colour followed by its word in
  small capitals (Critical, High, Medium, Low), the key and the word in the level's text colour.
  Counts by severity (`.sev`) are the same square holding the number, in a fixed order: critical, high,
  medium, low.
- **Pills and tags**: each has one job and one look. Counts are plain tabular figures beside their label;
  states (Paused, Archived) are soft tags; NEW and FIXED are small-capital tags; downloads (PDF, XLSX) are
  buttons, not pills. Tags and chips have square corners (`--radius-xs`, `--radius-sm`); only a progress
  bar is round-ended.
- **Buttons**: one primary action per area in `--accent`; the rest are outlined. Danger actions say so in
  `--bad` and ask before they act.
- **Icons**: one set, drawn on a 24px grid with round ends, in `_macros.html` (`icon()`). They sit beside a
  word, never alone without a label or a tooltip.

## Checking a change

- `/styleguide` and `/styleguide/report` (and its PDF) in light and dark.
- `pytest tests/test_styleguide.py tests/test_ux_fixes.py` (tokens and contrast), from `v2/`.
- The browser tests and layout checks (`v2/tests/browser/README.md`, `v2/tests/layout/README.md`) on any
  pull request that touches templates or CSS; add the `refresh-baselines` label when a screenshot or an
  example PDF changes because you meant it to, and look at the new images.
