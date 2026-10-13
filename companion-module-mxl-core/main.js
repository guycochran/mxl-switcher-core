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

class MXLCoreInstance extends InstanceBase {
	async init(config) {
		this.config = config
		this.state = { program: null, preview: null, revision: 0, banks: { a: {}, b: {} } }
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

	startPolling() {
		if (this.pollTimer) clearInterval(this.pollTimer)
		const tick = async () => {
			try {
				const value = await this.api('/api/skin/state')
				if (!value || !value.core) throw new Error('Invalid MXL Core state response')
				this.state = {
					program: value.core.program,
					preview: value.core.preview,
					revision: value.core.revision,
					banks: value.banks || { a: {}, b: {} },
				}
				this.setVariableValues({
					program: this.labelFor(this.state.program),
					preview: this.labelFor(this.state.preview),
					revision: String(this.state.revision),
					supersource_a: bankText(this.state.banks.a),
					supersource_b: bankText(this.state.banks.b),
				})
				this.updateStatus(InstanceStatus.Ok)
				this.checkFeedbacks('program_tally', 'preview_tally', 'bank_ready')
			} catch (error) {
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
		})
	}

	initVariables() {
		this.setVariableDefinitions([
			{ variableId: 'program', name: 'Current Program source' },
			{ variableId: 'preview', name: 'Current Preview source' },
			{ variableId: 'revision', name: 'Core state revision' },
			{ variableId: 'supersource_a', name: 'ME 1 layout' },
			{ variableId: 'supersource_b', name: 'ME 2 layout' },
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
				type: 'button', category: '4 · ME and switching', name: `ME ${me} to Preview`,
				style: { text: `ME ${me}\nPVW`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(50, 38, 18) },
				steps: [{ down: [{ actionId: 'preview', options: { source } }], up: [] }],
				feedbacks: [
					{ feedbackId: 'bank_ready', options: { bank }, style: { bgcolor: combineRgb(110, 72, 8) } },
					{ feedbackId: 'preview_tally', options: { source }, style: { bgcolor: combineRgb(0, 150, 65) } },
					{ feedbackId: 'program_tally', options: { source }, style: { bgcolor: combineRgb(200, 0, 0) } },
				],
			}
		}
		this.setPresetDefinitions(presets)
	}
}

runEntrypoint(MXLCoreInstance, [])
