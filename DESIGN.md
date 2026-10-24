# Office Sentry design

How Office Sentry looks, and why: the colours, type, shapes and the rules the shared components follow.
Read it before you change how a page looks. The values live in
[`v2/officesentry/web/static/tokens.css`](v2/officesentry/web/static/tokens.css), and `/styleguide` in a
running portal shows every token and component in light and dark. Where this file and the tokens disagree,
the tokens win and this file is out of date.

## The idea: a working console

Office Sentry is where an MSP's engineers check dozens of clients' Microsoft 365 between other jobs. They
already live in admin centres, PSA and RMM tools, so the portal looks and behaves like one of those: dense,
plain and predictable. Many rows on a screen, figures that line up, a toolbar above every table, and nothing
on a page that is there to look nice.

Layout, navigation and controls stay as people expect them: a menu on the left, a client's tabs, tables
with sortable headings, ordinary buttons and forms. The look comes from a tight type scale, one sans family,
plain grey surfaces and colour kept for what needs someone.

## Principles

1. **Data first.** Tables and figures get the room; headings, notes and chrome are small. A laptop shows
   the page's figures and the top of its table without scrolling.
2. **One family, small steps.** Everything is set in the same sans. Titles are a few pixels larger and
   heavier than the text around them, never a display face.
3. **Boxes with borders.** Cards, figure panels and menus have a 1px border and no shadow. Only what drops
   over the page (a menu, a notice, a side sheet) has a shadow. One elevation per element.
4. **Colour means something.** Red, orange, amber and grey are severity; green, amber and red are status;
   the accent is for links, the primary button, focus and the current tab. Colour is never decoration and
   never the only signal: a severity always carries its word, a check square its letter.
5. **Plain labels.** Labels (column headings, figure names, menu groups) are short, sentence case and
   grey. No small capitals, no shouted capitals.
6. **Say less.** A title and a one-line note are enough; longer help goes behind a link or in the docs.
7. **Readable everywhere.** Every text colour meets WCAG AA (4.5:1) on every surface it sits on, in both
   themes; no text is smaller than 12px.

## Colour

All colours are tokens; `app.css` and `report.css` define none of their own (`v2/tests/test_styleguide.py`
checks). The values below are light / dark.

| Role | Tokens | Light / dark |
|---|---|---|
| Surfaces | `--bg`, `--surface`, `--surface-2`, `--sunken` | grey `#f4f4f2` and white / charcoal `#111214`, `#17191b` |
| Text | `--text`, `--text-2`, `--muted`, `--faint` | `#1c1e20` to `#5d6267` / `#e8e9ea` to `#909498` |
| Lines | `--border`, `--border-strong`, `--decor` | plain greys; `--border-strong` under column headings; `--decor` is for lines and icons only, never text |
| Accent | `--accent`, `--accent-hover`, `--accent-soft`, `--accent-text` | ink `#2b4a6b` / pale ink `#9fb8d4` |
| Menu | `--sidebar*`, `--logo-from`, `--logo-to` | light grey `#e9eae7` / near-black `#0b0c0d`; the mark's tile is ink `#2b4a6b` |
| Status | `--ok`, `--warn`, `--bad`, `--info` (and `-soft`) | for states: collected, failing, paused |
| Severity | `--sev-critical`, `--sev-high`, `--sev-medium`, `--sev-low` (and `-soft`, `-fill`) | for findings only |
| Charts | `--chart-series`, `--chart-2`, `--chart-3`, `--chart-grid`, `--chart-track` | three series at most, in that order |

Rules:

- Use the role, not the colour: a link is `--accent`, a muted note `--muted`, a finding's level `--sev-*`.
- Severity text (`--sev-*`) is for words, on the surfaces or its own `-soft`; `--sev-*-fill` is for bars,
  dots and chart segments.
- Every text colour meets 4.5:1 on `--bg`, `--surface`, `--surface-2` and `--sunken` in both themes
  (`v2/tests/test_ux_fixes.py` measures them). A new text colour gets measured the same way.
- Dark mode follows the device unless the person chose Light or Dark on their Account page
  (`data-theme` on `<html>`). PDFs and print pages always set `<html data-theme="light">`.
- `reports/charts.py` writes the light chart and severity colours into the SVG for PDFs; change both
  together (`test_styleguide.py` checks they match).

### Client reports keep the MSP's colours

Client-facing output (report pages, PDFs, emails, the client's own pages) uses the same tokens for surfaces,
text, lines and severity, but its accent is the MSP's brand (`--brand`, `--brand-ink`, ... from
`/brand.css`, inline in PDFs). A client report never shows Office Sentry's ink where the MSP has set its
own colour; an MSP that hasn't set one gets the ink (`DEFAULT_ACCENT` in `reports/branding.py`). Client
reports have their own type sizes and layout in `report.css`, which uses only `--fs-2xs`, `--fs-xs`,
`--fs-lg` and `--fs-3xl` from the scale below.

### Colour themes

Staff can pick a colour for their own screen on the Account page: ink (the default), bottle green, navy,
claret, graphite or plum (`web/appearance.py`, `static/themes.css`). Bottle green was the default in 2.4.0; a
stored `green` reads as today's default, so nobody's saved choice points at a missing theme. A theme changes
only the Accent and Menu tokens; surfaces, text, lines, status and severity stay the same, so a finding reads
the same in every theme. Each theme meets the same contrast rules in light and dark
(`v2/tests/test_appearance.py` measures them). A theme is never loaded by `report.css`, so it never reaches a
client report, a PDF or an email, and client accounts only choose light or dark.

To add a theme: a block per mode in `themes.css` (light, dark when the device is dark, dark when the person
chose it), an entry in `appearance.THEMES`, and a look at the menu, a primary button and a selected tab in
both modes.

## Type

One family, **Source Sans 3** (`--font`), bundled in `v2/officesentry/web/static/fonts/` (Adobe's own
files, SIL Open Font License), so pages and PDFs set text the same way and nothing is fetched from
elsewhere. The website keeps its own serif headings; the build copies its font files from the same folder.

| Token | Size | For |
|---|---|---|
| `--fs-2xs` | 12px | tags, severity, counts, menu group labels, the menu's version line |
| `--fs-xs` | 13px | small notes, footnotes under figures |
| `--fs-sm` | 13px | secondary text, column headings, form labels, figure labels |
| `--fs-base` | 14px | body text, table cells, form fields, buttons |
| `--fs-md` | 15px | card and section titles |
| `--fs-lg` | 18px | a page's own title under tabs, a worded figure |
| `--fs-xl` | 20px | page titles on phones, figures on phones |
| `--fs-2xl` | 22px | page titles |
| `--fs-figure` | 24px | headline figures |
| `--fs-3xl` | 32px | client reports' cover figures only |

Rules:

- Nothing under 12px, anywhere a person reads it.
- Figures in tables and headline figures are tabular (`font-variant-numeric: tabular-nums`), so columns of
  numbers line up.
- Labels are sentence case at 600 in `--muted`; never `text-transform: uppercase` or small capitals. A
  stored lowercase word (a severity) is capitalised in the template (`|capitalize`).
- Prose under a table or a form (`.footnote`) keeps to 70 characters a line.
- Monospace (`--mono`) is for things people copy exactly: commands, IDs, thumbprints, codes. A domain or an
  address is text, not code.
- Weights: 400 for text, 500 for menu items, tabs and buttons, 600 for titles, labels and emphasis, 700 for
  the odd word that must stand out (a count in a severity square, a grade's letter).

## Space and shape

- Spacing is a 4px grid (`--space-1` is 4px, `--space-4` 16px), with half steps for tight controls.
- The page has 24px sides (16px on phones); cards are 16px top and bottom and 20px to the sides; sections
  are 16px apart.
- Table rows are compact: 7px above and below a line of 14px text, about 34px a row.
- Controls are about 32px tall (buttons, fields, selects); `.sm` ones about 26px.
- Radii are small: `--radius` 4px for cards and panels, `--radius-sm` 3px for buttons and fields,
  `--radius-xs` 2px for tags and severity. `--radius-pill` is for counts in tabs and the few things that
  are truly round-ended.
- Cards and figure panels have a border and no shadow (`--shadow` is `none`). Menus that drop over the
  page use `--shadow-menu`, floating notices `--shadow-lg`.
- A selected segment, a pinned header or a current tab is marked with a line or a tint, not a shadow.

## Components

`/styleguide` shows each one; `v2/officesentry/web/templates/_macros.html` builds most of them.

- **Headline figures** (`.tiles`): one bordered panel of equal boxes split by thin lines. Each box has a
  label, the figure at `--fs-figure` and a short note or the change since last time. Put the figure that
  answers the page's question first; it is not set larger. Inside a card, a few figures (`.mini-stats`) are
  one row split by lines, never boxes in a box.
- **Cards** (`.card`): a white box with a border and a 15px title. A card that holds a table is `.flush`:
  its title row, toolbar (search, filter, columns, copy, download) and table run edge to edge.
- **Tables**: headings small, grey and sentence case on a light band (`--surface-2`) with a
  `--border-strong` line under them; compact rows; tabular figures; the row under the pointer tints. A
  flagged row carries its severity tag beside its first value, and a thin band in the same colour down its
  left edge so a long table can be scanned. The row the keyboard is on (`j` and `k`) is tinted
  `--accent-soft` and edged in `--accent`.
- **Tabs** (`.tabs`, and the settings tabs): the current tab is underlined in `--accent` and set at 600; a
  count sits in a small grey pill. A second row (`.tab-pages`) is a row of small outlined links, the current
  one tinted.
- **Severity**: one shape everywhere, a small tag in the level's soft colour holding a square key and its
  word (Critical, High, Medium, Low) in the level's text colour. Counts by severity (`.sev`) are solid
  squares holding the number, in a fixed order: critical, high, medium, low. Status words in the same shape
  (Failed, Due today, Fixed) take the status colours.
- **Grades**: a bold letter in a tinted square in the colour of what holds it there (A and B green, C
  medium, D high, E critical); a client without one gets a dashed square.
- **Pills and tags**: each has one job and one look. Counts are plain tabular figures beside their label;
  states (Paused, Archived) are soft tags; New and Fixed are change tags; downloads (PDF, XLSX) are one
  joined control, not pills. Tags have square corners (`--radius-xs`).
- **Buttons**: one primary action per area in `--accent`; the rest are outlined. Danger actions say so in
  `--bad` and ask before they act.
- **Icons**: one set, drawn on a 24px grid with round ends, in `_macros.html` (`icon()`). They sit beside a
  word, never alone without a label or a tooltip.
- **The mark**: a flat-topped shield in solid white with its tick cut out, on a square ink tile with small
  corners (`--logo-from`, `--logo-to`, `--logo-text`; one flat colour, no gradient). It sits in the menu, the
  browser tab (`v2/officesentry/web/static/favicon.svg`, also the sign-in page's logo when the MSP has none)
  and the website (`website/assets/`: `favicon.svg`, `favicon.ico`, `apple-touch-icon.png` and the link
  preview `social-card.png`). Change them together. An MSP's own logo replaces it in the portal, never on the
  website.

## Checking a change

- `/styleguide` and `/styleguide/report` (and its PDF) in light and dark.
- `pytest tests/test_styleguide.py tests/test_ux_fixes.py` (tokens and contrast), from `v2/`.
- The browser tests and layout checks (`v2/tests/browser/README.md`, `v2/tests/layout/README.md`) on any
  pull request that touches templates or CSS; add the `refresh-baselines` label when a screenshot or an
  example PDF changes because you meant it to, and look at the new images.
