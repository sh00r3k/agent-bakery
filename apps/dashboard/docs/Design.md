# Agents Dashboard — Design System

Agent-readable spec for the dashboard redesign. **Encode, don't describe**: the
design system lives in the repo as `src/dashboard/static/tokens.css` (the single
source of truth) + this file + the component patterns below. When you build or
restyle a screen, consume tokens — **never write a hex literal in feature CSS or
templates**. A grep gate (below) enforces this.

---

## 1. Aesthetic direction — "terminal mission-control"

This is an **OPS / mission-control console**, not a marketing site. Dense,
precise, engineered. Near-black layered surfaces; the UI is **calm until
something is wrong**, then a sharp signal color appears.

- **Mood**: control room. Quiet neutrals, one signal accent, semantic status.
  No playfulness, no decorative gradients, no big shadows.
- **Type**: **IBM Plex Mono** for the cockpit — data, labels, numbers, ids,
  timestamps, status. **IBM Plex Sans** for prose. Both ship Cyrillic; the UI is
  **Russian** («Помощь», «Тикеты», «Инциденты»). Loaded via Google Fonts CDN in
  `tokens.css` (swap to self-hosted `@font-face` if needed). **Never**
  Inter / Roboto / Arial.
- **Color**: near-black surface ladder, low-chroma content, ONE accent (cyan-
  teal) + semantic status (ok/warn/danger/info/idle). Status renders as **small
  filled dots + mono labels**, never as big colored cards.
- **Density**: compact rows, **tabular numerals** (`font-variant-numeric:
  tabular-nums`) for all metrics, tight `--lh-tight` line-height, mono for
  ids/timestamps/counts.
- **Detail**: faint dotted/grid background texture at very low opacity; hairline
  1px borders (token); low radii (2–4px); flat (borders, not shadows).
  Micro-interactions are CSS-only (row highlight, button press, staggered
  fade-in on HTMX swap).
- **The memorable thing**: the monospace **agent status bar / header** with the
  signal-accent tick (`▮`) and faint grid — it should feel like a real control
  room, not a SaaS template.

### Anti-patterns — DO NOT produce
- Inter / Roboto / Arial fonts; purple gradients; gradients as decoration.
- 3-column identical card grids; colored card borders as the primary signal;
  big colored status cards.
- Center-aligned body text; arbitrary spacing (use `--sp-*`); bubbly oversized
  radii; drop-shadows everywhere.
- **Hex literals in feature CSS/templates** — tokens only.

---

## 2. Token table (`tokens.css`)

Intent-named only. No `--blue-500`, no raw hex outside `tokens.css`.

### Surface ladder (near-black, layered — isolate stacked sections)
| Token | Use |
|---|---|
| `--bg` | app canvas, deepest |
| `--surface` | panels, sidenav, header bar |
| `--surface-raised` | tiles / cards on a panel |
| `--surface-overlay` | hover rows, popovers, active inputs |

### Content (low-chroma; hierarchy primary → muted → faint)
| Token | Use |
|---|---|
| `--content` | primary text |
| `--content-muted` | labels, secondary text, table headers |
| `--content-faint` | timestamps, hints, disabled, separators |

### Border (hairline; structure via borders, not shadows)
| Token | Use |
|---|---|
| `--border-hairline` | default 1px divider / panel edge |
| `--border-strong` | emphasized edge (focused input, active) |

### Accent (the ONE signal color)
| Token | Use |
|---|---|
| `--accent` | primary actions, active nav, links |
| `--accent-quiet` | low-alpha tint bg (active pill, selected row) |
| `--accent-contrast` | text/icon ON a filled accent surface |

### Status (semantic; each has a `-bg` quiet variant)
| Solid | Quiet bg | Meaning |
|---|---|---|
| `--ok` | `--ok-bg` | healthy / calm green-teal |
| `--warn` | `--warn-bg` | degraded / amber |
| `--danger` | `--danger-bg` | down / error / red |
| `--info` | `--info-bg` | informational / cyan |
| `--idle` | `--idle-bg` | neutral / unknown / batch-no-port |

### Radii · space · type · motion · texture
- **Radii**: `--r-sm` 2px, `--r-md` 4px.
- **Space** (4px base): `--sp-1`=4 … `--sp-8`=64. No arbitrary spacing.
- **Type**: `--font-mono` (IBM Plex Mono), `--font-sans` (IBM Plex Sans);
  sizes `--fs-xs` 11 / `--fs-sm` 12 / `--fs-md` 13 / `--fs-lg` 15 /
  `--fs-xl` 18 / `--fs-2xl` 24; line-heights `--lh-tight` 1.25 / `--lh-base` 1.5;
  weights `--fw-regular/medium/semibold/bold`; `--num-tabular` = tabular-nums.
- **Elevation**: `--shadow-none-flat` (default, flat); `--shadow-overlay` for
  popovers/toasts ONLY.
- **Motion**: `--ease`, `--dur` 120ms, `--dur-slow` 240ms.
- **Texture**: `--grid-line` (very low alpha), `--grid-size` 24px — composed into
  `body` background as a faint grid.

### Legacy aliases — REMOVED
The staged-migration bridge (`--panel`/`--line`/`--fg`/`--muted`/`--green`/
`--amber`/`--red`/`--grey`) has been deleted from `tokens.css` now that every
screen consumes the intent-named tokens directly. Do not reintroduce them.

---

## 3. Component patterns

Each pattern lists its tokens + the four states it must support:
**loading / error / empty / populated**. One primary action per view. Text
hierarchy: primary (`--content`) → muted (`--content-muted`) → faint
(`--content-faint`).

### Status dot — `.dot.green|amber|red|grey`
8px filled circle + mono label. Maps to `--ok / --warn / --danger / --idle`,
faint `box-shadow` from the matching `-bg`. **The status primitive** — use it
instead of coloring whole cards. *States:* empty → grey dot "no data"; error →
red dot + message; populated → live color.

### Metric stat
Mono label (`--content-muted`, `--fs-sm`) above a big tabular number
(`--fs-2xl`, `--num-tabular`, `--content`). On `--surface-raised`, `--r-md`,
1px `--border-hairline`. *States:* loading → faint `—`; empty → `0` muted;
error → `--danger` short code; populated → number (+ optional delta in status
color).

### Data row / table — `.grid`
Compact rows, `--border-hairline` bottom dividers, header in `--content-muted`
`--fs-sm`. Numeric cells tabular. Hover → `--surface-overlay` (`.rowlink:hover`).
*States:* loading → skeleton/faint rows; empty → single muted "Нет записей" row;
error → `--danger` row; populated → rows.

### Tile — `.tile`
Card on `--surface-raised`, 1px `--border-hairline`, `--r-md`, `--sp-3` padding.
Status conveyed by the dot in `header`, NOT a thick colored border. Header mono
semibold; `.meta` column in `--content-muted`; `footer` for the one primary
action + links. *States:* all four; degraded → warn dot + `.warn` note.

### Badge — `.badge.info|low|warning|medium|critical|high`
Small mono pill, `--fs-xs`, `--r-sm`. Severity → status `-bg` fill + solid text
(quiet, not loud). Avoid as the sole signal where a dot reads better.

### Button — primary / ghost
- **Primary** (base `button`): filled `--accent`, text `--accent-contrast`,
  `--r-sm`, mono `--fw-medium`; hover brightens, active translateY(1px).
  **One primary per view.**
- **Ghost** (`.link` and a future `.btn-ghost`): transparent bg, `--accent`
  text or `--border-hairline` outline; for secondary/destructive-confirm.

### Panel
Section container on `--surface`, 1px `--border-hairline`, `--r-md`. Separate
stacked sections by surface-shift / border / whitespace (`--sp-4`+) — never two
same-surface blocks flush.

### Tab nav / sidenav — `.sidenav a`, `.pill`
Active = `--accent-quiet` bg + 2px `--accent` left border (sidenav) / accent
fill (`.pill.on`). Inactive = `--content-muted`, hover → `--surface-overlay`.

### Toast / action result — `#action-result`
Transient confirmation on `--surface-overlay` with `--shadow-overlay`, status-
colored left edge, auto fade via `--dur-slow`. *States:* success `--ok`,
error `--danger`.

### Control-room header bar — `.topbar`
Mono, `--surface`, hairline bottom border. Brand with accent `▮` tick; env +
principal in `--content-muted`/`--content-faint`. The signature element.

---

## 4. Enforcement — hex gate

No hex literals in feature CSS/templates (only `tokens.css` may define hex):

```sh
grep -rnE '#[0-9a-fA-F]{3,8}\b' src/dashboard/static/dash.css \
     src/dashboard/templates \
  | grep -v 'tokens.css'
# Expected: empty. Any hit = migrate it to a var(--token).
```

> Status: **clean.** The per-screen redesign migrated every feature rule onto
> semantic tokens, cleared the two pre-redesign hex residuals, and removed the
> legacy aliases from `tokens.css` (no feature CSS/template references them).
