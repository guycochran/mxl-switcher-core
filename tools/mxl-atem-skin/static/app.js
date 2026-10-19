const $ = (id) => document.getElementById(id)
let state = null
let sources = []
let layout = 4
let slots = [1, 2, 3, 4]
let busy = false
const panelOrderKey = 'mxl-panel-order'

const api = async (path, options = {}) => {
  const response = await fetch(path, { cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...options })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Error(data.error || response.statusText)
  return data
}

function notice(text, error = false) {
  $('notice').textContent = text
  $('notice').style.color = error ? '#ff8589' : ''
}

function bankOf(id) { return id?.startsWith('supersource-') ? id.slice(-1) : null }
function meNumber(bank) { return bank === 'a' ? 1 : 2 }

function sceneFor(id) {
  if (id === 'supersource-a') return state?.banks.a
  if (id === 'supersource-b') return state?.banks.b
  return null
}

function nameFor(id) {
  const bank = bankOf(id)
  if (bank) {
    const count = state?.banks?.[bank]?.count
    return `ME ${meNumber(bank)}${count ? ` · ${count}-UP` : ''}`
  }
  return sources.find((source) => source.id === id)?.label || id || '—'
}

function busLabel(source) {
  const zoomiso = source.id?.match(/^zoomiso-(\d+)$/)
  if (zoomiso) return zoomiso[1]
  if (source.id === 'playout-tc') return 'PLY'
  if (source.id === 'keyed-tc') return 'KEY'
  return source.label?.slice(0, 4).toUpperCase() || source.id?.slice(0, 4).toUpperCase() || '?'
}

function grid(element, scene) {
  element.replaceChildren()
  if (!scene?.ready) return
  element.style.gridTemplateColumns = `repeat(${scene.count <= 2 ? scene.count : scene.count <= 4 ? 2 : 4},1fr)`
  for (const source of scene.sources) {
    const tile = document.createElement('div')
    tile.className = 'tile'
    tile.textContent = source
    element.append(tile)
  }
}

function meterHeight(db) {
  if (!Number.isFinite(db)) return '1%'
  return `${Math.max(1, Math.min(100, ((db + 60) / 60) * 100))}%`
}

function renderMultiview(core) {
  const wall = $('multiviewGrid')
  wall.replaceChildren()
  // Tiles follow the Core catalog order, matching the multiview encoder's wall slots.
  for (const id of sources.slice(0, 8).map((source) => source.id)) {
    const meter = state?.audio?.meters?.[id]
    const button = document.createElement('button')
    button.type = 'button'
    button.title = `Preview ${nameFor(id)}`
    button.setAttribute('aria-label', button.title)
    if (core.program === id) button.classList.add('program-tally')
    if (core.preview === id) button.classList.add('preview-tally')
    button.disabled = busy
    button.onclick = () => command('preview', { source: id })

    const vu = document.createElement('div')
    vu.className = `vu-meter ${meter?.state || 'unavailable'}`
    vu.setAttribute('aria-label', meter ? `${meter.rms_dbfs?.join(', ')} dBFS` : 'Audio unavailable')
    for (let channel = 0; channel < 2; channel++) {
      const bar = document.createElement('span')
      bar.style.height = meterHeight(meter?.peak_dbfs?.[channel] ?? meter?.channels?.[channel]?.peak_dbfs)
      vu.append(bar)
    }
    button.append(vu)
    wall.append(button)
  }
}

async function audioCommand(body) {
  try {
    await api('/api/skin/audio', { method: 'POST', body: JSON.stringify(body) })
    await refresh()
  } catch (error) {
    notice(error.message, true)
  }
}

function renderAudioMixer() {
  const mixer = state?.audio_mixer
  const box = $('audioChannels')
  box.replaceChildren()
  const online = mixer?.state === 'ok'
  $('audioStatus').textContent = online ? `${mixer.mode.toUpperCase()} · ${nameFor(mixer.program)}` : 'MIXER OFFLINE'
  $('afv').className = online && mixer.mode === 'afv' ? 'selected' : ''
  $('manualAudio').className = online && mixer.mode === 'manual' ? 'selected' : ''
  $('afv').disabled = !online
  $('manualAudio').disabled = !online
  $('masterGain').disabled = !online
  if (online && document.activeElement !== $('masterGain')) $('masterGain').value = Math.round(mixer.master_gain * 100)
  for (let number = 1; number <= 8; number++) {
    const channel = mixer?.channels?.[String(number)] || {}
    const meter = state?.audio?.meters?.[`zoomiso-${number}`]
    const strip = document.createElement('div')
    strip.className = `audio-strip${channel.active ? ' active' : ''}`
    const vu = document.createElement('div')
    vu.className = 'audio-vu'
    for (let side = 0; side < 2; side++) {
      const rail = document.createElement('i')
      const bar = document.createElement('span')
      bar.style.height = meterHeight(meter?.peak_dbfs?.[side])
      rail.append(bar)
      vu.append(rail)
    }
    const fader = document.createElement('input')
    fader.type = 'range'; fader.min = 0; fader.max = 150
    fader.value = Math.round((channel.gain ?? 1) * 100)
    fader.disabled = !online
    fader.onchange = () => audioCommand({ channel: number, gain: Number(fader.value) / 100 })
    const on = document.createElement('button')
    on.textContent = channel.active ? 'ON' : (channel.on ? 'ON*' : 'OFF')
    on.className = channel.active ? 'on' : ''
    on.disabled = !online || mixer.mode === 'afv'
    on.onclick = () => audioCommand({ channel: number, on: !channel.on })
    const mute = document.createElement('button')
    mute.textContent = 'M'
    mute.className = channel.mute ? 'muted' : ''
    mute.disabled = !online
    mute.onclick = () => audioCommand({ channel: number, mute: !channel.mute })
    const label = document.createElement('strong')
    label.textContent = number
    strip.append(vu, fader, on, mute, label)
    box.append(strip)
  }
}

function offAirBank() {
  const programBank = bankOf(state?.core.program)
  if (programBank) return programBank === 'a' ? 'b' : 'a'
  const previewBank = bankOf(state?.core.preview)
  return previewBank === 'a' ? 'b' : 'a'
}

function audioIsFirst() {
  return $('panelStack').firstElementChild === $('audioPanel')
}

function setPanelOrder(audioFirst, persist = true) {
  const stack = $('panelStack')
  const first = audioFirst ? $('audioPanel') : $('supersourcePanel')
  const second = audioFirst ? $('supersourcePanel') : $('audioPanel')
  stack.append(first, second)
  $('moveAudio').textContent = audioFirst ? 'MOVE BELOW' : 'MOVE ABOVE'
  if (persist) {
    try { localStorage.setItem(panelOrderKey, audioFirst ? 'audio-first' : 'supersource-first') } catch (_) {}
  }
}

function swapPanels() {
  setPanelOrder(!audioIsFirst())
}

function initPanelOrder() {
  let audioFirst = true
  try { audioFirst = localStorage.getItem(panelOrderKey) !== 'supersource-first' } catch (_) {}
  setPanelOrder(audioFirst, false)
  $('moveAudio').onclick = swapPanels

  const panels = [$('audioPanel'), $('supersourcePanel')]
  for (const [handleId, panelId] of [['audioDragHandle', 'audioPanel'], ['supersourceDragHandle', 'supersourcePanel']]) {
    const handle = $(handleId)
    handle.ondragstart = (event) => {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', panelId)
      $(panelId).classList.add('dragging')
    }
    handle.ondragend = () => panels.forEach((panel) => panel.classList.remove('dragging', 'drop-target'))
  }
  for (const panel of panels) {
    panel.ondragover = (event) => {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      panel.classList.add('drop-target')
    }
    panel.ondragleave = () => panel.classList.remove('drop-target')
    panel.ondrop = (event) => {
      event.preventDefault()
      const sourceId = event.dataTransfer.getData('text/plain')
      panels.forEach((item) => item.classList.remove('dragging', 'drop-target'))
      if (sourceId && sourceId !== panel.id) swapPanels()
    }
  }
}

function render() {
  if (!state) return
  const core = state.core
  if (state.program_url && !$('programVideo').getAttribute('src')) $('programVideo').src = state.program_url
  if (state.preview_url && !$('previewVideo').getAttribute('src')) $('previewVideo').src = state.preview_url
  if (state.multiview_url && !$('multiviewVideo').getAttribute('src')) $('multiviewVideo').src = state.multiview_url
  $('programName').textContent = nameFor(core.program)
  $('previewName').textContent = nameFor(core.preview)
  $('selectionName').textContent = `SELECTED · ${nameFor(core.preview)}`
  renderMultiview(core)
  renderAudioMixer()
  grid($('programScene'), sceneFor(core.program))
  grid($('previewScene'), sceneFor(core.preview))
  $('revision').textContent = `REV ${core.revision}`
  $('connection').className = 'status live'
  $('connection').innerHTML = '<i></i>LIVE'

  for (const bus of ['program', 'preview']) {
    const box = $(bus + 'Bus')
    box.replaceChildren()
    for (const source of sources) {
      const button = document.createElement('button')
      button.textContent = busLabel(source)
      button.title = `${bus === 'program' ? 'Cut' : 'Preview'} ${source.label}`
      button.className = core[bus] === source.id ? (bus === 'program' ? 'program-on' : 'preview-on') : ''
      button.disabled = busy
      button.onclick = () => command(bus === 'program' ? 'cut' : 'preview', { source: source.id })
      box.append(button)
    }
    for (const bank of ['a', 'b']) {
      const source = `supersource-${bank}`
      const button = document.createElement('button')
      button.textContent = `ME ${meNumber(bank)}`
      button.className = 'ss'
      if (core[bus] === source) button.classList.add(bus === 'program' ? 'program-on' : 'preview-on')
      const scene = state.banks[bank]
      button.title = `ME ${meNumber(bank)}${scene?.ready ? ` · ${scene.count}-UP · ${scene.sources.join('/')}` : ' · not ready'}`
      button.disabled = busy || !scene?.ready
      button.onclick = () => command(bus === 'program' ? 'cut' : 'preview', { source })
      box.append(button)
    }
  }

  const safeBank = offAirBank()
  const editState = $('bankState')
  const safeBankIsPreview = core.preview === `supersource-${safeBank}`
  editState.className = 'edit-safety safe'
  editState.innerHTML = `<span>SAFE EDIT BANK</span><strong>ME ${meNumber(safeBank)} · OFF AIR${safeBankIsPreview ? ' / IN PREVIEW' : ''}</strong><small>${safeBankIsPreview ? 'Changes appear in Preview, never Program' : 'Building here cannot change Program'}</small>`
  $('prepare').disabled = busy || !state.live
  $('prepare').textContent = !state.live
    ? 'SCENE ENGINE PENDING'
    : `BUILD ${layout}-UP ON SAFE ME ${meNumber(safeBank)} → PREVIEW`
  const previewBank = bankOf(core.preview)
  const previewIsProgram = Boolean(previewBank && core.preview === core.program)
  $('takeSS').disabled = busy || !previewBank || previewIsProgram
  $('takeSS').textContent = !previewBank
    ? 'SELECT AN ME IN PREVIEW'
    : previewIsProgram
      ? `ME ${meNumber(previewBank)} ALREADY ON AIR`
      : `CUT ME ${meNumber(previewBank)} TO AIR`
  for (const bank of ['a', 'b']) {
    const button = $(`previewME${meNumber(bank)}`)
    const source = `supersource-${bank}`
    const scene = state.banks[bank]
    const role = core.program === source ? 'program' : core.preview === source ? 'preview' : 'safe'
    const roleLabel = role === 'program' ? 'ON AIR' : role === 'preview' ? 'PREVIEW · NEXT' : 'SAFE TO EDIT'
    const sceneLabel = scene?.ready
      ? `${scene.count}-UP · ${scene.sources.join('/')}${role === 'safe' ? ' · CLICK → PREVIEW' : ''}`
      : 'NOT BUILT'
    button.disabled = busy || !scene?.ready || role === 'program'
    button.className = `me-status ${role}`
    button.innerHTML = `<span>ME ${meNumber(bank)}</span><strong>${roleLabel}</strong><small>${sceneLabel}</small>`
    button.title = role === 'program'
      ? `ME ${meNumber(bank)} is live and locked`
      : scene?.ready
        ? `Load ME ${meNumber(bank)} into Preview: ${scene.count}-up, inputs ${scene.sources.join(', ')}`
        : `ME ${meNumber(bank)} is not ready`
  }
}

async function command(kind, body) {
  busy = true
  render()
  try {
    const output = await api(`/api/mxl/v1/${kind}`, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
    notice(`Accepted command ${output.command_id}; waiting for frame boundary…`)
    setTimeout(refresh, 250)
  } catch (error) {
    notice(error.message, true)
  } finally {
    busy = false
    render()
  }
}

function layoutGrid() {
  const geometry = {
    2: [[6, 1], [6, 1]],
    3: [[8, 2], [4, 1], [4, 1]],
    4: [[6, 1], [6, 1], [6, 1], [6, 1]],
    5: [[4, 1], [4, 1], [4, 1], [6, 1], [6, 1]],
    6: Array(6).fill([4, 1]),
    7: [...Array(4).fill([3, 1]), ...Array(3).fill([4, 1])],
    8: Array(8).fill([3, 1]),
  }[layout]
  $('canvas').style.gridTemplateColumns = 'repeat(12,1fr)'
  $('canvas').style.gridTemplateRows = 'repeat(2,1fr)'
  $('canvas').replaceChildren()
  for (let i = 0; i < layout; i++) {
    const tile = document.createElement('div')
    tile.className = 'tile'
    tile.textContent = slots[i]
    tile.style.gridColumn = `span ${geometry[i][0]}`
    tile.style.gridRow = `span ${geometry[i][1]}`
    $('canvas').append(tile)
  }
  $('slots').replaceChildren()
  for (let i = 0; i < layout; i++) {
    const slot = document.createElement('div')
    slot.className = 'slot'
    const label = document.createElement('label')
    label.innerHTML = `<span>BOX ${i + 1}</span><span>${sources[slots[i] - 1]?.label || 'INPUT ' + slots[i]}</span>`
    const punch = document.createElement('div')
    punch.className = 'source-punch'
    for (let number = 1; number <= 8; number++) {
      const button = document.createElement('button')
      button.textContent = number
      button.title = `Put ${sources[number - 1]?.label || 'Input ' + number} in box ${i + 1}`
      if (slots[i] === number) button.className = 'selected'
      button.disabled = busy
      button.onclick = () => {
        const other = slots.indexOf(number)
        if (other >= 0 && other !== i) [slots[i], slots[other]] = [slots[other], slots[i]]
        else slots[i] = number
        layoutGrid()
      }
      punch.append(button)
    }
    slot.append(label, punch)
    $('slots').append(slot)
  }
}

function buildLayouts() {
  const names = { 2: 'INTERVIEW', 3: 'PANEL', 4: 'GRID', 5: 'PANEL', 6: 'GRID', 7: 'PANEL', 8: 'WALL' }
  $('layouts').replaceChildren()
  for (const count of [2, 3, 4, 5, 6, 7, 8]) {
    const button = document.createElement('button')
    button.innerHTML = `<strong>${count}-UP</strong><small>${names[count]}</small>`
    button.title = `${count}-up ${names[count].toLowerCase()} preset`
    button.className = count === layout ? 'selected' : ''
    button.onclick = () => {
      layout = count
      while (slots.length < count) slots.push((slots.length % 8) + 1)
      slots = slots.slice(0, count)
      buildLayouts()
      layoutGrid()
    }
    $('layouts').append(button)
  }
}

async function prepare() {
  if (new Set(slots).size !== slots.length) return notice('Each box needs a different input.', true)
  const bank = offAirBank()
  busy = true
  render()
  notice(`Punching ${layout}-up on off-air ME ${meNumber(bank)}…`)
  try {
    const result = await api('/api/skin/supersource/prepare', { method: 'POST', body: JSON.stringify({ count: layout, sources: slots }) })
    const timing = result.engine?.ready_ms ? ` in ${(result.engine.ready_ms / 1000).toFixed(1)}s` : ''
    notice(`ME ${meNumber(result.bank)} is ready in Preview${timing}.`)
    await refresh()
  } catch (error) {
    notice(error.message, true)
  } finally {
    busy = false
    render()
  }
}

async function refresh() {
  try {
    state = await api('/api/skin/state')
    sources = await api('/api/mxl/v1/sources')
    render()
  } catch (error) {
    $('connection').className = 'status'
    $('connection').innerHTML = '<i></i>OFFLINE'
    notice(error.message, true)
  }
}

$('cut').onclick = () => command('take')
$('takeSS').onclick = () => command('take')
$('prepare').onclick = prepare
$('afv').onclick = () => audioCommand({ mode: 'afv' })
$('manualAudio').onclick = () => audioCommand({ mode: 'manual' })
$('masterGain').onchange = () => audioCommand({ master_gain: Number($('masterGain').value) / 100 })
$('previewME1').onclick = () => command('preview', { source: 'supersource-a' })
$('previewME2').onclick = () => command('preview', { source: 'supersource-b' })
initPanelOrder()
buildLayouts()
layoutGrid()
refresh()
setInterval(refresh, 1000)
