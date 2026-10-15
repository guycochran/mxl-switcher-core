const $ = (id) => document.getElementById(id)
const history = []
const sourceMotion = new Map()
let prior = null
let baseline = null
let lastSampleAt = 0
let consecutiveRouteStalls = 0

const faultCounters = [
  ['invalid', 'Invalid grains'],
  ['too_early', 'Too early'],
  ['too_late', 'Too late'],
  ['timeouts', 'Read timeouts'],
  ['reader_reopens', 'Reader reopens'],
  ['sync_timeouts', 'Sync timeouts'],
  ['sync_too_early', 'Sync too early'],
  ['sync_too_late', 'Sync too late'],
  ['no_common_window', 'No common window'],
  ['selected_input_regressions', 'Input regressions'],
]
const measurementCounters = [
  ['held', 'Held output'],
  ['observed_common_window_empty', 'Observed gaps'],
  ['input_timeline_resets', 'Timeline resets'],
  ['recovery_index_clamps', 'Recovery clamps'],
  ['copy_overwrite_suspects', 'Copy-lap suspects'],
  ['selected_input_repeats', 'Repeated input'],
  ['selected_input_skip_events', 'Skip events'],
]

function fmt(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : '—'
}

function setCard(id, state, value, detail) {
  const card = $(id)
  card.className = `confidence ${state}`
  $(id.replace('Card', 'Value')).textContent = value
  $(id.replace('Card', 'Detail')).textContent = detail
}

function sourceName(source, sources) {
  const found = sources.find((candidate) => candidate.flow_id === source.flow_id || candidate.flow === source.flow_id)
  return found?.label || found?.id || 'Unknown input'
}

function renderChart() {
  const canvas = $('routeChart')
  const context = canvas.getContext('2d')
  const width = canvas.width
  const height = canvas.height
  context.clearRect(0, 0, width, height)
  context.strokeStyle = '#1f2b37'
  context.lineWidth = 1
  for (let line = 1; line < 4; line++) {
    const y = (height / 4) * line
    context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke()
  }
  if (history.length < 2) return
  const max = Math.max(1, ...history.map((item) => item.rate))
  context.strokeStyle = history.at(-1).rate > 0 ? '#37d579' : '#f34a50'
  context.lineWidth = 4
  context.lineJoin = 'round'
  context.beginPath()
  history.forEach((item, index) => {
    const x = (index / Math.max(1, history.length - 1)) * width
    const y = height - 8 - (item.rate / max) * (height - 22)
    if (index === 0) context.moveTo(x, y); else context.lineTo(x, y)
  })
  context.stroke()
  $('chartRange').textContent = `PEAK ${max.toFixed(1)} GRAINS/S`
}

function renderInputs(data) {
  const grid = $('inputGrid')
  grid.replaceChildren()
  let moving = 0
  const telemetry = new Map((data.health.sources || []).map((source) => [source.flow_id, source]))
  const selectedIds = new Set([data.core.program, data.core.preview])
  const activeFlows = new Set(data.sources.filter((source) => selectedIds.has(source.id)).map((source) => source.flow_id))
  const stalledSources = []
  for (const configured of data.sources) {
    const source = telemetry.get(configured.flow_id) || { flow_id: configured.flow_id, latest_head_index: 0 }
    const active = activeFlows.has(configured.flow_id)
    const previous = sourceMotion.get(source.flow_id)
    const advanced = active && previous && Number(source.latest_head_index) > Number(previous.head)
    const stalled = active && previous ? (advanced ? 0 : previous.stalled + 1) : 0
    sourceMotion.set(source.flow_id, { head: source.latest_head_index, stalled })
    if (advanced) moving += 1
    if (active && stalled >= 3) stalledSources.push(source)
    const card = document.createElement('div')
    card.className = `input-card ${advanced ? 'moving' : active && stalled >= 3 ? 'stalled' : ''}`
    const lamp = document.createElement('i')
    const label = document.createElement('div')
    const strong = document.createElement('strong')
    strong.textContent = configured.label || configured.id
    const detail = document.createElement('small')
    detail.textContent = !active ? 'STANDBY · NOT CURRENTLY READ' : advanced ? 'ADVANCING' : stalled >= 3 ? 'STALLED' : 'MEASURING'
    label.append(strong, detail)
    const head = document.createElement('code')
    head.textContent = fmt(source.latest_head_index)
    card.append(lamp, label, head)
    grid.append(card)
  }
  const total = activeFlows.size
  if (!data.sources.length) setCard('inputsCard', 'bad', 'NO INPUTS', 'Core reported no configured sources')
  else if (stalledSources.length) setCard('inputsCard', 'bad', `${total - stalledSources.length}/${total} ACTIVE`, `${stalledSources.length} selected path${stalledSources.length === 1 ? '' : 's'} stalled`)
  else if (!prior) setCard('inputsCard', 'neutral', `${total} SELECTED`, `${data.sources.length - total} sources on standby`)
  else setCard('inputsCard', 'ok', `${moving}/${total} ACTIVE`, 'All selected source rings advancing')
  return stalledSources
}

function renderNmos(nmos) {
  const registered = nmos.state === 'online' && nmos.registry === 'registered'
  if (registered) setCard('nmosCard', 'ok', 'REGISTERED', `${nmos.senders || 0} senders · ${nmos.receivers || 0} receivers`)
  else if (nmos.state === 'disabled') setCard('nmosCard', 'neutral', 'NOT CONFIGURED', 'Optional Node telemetry disabled')
  else if (nmos.state === 'online' && nmos.registry === 'disabled') setCard('nmosCard', 'warn', 'NODE ONLINE', 'Registry telemetry not configured')
  else setCard('nmosCard', 'bad', 'ATTENTION', `Node ${nmos.state} · registry ${nmos.registry}`)
  $('nodeLabel').textContent = nmos.label || '—'
  for (const [id, key] of [['devices','devices'],['nmosSources','sources'],['flows','flows'],['senders','senders'],['receivers','receivers']]) $(id).textContent = fmt(nmos[key])
  $('registry').textContent = (nmos.registry || '—').toUpperCase()
  const clock = (nmos.clock || 'unknown').toUpperCase()
  setCard('clockCard', nmos.clock === 'ptp' ? 'ok' : nmos.state === 'disabled' ? 'neutral' : 'warn', clock, nmos.clock === 'ptp' ? 'Precision Time Protocol' : nmos.clock === 'internal' ? 'Local reference clock' : 'Clock telemetry unavailable')
}

function renderAudio(audio, mixer) {
  if (audio.state === 'ok') {
    const meters = Object.values(audio.meters || {})
    const active = meters.filter((meter) => meter.state !== 'silent').length
    setCard('audioCard', 'ok', `${active}/${meters.length} PRESENT`, mixer.state === 'ok' ? `Program mixer ${mixer.mode || 'online'}` : 'Input meters online')
  } else if (audio.state === 'disabled') setCard('audioCard', 'neutral', 'METER OFF', 'Optional confidence meter disabled')
  else setCard('audioCard', 'bad', 'METER FAIL', 'Configured meter is unavailable')
}

function renderCounters(health) {
  if (!baseline) baseline = Object.fromEntries([...faultCounters, ...measurementCounters].map(([key]) => [key, Number(health[key] || 0)]))
  const container = $('counters')
  container.replaceChildren()
  for (const [key, label, measurement = false] of [...faultCounters.map((item) => [...item, false]), ...measurementCounters.map((item) => [...item, true])]) {
    const total = Number(health[key] || 0)
    const delta = total - Number(baseline[key] || 0)
    const card = document.createElement('div')
    card.className = `counter ${delta > 0 ? (measurement ? 'measurement' : 'fault') : ''}`
    const name = document.createElement('span'); name.textContent = label
    const value = document.createElement('strong'); value.textContent = fmt(total)
    const change = document.createElement('small'); change.textContent = delta ? `+${fmt(delta)} SINCE OPEN` : 'Δ 0 SINCE OPEN'
    card.append(name, value, change); container.append(card)
  }
}

function renderAlerts(data, stalledSources, routeStalled) {
  const alerts = []
  const health = data.health
  if (Number(health.sustained_unavailable_active) > 0) alerts.push(['critical', `${health.sustained_unavailable_active} active input alarm${health.sustained_unavailable_active === 1 ? '' : 's'}`, 'Core is holding output while one or more selected inputs are unavailable.'])
  if (routeStalled) alerts.push(['critical', 'Routing cadence stalled', 'The routed grain counter has not advanced for three consecutive samples.'])
  for (const source of stalledSources) alerts.push(['critical', `${sourceName(source, data.sources)} is stalled`, 'Its MXL ring head index is not advancing.'])
  for (const [key, label] of faultCounters) {
    const delta = Number(health[key] || 0) - Number(baseline?.[key] || 0)
    if (delta > 0) alerts.push(['critical', `${label} increased`, `+${fmt(delta)} since this dashboard was opened.`])
  }
  if (data.nmos.state !== 'disabled' && (data.nmos.state !== 'online' || !['registered','disabled'].includes(data.nmos.registry))) alerts.push(['critical', 'NMOS control-plane fault', `Node ${data.nmos.state}; Registry ${data.nmos.registry}.`])
  if (data.audio.state === 'unavailable') alerts.push(['critical', 'Audio confidence unavailable', 'The configured meter service did not respond.'])
  const box = $('alerts'); box.replaceChildren()
  if (!alerts.length) alerts.push(['clear', 'No active alarms', 'The current sample is healthy; lifetime measurements remain visible below.'])
  for (const [kind, title, detail] of alerts) {
    const item = document.createElement('div'); item.className = `alert ${kind}`
    const strong = document.createElement('strong'); strong.textContent = title
    const span = document.createElement('span'); span.textContent = detail
    item.append(strong, span); box.append(item)
  }
  $('alarmCount').textContent = alerts[0][0] === 'clear' ? 'CLEAR' : `${alerts.length} ACTIVE`
  return alerts[0][0] !== 'clear'
}

function render(data) {
  const health = data.health
  const core = data.core
  const elapsed = prior ? Math.max(.05, Number(data.sample_time) - Number(prior.sample_time)) : 0
  const routedDelta = prior ? Number(health.routed) - Number(prior.health.routed) : 0
  const routeRate = elapsed ? routedDelta / elapsed : 0
  consecutiveRouteStalls = prior && routedDelta <= 0 ? consecutiveRouteStalls + 1 : 0
  const routeStalled = consecutiveRouteStalls >= 3
  if (prior) { history.push({ rate: Math.max(0, routeRate) }); if (history.length > 60) history.shift() }
  $('routeRate').textContent = `${routeRate.toFixed(1)} grains/s`
  if (!prior) setCard('routeCard', 'neutral', 'MEASURING', 'Waiting for the next sample')
  else if (routeStalled) setCard('routeCard', 'bad', 'STALLED', `${consecutiveRouteStalls} consecutive samples`)
  else if (routedDelta > 0) setCard('routeCard', 'ok', 'MOVING', `+${fmt(routedDelta)} grains in ${elapsed.toFixed(1)}s`)
  else setCard('routeCard', 'warn', 'WAITING', 'No movement in this sample')
  setCard('coreCard', health.status === 'running' ? 'ok' : 'bad', (health.status || 'unknown').toUpperCase(), `${fmt(health.sustained_unavailable_active)} active input alarms`)

  const stalledSources = renderInputs(data)
  renderNmos(data.nmos)
  renderAudio(data.audio, data.audio_mixer)
  renderCounters(health)
  const hasAlarm = renderAlerts(data, stalledSources, routeStalled)

  const sourceById = (id) => data.sources.find((source) => source.id === id)
  $('programName').textContent = sourceById(core.program)?.label || core.program || '—'
  $('previewName').textContent = sourceById(core.preview)?.label || core.preview || '—'
  $('programFlow').textContent = core.program_flow || '—'
  $('previewFlow').textContent = core.preview_flow || '—'
  $('revision').textContent = `REV ${fmt(core.revision)}`
  $('windowNow').textContent = fmt(health.last_common_window_width)
  $('windowRange').textContent = `${fmt(health.min_common_window_width)}–${fmt(health.max_common_window_width)} observed`
  $('skewNow').textContent = fmt(health.last_source_skew_frames)
  $('skewRange').textContent = `${fmt(health.min_source_skew_frames)}–${fmt(health.max_source_skew_frames)} observed`
  $('outputIndex').textContent = fmt(core.last_output_index)

  const overall = $('overall')
  const degraded = health.status !== 'running' || hasAlarm
  overall.className = `overall ${degraded ? 'bad' : 'ok'}`
  $('overallTitle').textContent = degraded ? 'ACTION REQUIRED' : 'SYSTEM HEALTHY'
  $('overallDetail').textContent = degraded ? 'One or more live confidence checks require engineering attention.' : 'Core, media motion, and configured control-plane checks are healthy.'
  lastSampleAt = Date.now()
  $('lastUpdate').textContent = `LAST SAMPLE ${new Date(lastSampleAt).toLocaleTimeString()}`
  renderChart()
  prior = data
}

async function refresh() {
  try {
    const response = await fetch('/api/skin/health', { cache: 'no-store' })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || response.statusText)
    render(data)
  } catch (error) {
    const overall = $('overall')
    overall.className = 'overall bad'
    $('overallTitle').textContent = 'CORE OFFLINE'
    $('overallDetail').textContent = error.message
    setCard('coreCard', 'bad', 'OFFLINE', 'Health endpoint did not respond')
  }
}

setInterval(() => {
  $('sampleAge').textContent = lastSampleAt ? `SAMPLE AGE ${((Date.now() - lastSampleAt) / 1000).toFixed(1)}s` : 'WAITING FOR CORE'
}, 250)
refresh()
setInterval(refresh, 1000)
