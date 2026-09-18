# Frontend

Astro shell + a single React island. No client routing — screen switching is
component state. Every value on screen comes from the backend.

## Component map

```
index.astro → Base.astro (fonts, global CSS, keyframes, hover classes)
└── App.tsx                  shell: sidebar nav, header (in-flight chip,
    │                        hot-swap dropdown), screen switching; polls
    │                        endpoints (5s), live stats (2s), proxy info (30s)
    │                        and passes them down
    ├── screens/Dashboard.tsx   range + detail + layout controls, KPI row,
    │                           chart grid, by-endpoint breakdown
    ├── screens/Requests.tsx    filter chips, search, expandable request table
    │                           (polls /admin/stats/recent at 1.5s)
    ├── screens/Endpoints.tsx   endpoint cards, test, set-active, inline
    │                           terminal for interactive tunnel connect,
    │                           saved tunnel routes, ~/.ssh/config host picker;
    │                           "managed · <owner>" badge + edit notice for
    │                           registered endpoints (links meta.manager_url)
    ├── screens/EndpointForm.tsx  the shared add/edit form: name, type,
    │                           alias, url, key, model allowlist; derives
    │                           `protocol` from the type select
    ├── screens/SshTunnel.tsx   structured tunnel form, generated command,
    │                           PTY session panel, test log, active tunnels
    ├── screens/ProxyInfo.tsx   base URL, curl/python/node snippets,
    │                           proxy stat cards (no key: there isn't one)
    ├── screens/Settings.tsx    toggles, proxy port, retention slider
    └── EChart.tsx              generic ECharts host (init once, setOption,
                                ResizeObserver, dispose on unmount)
```

## Ownership

| Concern | Owner |
| --- | --- |
| Visual tokens & shared style helpers (`dot`, `badge`, `track`, `knob`, `segStyle`, `input`, `card`, `sectionLabel`) | `src/lib/styles.ts` |
| Chart option builders (`volumeTokensOption`, `concurrencyOption`, `hourOfDayOption`, `dailyOption`) | `src/lib/chartOptions.ts` |
| Slim ECharts registration (canvas, bar/line, grid/tooltip/legend/dataZoom/markLine) | `src/lib/echarts.ts` |
| Typed API client (all `/admin/*` calls) | `src/lib/api.ts` |
| API contract types (mirror of the backend schemas) | `src/lib/types.ts` |
| Polling | `src/hooks/usePoll.ts` (interval + visibility-aware) |
| Number/time formatting (`fmt`, `fmtBucket`, `fmtDay`, `fmtClock`, `fmtDuration`, `fmtUptime`) | `src/lib/format.ts` |
| Frontend event logging (batched POST to `/admin/logs/frontend`) | `src/lib/logger.ts` |
| Fonts, global CSS, keyframes, `.hv-*` hover classes | `src/layouts/Base.astro` |

## Design tokens

Defined once in `src/lib/styles.ts`. `ACCENT` is a module constant — there is
no runtime theme or accent picker.

| Token | Value | Use |
| --- | --- | --- |
| `bg` | `#0B0E14` | page background |
| `bgSidebar` | `#0D1119` | sidebar, header, inputs, expanded rows |
| `bgCard` | `#11151D` | cards, chart panels |
| `bgInset` | `#131926` | segmented controls, header chips |
| `bgHover` | `#161C28` | nav hover/active, chart split lines |
| `bgRow` | `#1A2130` | table row emphasis |
| `border` | `#1D2430` | default borders, chart axis lines |
| `borderStrong` | `#2A3547` | input borders, dropdown, toggle track |
| `borderHover` | `#3A4A63` | hover borders, breakdown bars |
| `text` | `#E6EAF2` | primary text |
| `textMut` | `#8A94A6` | secondary text |
| `textDim` | `#525C6E` | tertiary text |
| `axis` | `#68738A` | chart axis labels |
| `ACCENT` | `#FFB224` | active states, tokens-out series |
| `cyan` | `#58C4DD` | tokens-in series, streaming states |
| `green` | `#3FB950` | success / online / requests bars |
| `red` | `#F85149` | errors / offline |
| `purple` | `#BC8CFF` | remote badge |

`statusColor()` maps health strings onto these: green for
`healthy`/`online`/`up`, accent for `degraded`/`unknown`/`starting`, red
otherwise.

## Typography

- UI: `'IBM Plex Sans', sans-serif`, base 13px (`SANS`).
- Numerals, code, labels, chart text: `'JetBrains Mono', monospace` (`MONO`).
- Section headers (`sectionLabel`): 600 11px, letter-spacing .07em, uppercase,
  `textMut`.

Both families are loaded from Google Fonts in `Base.astro`.

## Layout

- Fixed 200px sidebar, 48px header, scrollable content.
- Dashboard chart grid: 12-column CSS grid with two layout presets, each
  panel declared as `[span, height, order?]`:

| Panel | A — Overview | C — Dense |
| --- | --- | --- |
| `combo` (volume + tokens) | span 8, h 300 | span 6, h 240 |
| `conc` (live concurrency) | span 4, h 300 | span 6, h 240 |
| `hod` (tokens by hour-of-day) | span 4, h 250 | span 4, h 220 |
| `daily` (tokens per day) | span 4, h 250 | span 4, h 220 |
| `break` (by-endpoint breakdown) | span 4, h 250 | span 4, h 220 |

There is no layout B — the former "Timeline" preset was removed.

- Ranges: `1H`, `24H`, `7D`, `30D`, `1Y`, `Custom`, each with a
  `Summary`/`Detailed` toggle. `Dashboard.tsx` carries a `GRAN` table of
  human-readable bucket captions that **mirrors the backend's `_TS_BUCKETS`** —
  change one and change the other.
- Cards: radius 8, 1px `border`.

## Motion

- `livepulse` 2s opacity pulse for live dots.
- `rowstream` 1.6s background pulse for streaming request rows.
- `spin` for busy indicators.
- Toggle knobs animate `left .2s`; the hot-swap control shows a "draining"
  state before switching.
- ECharts default animations for windowed charts; animation is disabled on
  the live concurrency chart.

## Charts

- Slim `echarts/core` build with the canvas renderer and only the components
  in use — keeps the bundle small.
- Axis style: line `border`, labels `axis` 10px JetBrains Mono, split lines
  `bgHover`. Tooltip: bg `#161B26`, border `borderStrong`, text `text` 11px
  mono.
- Series colours: requests `green`, errors `red` (stacked on requests),
  tokens in `cyan`, tokens out `ACCENT`. Legend swatches are set explicitly so
  labels match the plotted lines.
- The combined panel puts request volume on the right-hand axis as bars and
  tokens in/out on the left-hand axis as smooth area lines, sharing one
  category axis.

## Conventions

- Inline React style objects; shared values come from `styles.ts` rather than
  being repeated. No Tailwind or component library.
- Screens are separate modules; ~850 lines is the ceiling. `Endpoints.tsx`
  hit it and shed its add/edit forms into `EndpointForm.tsx` — new endpoint
  fields go there once, not into two near-duplicate JSX blocks.
- UI actions call `log.*(...)` so they reach the backend log stream.
- `usePoll` pauses while the tab is hidden — don't hand-roll `setInterval`.
