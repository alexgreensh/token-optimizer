import type { Elements } from 'claude-code'
import { dashboardStatus } from './actions.ts'
import { LIGHT, type Gaze, type Palette } from './clawd.ts'
import type { Snapshot } from './contracts.ts'
import { savingsRetryNote, type FormatOptions } from './format.ts'
import { ICON_ALT, iconSvg, type IconName } from './icons.ts'
import { QUALITY_FLOOR, sentence, type ActionId, type Run } from './ladder.ts'
import { cards, marks, row, savingsPill, type Card, type Mark, type MarkTone, type SavingsPill } from './marks.ts'
import { slimSide } from './size.ts'

export type Desktop = Elements['desktop']
type Tones = { good: string; caution: string; bad: string; cold: string; ink: string; track: string; card: string; line: string; goodSoft: string }

/** The design page's tone colours (light; each picture follows dark mode itself, see DARK_STYLE). */
export function tonesFor(p: Palette): Tones {
  return { good: '#2f9e55', goodSoft: '#e2f3e9', caution: '#c98a1b', bad: p.bad, cold: p.cold, ink: p.ink, track: '#d9d6cd', card: p.card, line: '#e2dfd6' }
}

// ---- drawing ----

const ring = (p: number, color: string, track: string, cold: boolean): string => {
  const c = 2 * Math.PI * 7
  const bg = cold ? `stroke="${color}" stroke-dasharray="2.2 2.2"` : `class="t" stroke="${track}"`
  return (
    `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" ${bg}/>` +
    (p > 0 ? `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" stroke="${color}" stroke-linecap="round" stroke-dasharray="${((c * p) / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 10 10)"/>` : '')
  )
}

/**
 * The desktop gives the band no light/dark signal (the config's theme is the
 * terminal's), so the marks and icons follow the app's own appearance through their
 * colour-scheme query; the drawn colours are the light ones, the fallback.
 * Classes: k = ink stroke, kf = ink fill, t = track stroke, tf = track fill.
 */
const DARK_STYLE = '<style>@media (prefers-color-scheme: dark){.k{stroke:#f3f1ea}.kf{fill:#f3f1ea}.t{stroke:#4b4a46}.tf{fill:#4b4a46}}</style>'

function themed(svg: string, rootClass?: string): string {
  const open = svg.indexOf('>') + 1
  const head = rootClass ? svg.slice(0, open - 1).replace('<svg ', `<svg class="${rootClass}" `) + '>' : svg.slice(0, open)
  return head + DARK_STYLE + svg.slice(open)
}

const esc = (v: string): string => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** A mark's ring or grade badge, as the artifact draws it: the colour lives here, the text stays ink. */
function markSvg(mark: Mark, t: Tones): string {
  const color = toneColor(mark.tone, t)
  const right =
    mark.badge !== undefined
      ? `<rect x="1" y="1" width="18" height="18" rx="5"${mark.tone === 'none' ? ' class="tf"' : ''} fill="${mark.tone === 'none' ? t.track : color}"/>` +
        `<text x="10" y="14" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11.5" font-weight="700" fill="${t.card}">${esc(mark.badge)}</text>`
      : ring(mark.ringPercent ?? 0, color, t.track, mark.tone === 'cold')
  return themed(`<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" role="img" aria-label="${esc(mark.alt)}"><title>${esc(mark.alt)}</title>${right}</svg>`)
}

function toneColor(tone: MarkTone, t: Tones): string {
  return tone === 'none' ? t.track : t[tone]
}

function runsOf(D: Desktop, runs: Run[], t: Tones, ink?: string) {
  const { Text } = D
  return runs.map(r => (r.lose ? <Text bold color={t.bad}>{r.text}</Text> : r.strong ? <Text bold color={ink}>{r.text}</Text> : r.text))
}

function icon(D: Desktop, name: IconName, color: string, alt?: string) {
  const { Svg } = D
  const label = alt ?? ICON_ALT[name]
  const svg = iconSvg(name, color, { alt: label })
  return <Svg source={color === LIGHT.ink ? themed(svg, 'k') : svg} alt={label} width={16} height={16} />
}

export type Model = {
  snap: Snapshot
  tones: Tones
  sheetOpen: boolean
  /** The marks' hover cards; off, the marks are plain (the engine has no hover delay, so this is the only way to quiet them). */
  hoverCards?: boolean
  savingsReason: string | null
  keepwarmNote: string | null
  savingsComputedAt: number | null
  savingsRefreshFailed: boolean
  /** Keep warm can run now (the one guard): only then does the row offer it. */
  canWarm: boolean
  /** Clawd's pictures, bottom first: the previous pose stays beneath a new one while it fades in. */
  clawd: ClawdLayer[]
  /** While watching: one picture per look, each shown while the pointer is over its part of the band. */
  gazes: (ClawdLayer & { gaze: Gaze })[]
}

export type ClawdLayer = { key: string; source: string; alt: string }
export type Handlers = { act: (id: ActionId) => void; dashboard: () => void; details: () => void; size: () => void }

/** The green savings chip right after the sentence: the shared headline's own words, a press opens the dashboard like `Dashboard ↗` does. */
function savingsChip(D: Desktop, pill: SavingsPill, t: Tones, on: Handlers) {
  const { Box, Button } = D
  return (
    <Box flexDirection="row" alignItems="center" columnGap={1} flexShrink={0} paddingX={1} borderStyle="round" borderColor={t.good} backgroundColor={t.goodSoft}>
      {icon(D, 'saved', t.good)}
      <Button key="savings-pill" plain label={`${pill.text} ↗`} onPress={() => on.dashboard()} />
    </Box>
  )
}

/** The five marks with their hover cards: one line, never wrapped, in both sizes. `Dashboard ↗` caps the row; `status` is the slim band's dashboard progress, which has no sentence line to carry it. */
function markRow(D: Desktop, markList: Mark[], cardList: Card[], t: Tones, on: Handlers, status: string | null = null) {
  const { Box, Text, Svg, Button } = D
  return (
    <Box flexDirection="row" flexWrap="nowrap" columnGap={3} flexShrink={1}>
      {markList.map((mark, i) => (
        <Box key={`mark-${mark.id}`} position="relative" flexDirection="row" alignItems="center" columnGap={1} flexShrink={1}>
          <Svg source={markSvg(mark, t)} alt={mark.alt} width={20} height={20} />
          <Box flexShrink={0}>
            <Text bold>{mark.value}</Text>
          </Box>
          {/* Always labelled: without the word, nobody knows which number is which. In a window too narrow for every label the word is cut with an ellipsis on one line; it never wraps under its value. */}
          <Box flexShrink={1} minWidth={3}>
            <Text wrap="truncate-end">{mark.label}</Text>
          </Box>
          {(() => {
            // Matched by id, not position: a missing limit never shifts a card under the wrong mark.
            const card = cardList.find(c => c.id === mark.id)
            return card ? cardBox(D, card, i, t, on) : ''
          })()}
        </Box>
      ))}
      <Box flexDirection="row" alignItems="center" columnGap={1} flexShrink={0}>
        {status !== null ? (
          <Box key="dashboard-status">
            <Text>{status}</Text>
          </Box>
        ) : (
          ''
        )}
        <Button key="dashboard" plain label="Dashboard ↗" onPress={() => on.dashboard()} />
      </Box>
    </Box>
  )
}

function cardBox(D: Desktop, card: Card, index: number, t: Tones, on: Handlers) {
  const { Box, Text, Button } = D
  const side = index < 2 ? { left: 0 } : { right: 0 }
  return (
    <Box
      position="absolute"
      bottom={1}
      {...side}
      display="none"
      hover={{ display: 'flex' }}
      flexDirection="column"
      width={44}
      paddingX={1}
      borderStyle="round"
      borderColor={t.line}
      backgroundColor={t.card}
    >
      {/* The card is always light (it keeps LIGHT's white), so its words carry the light palette's ink: left to the app's default they turn white on dark themes, white on white. */}
      <Text bold color={t.ink}>{card.title}</Text>
      <Text wrap="wrap" color={t.ink}>
        <Text bold color={t.ink}>What it means: </Text>
        {runsOf(D, card.means, t, t.ink)}
      </Text>
      <Text wrap="wrap" color={t.ink}>
        <Text bold color={t.ink}>What to do: </Text>
        {runsOf(D, card.todo, t, t.ink)}
      </Text>
      {card.actions.length > 0 ? (
        <Box flexDirection="row" columnGap={1} marginTop={1}>
          {card.actions.map(a => (
            <Button key={`card-${card.id}-${a.id}`} label={a.label} onPress={() => on.act(a.id)} />
          ))}
        </Box>
      ) : (
        ''
      )}
    </Box>
  )
}

/** The unfolded row, shared by both sizes: the facts and any card action the moment calls for. Savings live on the title-row chip; behind the arrow only an unavailable reason remains. */
function detailRow(D: Desktop, m: Model, on: Handlers, timeOpts: FormatOptions) {
  const { Box, Text, Button } = D
  const { snap, tones: t } = m
  const say = sentence(snap, timeOpts)
  const cardList = cards(snap, timeOpts)
  const detail = row(snap)
  // A card action joins the row only when the moment calls for it (quality sagging, the cache
  // about to drop) and the sentence's own button is not already offering it.
  const q = snap.quality
  const sagging = q !== null && (q.score < QUALITY_FLOOR || q.recoveryStatus === 'needs_attention')
  const rowActions = [
    ...(sagging ? cardList.find(c => c.id === 'quality')?.actions ?? [] : []),
    ...(m.canWarm && snap.cache.state === 'warning' ? [{ id: 'warm' as const, label: 'Keep warm' }] : []),
  ].filter(a => a.id !== say.action?.id && !(a.id === 'clean' && say.action?.id === 'clean-first'))

  const savingsNote =
    detail.savings.state === 'unavailable' || (snap.savingsLoading && !snap.savings) ? (
      <Box flexDirection="row" alignItems="center" columnGap={1}>
        {icon(D, 'saved', t.ink)}
        <Text>{m.savingsReason ?? (snap.savingsLoading ? 'Still counting savings.' : detail.savings.reason) ?? 'Savings appear once Token Optimizer has measured some.'}</Text>
      </Box>
    ) : savingsRetryNote(m.savingsRefreshFailed, m.savingsComputedAt, snap.now) ? <Text>{savingsRetryNote(m.savingsRefreshFailed, m.savingsComputedAt, snap.now)}</Text> : ''

  return (
    <Box key="row" flexDirection="row" flexWrap="wrap" alignItems="center" justifyContent="space-between" columnGap={2} rowGap={1}>
      <Box flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={2} rowGap={1}>
        {detail.facts.map(f => (
          <Box flexDirection="row" alignItems="center" columnGap={1}>
            {icon(D, f.icon, t.ink)}
            <Text>{runsOf(D, f.runs, t)}</Text>
          </Box>
        ))}
        {rowActions.length > 0 ? (
          <Box flexDirection="row" alignItems="center" columnGap={1}>
            {rowActions.map(a => (
              <Button key={`row-${a.id}`} label={a.label} onPress={() => on.act(a.id)} />
            ))}
          </Box>
        ) : (
          ''
        )}
      </Box>
      <Box flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={2} rowGap={1}>{savingsNote}{m.keepwarmNote ? <Text>{m.keepwarmNote}</Text> : ''}</Box>
    </Box>
  )
}

export function drawBand(D: Desktop, m: Model, on: Handlers, timeOpts: FormatOptions) {
  const { Box, Text, Button, Svg } = D
  const { snap, tones: t } = m
  const say = sentence(snap, timeOpts)
  const markList = marks(snap, timeOpts)
  const cardList = m.hoverCards === false ? [] : cards(snap, timeOpts)
  const pill = savingsPill(snap, say, 'full')
  // The artifact's layout: Clawd and his arrow beside the sentence and the marks; the
  // unfolded row under all of it, from Clawd's left edge.
  return (
    // Keyed: the whole band is one hover zone (the desktop reveals within a keyed Box,
    // not across separate hover groups), so a pointer anywhere on it wakes Clawd to look.
    <Box key="band" flexDirection="column" paddingX={1} rowGap={1}>
      <Box flexDirection="row" alignItems="center" columnGap={2}>
        <Box flexDirection="row" alignItems="center" columnGap={1} flexShrink={0}>
          {/* Box sizes count text cells on desktop, so the bottom picture sizes the stack and the new one sits over it.
              Only a Button can be pressed, and its label is text, so the arrow beside him opens the row. */}
          {/* No hover on this Box: the desktop rebuilds a hover box's pictures on every redraw (Clawd blanked once a second). */}
          <Box position="relative">
            {m.clawd.map((c, i) => (
              <Box key={c.key} {...(i === 0 ? {} : { position: 'absolute' as const, top: 0, left: 0 })}>
                {/* Not isInteractive: the desktop reloads an interactive picture on every redraw (a blank frame); a plain one keeps its animation. */}
                <Svg source={c.source} alt={c.alt} width={72} height={57} />
              </Box>
            ))}
            {/* Hover can reveal but not move: his look toward the band is its own picture, drawn hidden over him. */}
            {m.gazes.map(g => (
              // No key of its own: a keyed Box drawn hidden is a hover zone nobody can point at.
              <Box position="absolute" top={0} left={0} display="none" hover={{ display: 'flex' }}>
                <Svg source={g.source} alt={g.alt} width={72} height={57} />
              </Box>
            ))}
          </Box>
          <Box flexDirection="row" columnGap={1}>
            {/* A native button, not a bare glyph: its frame says "press me". */}
            <Button
              key="details"
              label={m.sheetOpen ? '▴' : '▾'}
              onPress={() => on.details()}
            />
            {/* One line instead of the band: vertical space is at a premium. */}
            <Button key="size" label="–" onPress={() => on.size()} />
          </Box>
        </Box>
        <Box flexDirection="column" flexGrow={1} flexShrink={1} rowGap={1}>
          <Box flexDirection="row" alignItems="center" justifyContent="space-between" columnGap={2}>
            <Box flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={1} flexShrink={1}>
              <Text bold>Token Optimizer</Text>
              {icon(D, say.icon, toneColor(say.tone, t))}
              <Text wrap="wrap">{runsOf(D, say.runs, t)}</Text>
              {pill !== null ? savingsChip(D, pill, t, on) : ''}
            </Box>
            {say.action ? <Button key="action" variant="primary" label={say.action.label} onPress={() => on.act(say.action!.id)} /> : ''}
          </Box>
          {/* One line, never wrapped: a wrapped mark's card would open over the marks above it. */}
          {markRow(D, markList, cardList, t, on)}
        </Box>
      </Box>
      {m.sheetOpen ? detailRow(D, m, on, timeOpts) : ''}
    </Box>
  )
}

/** The slim band: one line, plus the unfolded row when open. Tiny Clawd, the arrow and + buttons, the five marks, and what the sentence earns on the right. */
export function drawSlim(D: Desktop, m: Model, on: Handlers, timeOpts: FormatOptions) {
  const { Box, Text, Button, Svg } = D
  const { snap, tones: t } = m
  const say = sentence(snap, timeOpts)
  const side = slimSide(say)
  const pill = side.kind === 'none' ? savingsPill(snap, say, 'slim') : null
  return (
    // Keyed like the full band: the same one hover zone, so pointing still wakes Clawd's look.
    <Box key="band" flexDirection="column" paddingX={1} rowGap={1}>
    <Box flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={2}>
      {/* Clawd, the buttons and the marks are one group that fills the line: in a narrow window the chip drops to a second line and the marks stay beside the buttons, cutting their labels short if they must (a marks row on its own line would put its hover cards over the buttons above). */}
      <Box flexDirection="row" alignItems="center" columnGap={2} flexGrow={1} flexShrink={1} minWidth={60}>
      {/* The same layered pictures as the full band (same poses, fades, gaze layers), just small. */}
      <Box position="relative" flexShrink={0}>
        {m.clawd.map((c, i) => (
          <Box key={c.key} {...(i === 0 ? {} : { position: 'absolute' as const, top: 0, left: 0 })}>
            <Svg source={c.source} alt={c.alt} width={24} height={19} />
          </Box>
        ))}
        {m.gazes.map(g => (
          <Box position="absolute" top={0} left={0} display="none" hover={{ display: 'flex' }}>
            <Svg source={g.source} alt={g.alt} width={24} height={19} />
          </Box>
        ))}
      </Box>
      <Box flexDirection="row" columnGap={1} flexShrink={0}>
        {/* The same arrow as the full band: the unfolded row is where the detail lives. */}
        <Button key="details" label={m.sheetOpen ? '▴' : '▾'} onPress={() => on.details()} />
        <Button key="size" label="+" onPress={() => on.size()} />
      </Box>
      <Box flexGrow={1} flexShrink={1}>
        {markRow(D, marks(snap, timeOpts), m.hoverCards === false ? [] : cards(snap, timeOpts), t, on, (snap.busy === 'warming' ? 'Keeping the cache warm.' : dashboardStatus(snap.busy, snap.note)))}
      </Box>
      </Box>
      {side.kind === 'action' ? (
        <Box flexDirection="row" alignItems="center" columnGap={1} flexShrink={0}>
          {icon(D, side.icon, toneColor(side.tone, t))}
          <Button key="action" variant="primary" label={side.action.label} onPress={() => on.act(side.action!.id)} />
        </Box>
      ) : side.kind === 'warn' ? (
        <Box flexDirection="row" alignItems="center" columnGap={1} flexShrink={0}>
          {icon(D, side.icon, toneColor(side.tone, t))}
          <Text>{runsOf(D, side.runs, t)}</Text>
        </Box>
      ) : pill !== null ? (
        savingsChip(D, pill, t, on)
      ) : (
        ''
      )}
    </Box>
    {m.sheetOpen ? detailRow(D, m, on, timeOpts) : ''}
    </Box>
  )
}
