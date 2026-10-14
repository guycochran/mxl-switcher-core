const { InstanceBase, runEntrypoint, InstanceStatus, combineRgb } = require('@companion-module/base')

const INPUTS = Array.from({ length: 8 }, (_, index) => ({
	id: `zoomiso-${index + 1}`,
	label: `Input ${index + 1}`,
	number: index + 1,
}))
const BANKS = [
	{ id: 'supersource-a', label: 'ME 1' },
	{ id: 'supersource-b', label: 'ME 2' },
]
const SOURCES = [...INPUTS, ...BANKS]
const SOURCE_CHOICES = SOURCES.map(({ id, label }) => ({ id, label }))

function bankText(bank) {
	if (!bank || !bank.ready) return 'NOT READY'
	return `${bank.count}-UP ${bank.sources.join('/')}`
}

function bankRole(state, bank) {
	const source = `supersource-${bank}`
	if (state.program === source) return 'ON AIR'
	if (state.preview === source) return 'PREVIEW'
	return state.banks[bank]?.ready ? 'SAFE TO EDIT' : 'NOT READY'
}

class MXLCoreInstance extends InstanceBase {
	async init(config) {
		this.config = config
		this.state = {
			connected: false,
			program: null,
			preview: null,
			revision: 0,
			programFlow: null,
			previewFlow: null,
			banks: { a: {}, b: {} },
			health: {},
			nmosRegistered: false,
			audioState: 'unknown',
		}
		this.initActions()
		this.initFeedbacks()
		this.initVariables()
		this.initPresets()
		this.startPolling()
		this.updateStatus(InstanceStatus.Connecting)
	}

	async configUpdated(config) {
		this.config = config
		this.startPolling()
	}

	async destroy() {
		if (this.pollTimer) clearInterval(this.pollTimer)
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'baseurl',
				label: 'Operator API URL (use the secure local tunnel)',
				width: 8,
				default: 'http://127.0.0.1:13010',
			},
			{
				type: 'number',
				id: 'pollms',
				label: 'Tally poll interval (ms)',
				width: 4,
				default: 300,
				min: 200,
				max: 5000,
			},
			{
				type: 'textinput',
				id: 'nmosurl',
				label: 'NMOS Node URL (optional, use a secure local tunnel)',
				width: 8,
				default: '',
			},
		]
	}

	async api(path, body) {
		const base = (this.config.baseurl || '').replace(/\/$/, '')
		const response = await fetch(`${base}${path}`, {
			method: body === undefined ? 'GET' : 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body),
			signal: AbortSignal.timeout(8000),
		})
		const data = await response.json().catch(() => ({}))
		if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`)
		return data
	}

	async getJson(url) {
		const response = await fetch(url, { signal: AbortSignal.timeout(3000) })
		const data = await response.json().catch(() => ({}))
		if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
		return data
	}

	healthClean(health = this.state.health) {
		const counters = [
			'invalid', 'too_early', 'too_late', 'timeouts', 'reader_reopens',
			'sync_timeouts', 'sync_too_early', 'sync_too_late', 'no_common_window',
			'selected_input_regressions', 'sustained_unavailable_active',
		]
		return health.status === 'running' && counters.every((key) => Number(health[key] || 0) === 0)
	}

	startPolling() {
		if (this.pollTimer) clearInterval(this.pollTimer)
		const tick = async () => {
			try {
				const value = await this.api('/api/skin/state')
				if (!value || !value.core) throw new Error('Invalid MXL Core state response')
				const health = await this.api('/api/mxl/v1/health')
				let nmosRegistered = false
				const nmosBase = String(this.config.nmosurl || '').replace(/\/$/, '')
				if (nmosBase) {
					try {
						const node = await this.getJson(`${nmosBase}/x-nmos/node/v1.3/self`)
						nmosRegistered = !!node.id
					} catch (_error) {
						nmosRegistered = false
					}
				}
				this.state = {
					connected: true,
					program: value.core.program,
					preview: value.core.preview,
					revision: value.core.revision,
					programFlow: value.core.program_flow,
					previewFlow: value.core.preview_flow,
					banks: value.banks || { a: {}, b: {} },
					health,
					nmosRegistered,
					audioState: value.audio?.state || 'unknown',
				}
				this.setVariableValues({
					program: this.labelFor(this.state.program),
					preview: this.labelFor(this.state.preview),
					revision: String(this.state.revision),
					supersource_a: bankText(this.state.banks.a),
					supersource_b: bankText(this.state.banks.b),
					me_1_status: bankRole(this.state, 'a'),
					me_2_status: bankRole(this.state, 'b'),
					core_status: this.healthClean(health) ? 'CLEAN' : 'CHECK',
					core_routed: String(health.routed || 0),
					core_window: String(health.last_common_window_width ?? '—'),
					core_skew: String(health.last_source_skew_frames ?? '—'),
					core_inputs: String(Array.isArray(health.sources) ? health.sources.length : 0),
					core_alarms: String(health.sustained_unavailable_active || 0),
					nmos_status: nmosBase ? (nmosRegistered ? 'REGISTERED' : 'OFFLINE') : 'NOT SET',
					audio_status: String(value.audio?.state || 'unknown').toUpperCase(),
					program_flow_status: value.core.program_flow ? 'PRESENT' : 'MISSING',
					preview_flow_status: value.core.preview_flow ? 'PRESENT' : 'MISSING',
				})
				this.updateStatus(InstanceStatus.Ok)
				this.checkFeedbacks(
					'program_tally', 'preview_tally', 'bank_ready', 'core_connected',
					'core_clean', 'nmos_registered', 'program_flow_present',
					'preview_flow_present', 'four_inputs_present', 'audio_ok',
				)
			} catch (error) {
				this.state.connected = false
				this.checkFeedbacks('core_connected', 'core_clean')
				this.updateStatus(InstanceStatus.ConnectionFailure, String(error).slice(0, 80))
			}
		}
		tick()
		this.pollTimer = setInterval(tick, this.config.pollms || 300)
	}

	labelFor(id) {
		return SOURCES.find((source) => source.id === id)?.label || id || '—'
	}

	initActions() {
		this.setActionDefinitions({
			program: {
				name: 'Cut source directly to Program',
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: 'zoomiso-1', choices: SOURCE_CHOICES }],
				callback: async (action) => this.api('/api/mxl/v1/cut', { source: action.options.source }),
			},
			preview: {
				name: 'Arm source in Preview',
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: 'zoomiso-1', choices: SOURCE_CHOICES }],
				callback: async (action) => this.api('/api/mxl/v1/preview', { source: action.options.source }),
			},
			take: {
				name: 'CUT Preview to Program',
				options: [],
				callback: async () => this.api('/api/mxl/v1/take', {}),
			},
			prepare_supersource: {
				name: 'Prepare ME look off air',
				options: [
					{ type: 'dropdown', id: 'count', label: 'Layout', default: 2, choices: Array.from({ length: 7 }, (_, i) => ({ id: i + 2, label: `${i + 2}-UP` })) },
					{ type: 'textinput', id: 'sources', label: 'Unique input numbers, comma-separated', default: '1,2' },
				],
				callback: async (action) => {
					const count = Number(action.options.count)
					const sources = String(action.options.sources).split(',').map((item) => Number(item.trim())).slice(0, count)
					if (sources.length !== count || sources.some((item) => item < 1 || item > 8) || new Set(sources).size !== count) {
						throw new Error('ME look needs the selected number of unique inputs 1–8')
					}
					return this.api('/api/skin/supersource/prepare', { count, sources })
				},
			},
			take_supersource: {
				name: 'CUT prepared ME look',
				options: [],
				callback: async () => this.api('/api/skin/supersource/take', {}),
			},
		})
	}

	initFeedbacks() {
		this.setFeedbackDefinitions({
			program_tally: {
				type: 'boolean',
				name: 'Program tally',
				defaultStyle: { bgcolor: combineRgb(200, 0, 0), color: combineRgb(255, 255, 255) },
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: 'zoomiso-1', choices: SOURCE_CHOICES }],
				callback: (feedback) => this.state.program === feedback.options.source,
			},
			preview_tally: {
				type: 'boolean',
				name: 'Preview tally',
				defaultStyle: { bgcolor: combineRgb(0, 150, 65), color: combineRgb(255, 255, 255) },
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: 'zoomiso-1', choices: SOURCE_CHOICES }],
				callback: (feedback) => this.state.preview === feedback.options.source,
			},
			bank_ready: {
				type: 'boolean',
				name: 'ME layer ready',
				defaultStyle: { bgcolor: combineRgb(135, 85, 5), color: combineRgb(255, 255, 255) },
				options: [{ type: 'dropdown', id: 'bank', label: 'Bank', default: 'a', choices: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }] }],
				callback: (feedback) => !!this.state.banks[feedback.options.bank]?.ready,
			},
			core_connected: {
				type: 'boolean', name: 'Core API connected',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.connected,
			},
			core_clean: {
				type: 'boolean', name: 'Core critical counters clean',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.connected && this.healthClean(),
			},
			nmos_registered: {
				type: 'boolean', name: 'NMOS Node responding',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.nmosRegistered,
			},
			program_flow_present: {
				type: 'boolean', name: 'Program flow present',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => !!this.state.programFlow,
			},
			preview_flow_present: {
				type: 'boolean', name: 'Preview flow present',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => !!this.state.previewFlow,
			},
			four_inputs_present: {
				type: 'boolean', name: 'Four Core inputs present',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => Array.isArray(this.state.health.sources) && this.state.health.sources.length >= 4,
			},
			audio_ok: {
				type: 'boolean', name: 'Audio meter service healthy',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.audioState === 'ok',
			},
		})
	}

	initVariables() {
		this.setVariableDefinitions([
			{ variableId: 'program', name: 'Current Program source' },
			{ variableId: 'preview', name: 'Current Preview source' },
			{ variableId: 'revision', name: 'Core state revision' },
			{ variableId: 'supersource_a', name: 'ME 1 layout' },
			{ variableId: 'supersource_b', name: 'ME 2 layout' },
			{ variableId: 'me_1_status', name: 'ME 1 tally status' },
			{ variableId: 'me_2_status', name: 'ME 2 tally status' },
			{ variableId: 'core_status', name: 'Core critical health' },
			{ variableId: 'core_routed', name: 'Core routed frames' },
			{ variableId: 'core_window', name: 'Core common-window width' },
			{ variableId: 'core_skew', name: 'Core source skew in frames' },
			{ variableId: 'core_inputs', name: 'Core input count' },
			{ variableId: 'core_alarms', name: 'Active sustained alarms' },
			{ variableId: 'nmos_status', name: 'NMOS Node status' },
			{ variableId: 'audio_status', name: 'Audio meter status' },
			{ variableId: 'program_flow_status', name: 'Program flow status' },
			{ variableId: 'preview_flow_status', name: 'Preview flow status' },
		])
	}

	initPresets() {
		const presets = {}
		for (const source of INPUTS) {
			presets[`program_${source.number}`] = {
				type: 'button', category: '1 · Program bus', name: `PGM ${source.number}`,
				style: { text: `${source.number}\nPGM`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(28, 32, 38) },
				steps: [{ down: [{ actionId: 'program', options: { source: source.id } }], up: [] }],
				feedbacks: [{ feedbackId: 'program_tally', options: { source: source.id }, style: { bgcolor: combineRgb(200, 0, 0) } }],
			}
			presets[`preview_${source.number}`] = {
				type: 'button', category: '2 · Preview bus', name: `PVW ${source.number}`,
				style: { text: `${source.number}\nPVW`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(28, 32, 38) },
				steps: [{ down: [{ actionId: 'preview', options: { source: source.id } }], up: [] }],
				feedbacks: [
					{ feedbackId: 'preview_tally', options: { source: source.id }, style: { bgcolor: combineRgb(0, 150, 65) } },
					{ feedbackId: 'program_tally', options: { source: source.id }, style: { bgcolor: combineRgb(200, 0, 0) } },
				],
			}
		}

		for (let count = 2; count <= 8; count++) {
			const sources = Array.from({ length: count }, (_, i) => i + 1).join(',')
			presets[`prepare_${count}`] = {
				type: 'button', category: '3 · ME looks', name: `Prepare ${count}-UP`,
				style: { text: `PREP\n${count}-UP`, size: '14', color: combineRgb(255, 225, 155), bgcolor: combineRgb(82, 55, 10) },
				steps: [{ down: [{ actionId: 'prepare_supersource', options: { count, sources } }], up: [] }],
				feedbacks: [],
			}
		}

		presets.take = {
			type: 'button', category: '4 · ME and switching', name: 'CUT',
			style: { text: 'CUT', size: '20', color: combineRgb(20, 20, 20), bgcolor: combineRgb(235, 235, 235) },
			steps: [{ down: [{ actionId: 'take', options: {} }], up: [] }], feedbacks: [],
		}
		presets.take_supersource = {
			type: 'button', category: '4 · ME and switching', name: 'CUT prepared ME',
			style: { text: 'CUT\nME', size: '16', color: combineRgb(20, 20, 20), bgcolor: combineRgb(255, 190, 60) },
			steps: [{ down: [{ actionId: 'take_supersource', options: {} }], up: [] }], feedbacks: [],
		}
		for (const bank of ['a', 'b']) {
			const source = `supersource-${bank}`
			const me = bank === 'a' ? 1 : 2
			presets[`program_me_${bank}`] = {
				type: 'button', category: '4 · ME and switching', name: `ME ${me} to Program`,
				style: { text: `ME ${me}\nPGM`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(50, 38, 18) },
				steps: [{ down: [{ actionId: 'program', options: { source } }], up: [] }],
				feedbacks: [
					{ feedbackId: 'bank_ready', options: { bank }, style: { bgcolor: combineRgb(110, 72, 8) } },
					{ feedbackId: 'program_tally', options: { source }, style: { bgcolor: combineRgb(200, 0, 0) } },
				],
			}
			presets[`preview_me_${bank}`] = {
				type: 'button', category: '4 · ME and switching', name: `ME ${me} smart Preview`,
				style: { text: `ME ${me}\nNOT READY`, size: '14', color: combineRgb(190, 195, 202), bgcolor: combineRgb(28, 32, 38) },
				steps: [{ down: [{ actionId: 'preview', options: { source } }], up: [] }],
				feedbacks: [
					{ feedbackId: 'bank_ready', options: { bank }, style: { text: `ME ${me}\nSAFE · LOAD`, color: combineRgb(255, 238, 188), bgcolor: combineRgb(110, 72, 8) } },
					{ feedbackId: 'preview_tally', options: { source }, style: { text: `ME ${me}\nPREVIEW`, color: combineRgb(255, 255, 255), bgcolor: combineRgb(0, 150, 65) } },
					{ feedbackId: 'program_tally', options: { source }, style: { text: `ME ${me}\nON AIR`, color: combineRgb(255, 255, 255), bgcolor: combineRgb(200, 0, 0) } },
				],
			}
		}

		const healthPreset = (name, text, feedbackId) => ({
			type: 'button', category: '5 · EIC health', name,
			style: { text, size: '12', color: combineRgb(255, 255, 255), bgcolor: combineRgb(150, 28, 28) },
			steps: [{ down: [], up: [] }],
			feedbacks: [{ feedbackId, options: {}, style: { bgcolor: combineRgb(0, 145, 60) } }],
		})
		presets.health_core = healthPreset('Core health', 'CORE\n$(mxl:core_status)', 'core_clean')
		presets.health_nmos = healthPreset('NMOS registration', 'NMOS\n$(mxl:nmos_status)', 'nmos_registered')
		presets.health_inputs = healthPreset('Four Core inputs', 'INPUTS\n$(mxl:core_inputs)', 'four_inputs_present')
		presets.health_program = healthPreset('Program flow', 'PGM FLOW\n$(mxl:program_flow_status)', 'program_flow_present')
		presets.health_preview = healthPreset('Preview flow', 'PVW FLOW\n$(mxl:preview_flow_status)', 'preview_flow_present')
		presets.health_audio = healthPreset('Audio meter', 'AUDIO\n$(mxl:audio_status)', 'audio_ok')
		this.setPresetDefinitions(presets)
	}
}

runEntrypoint(MXLCoreInstance, [])
