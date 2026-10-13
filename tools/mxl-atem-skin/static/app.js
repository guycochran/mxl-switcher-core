const $ = (id) => document.getElementById(id)
let state = null
let sources = []
let layout = 4
let slots = [1, 2, 3, 4]
let busy = false

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
  for (let i = 1; i <= 8; i++) {
    const id = `zoomiso-${i}`
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

function offAirBank() {
  const active = new Set([state?.core.program, state?.core.preview])
  return active.has('supersource-a') ? 'b' : 'a'
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
  grid($('programScene'), sceneFor(core.program))
  grid($('previewScene'), sceneFor(core.preview))
  $('revision').textContent = `REV ${core.revision}`
  $('connection').className = 'status live'
  $('connection').innerHTML = '<i></i>LIVE'

  for (const bus of ['program', 'preview']) {
    const box = $(bus + 'Bus')
    box.replaceChildren()
    for (let i = 1; i <= 8; i++) {
      const button = document.createElement('button')
      button.textContent = i
      button.className = core[bus] === `zoomiso-${i}` ? (bus === 'program' ? 'program-on' : 'preview-on') : ''
      button.disabled = busy
      button.onclick = () => command(bus === 'program' ? 'cut' : 'preview', { source: `zoomiso-${i}` })
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

  $('bankState').textContent = state.live ? `OFF-AIR ME ${meNumber(offAirBank())}` : 'SCENE ENGINE PENDING'
  $('prepare').disabled = busy || !state.live
  $('prepare').textContent = state.live ? 'PUNCH LOOK TO PREVIEW' : 'SCENE ENGINE PENDING'
  $('takeSS').disabled = busy || !bankOf(core.preview)
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
  const cols = layout <= 2 ? layout : layout <= 4 ? 2 : 4
  $('canvas').style.gridTemplateColumns = `repeat(${cols},1fr)`
  $('canvas').replaceChildren()
  for (let i = 0; i < layout; i++) {
    const tile = document.createElement('div')
    tile.className = 'tile'
    tile.textContent = slots[i]
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
  const names = { 2: 'INTERVIEW', 3: 'PANEL', 4: 'GRID', 5: 'PANEL', 6: 'GRID', 7: 'FORUM', 8: 'WALL' }
  $('layouts').replaceChildren()
  for (let count = 2; count <= 8; count++) {
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
  busy = true
  render()
  const bank = offAirBank()
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
buildLayouts()
layoutGrid()
refresh()
setInterval(refresh, 1000)
