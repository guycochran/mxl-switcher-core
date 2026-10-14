const { InstanceBase, runEntrypoint, InstanceStatus, combineRgb } = require('@companion-module/base')
const { FALLBACK_SOURCES, normalizedSources, sourceChoices, sourceSignature } = require('./source_catalog')

const BANKS = [
	{ id: 'supersource-a', label: 'ME 1' },
	{ id: 'supersource-b', label: 'ME 2' },
]
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

function normalizedAudioState(state) {
	if (state === 'ok') return 'METER OK'
	if (state === 'disabled') return 'METER OFF'
	if (state === 'unavailable') return 'METER FAIL'
	return 'METER ?'
}

function resourceCount(value) {
	return Array.isArray(value) ? value.length : 0
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
			routingMoving: false,
			nmosNodeOnline: false,
			nmosRegistryOnline: false,
			nmosRegistered: false,
			nmosCounts: { devices: 0, sources: 0, flows: 0, senders: 0, receivers: 0 },
			nmosClock: 'UNKNOWN',
			audioState: 'unknown',
			sources: FALLBACK_SOURCES,
			sourceSignature: sourceSignature(FALLBACK_SOURCES),
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
				label: 'NMOS Node API URL (optional, use a secure local tunnel)',
				width: 8,
				default: '',
			},
			{
				type: 'textinput',
				id: 'nmosregistryurl',
				label: 'NMOS Registry Query URL (optional, proves registration)',
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
				const [health, sourceRows] = await Promise.all([
					this.api('/api/mxl/v1/health'),
					this.api('/api/mxl/v1/sources'),
				])
				const sources = normalizedSources(sourceRows)
				const nextSourceSignature = sourceSignature(sources)
				const sourceCatalogChanged = nextSourceSignature !== this.state.sourceSignature
				let nmosNodeOnline = false
				let nmosRegistryOnline = false
				let nmosRegistered = false
				let nmosCounts = { devices: 0, sources: 0, flows: 0, senders: 0, receivers: 0 }
				let nmosClock = 'UNKNOWN'
				let nmosNodeId = null
				const nmosBase = String(this.config.nmosurl || '').replace(/\/$/, '')
				const registryBase = String(this.config.nmosregistryurl || '').replace(/\/$/, '')
				if (nmosBase) {
					try {
						const [node, devices, sources, flows, senders, receivers] = await Promise.all([
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/self`),
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/devices`),
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/sources`),
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/flows`),
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/senders`),
							this.getJson(`${nmosBase}/x-nmos/node/v1.3/receivers`),
						])
						nmosNodeId = node.id || null
						nmosNodeOnline = !!nmosNodeId
						nmosCounts = {
							devices: resourceCount(devices), sources: resourceCount(sources),
							flows: resourceCount(flows), senders: resourceCount(senders),
							receivers: resourceCount(receivers),
						}
						const clocks = Array.isArray(node.clocks) ? node.clocks : []
						nmosClock = clocks.some((clock) => clock.ref_type === 'ptp') ? 'PTP' : (clocks.length ? 'INTERNAL' : 'UNKNOWN')
					} catch (_error) {
						nmosNodeOnline = false
					}
				}
				if (registryBase) {
					try {
						const nodes = await this.getJson(`${registryBase}/x-nmos/query/v1.3/nodes`)
						nmosRegistryOnline = Array.isArray(nodes)
						nmosRegistered = !!nmosNodeId && nodes.some((node) => node.id === nmosNodeId)
					} catch (_error) {
						nmosRegistryOnline = false
						nmosRegistered = false
					}
				}
				const previousRouted = Number(this.state.health?.routed)
				const currentRouted = Number(health.routed)
				const routingMoving = this.state.connected && Number.isFinite(previousRouted) && Number.isFinite(currentRouted) && currentRouted > previousRouted
				const inputCount = Array.isArray(health.sources) ? health.sources.length : 0
				const activeAlarms = Number(health.sustained_unavailable_active || 0)
				const liveInputs = Math.max(0, inputCount - activeAlarms)
				this.state = {
					connected: true,
					program: value.core.program,
					preview: value.core.preview,
					revision: value.core.revision,
					programFlow: value.core.program_flow,
					previewFlow: value.core.preview_flow,
					banks: value.banks || { a: {}, b: {} },
					health,
					routingMoving,
					nmosNodeOnline,
					nmosRegistryOnline,
					nmosRegistered,
					nmosCounts,
					nmosClock,
					audioState: value.audio?.state || 'unknown',
					sources,
					sourceSignature: nextSourceSignature,
				}
				if (sourceCatalogChanged) {
					this.initActions()
					this.initFeedbacks()
					this.initPresets()
					this.log('info', `MXL source catalog updated (${sources.length} routable sources)`)
				}
				this.setVariableValues({
					program: this.labelFor(this.state.program),
					preview: this.labelFor(this.state.preview),
					revision: String(this.state.revision),
					supersource_a: bankText(this.state.banks.a),
					supersource_b: bankText(this.state.banks.b),
					me_1_status: bankRole(this.state, 'a'),
					me_2_status: bankRole(this.state, 'b'),
					core_link_status: 'ONLINE',
					core_status: this.healthClean(health)
						? 'CLEAN'
						: (activeAlarms ? `${activeAlarms} INPUT ALARM${activeAlarms === 1 ? '' : 'S'}` : 'DEGRADED'),
					core_routed: String(health.routed || 0),
					routing_status: routingMoving ? 'MOVING' : 'STALLED',
					core_window: String(health.last_common_window_width ?? '—'),
					core_skew: String(health.last_source_skew_frames ?? '—'),
					core_inputs: String(inputCount),
					core_live_inputs: String(liveInputs),
					core_input_status: `${liveInputs}/${inputCount} LIVE`,
					core_alarms: String(activeAlarms),
					nmos_status: registryBase
						? (nmosRegistryOnline ? (nmosRegistered ? 'REGISTERED' : 'UNREGISTERED') : 'REGISTRY OFFLINE')
						: (nmosNodeOnline ? 'NODE ONLINE' : (nmosBase ? 'NODE OFFLINE' : 'NOT SET')),
					nmos_node_status: nmosNodeOnline ? 'ONLINE' : (nmosBase ? 'OFFLINE' : 'NOT SET'),
					nmos_registry_status: registryBase ? (nmosRegistryOnline ? 'ONLINE' : 'OFFLINE') : 'NOT SET',
					nmos_devices: String(nmosCounts.devices),
					nmos_sources: String(nmosCounts.sources),
					nmos_flows: String(nmosCounts.flows),
					nmos_senders: String(nmosCounts.senders),
					nmos_receivers: String(nmosCounts.receivers),
					nmos_clock: nmosClock,
					audio_status: normalizedAudioState(value.audio?.state || 'unknown'),
					program_flow_status: value.core.program_flow ? 'PRESENT' : 'MISSING',
					preview_flow_status: value.core.preview_flow ? 'PRESENT' : 'MISSING',
				})
				this.updateStatus(InstanceStatus.Ok)
				this.checkFeedbacks(
					'program_tally', 'preview_tally', 'bank_ready', 'core_connected',
					'core_clean', 'core_degraded', 'routing_moving', 'nmos_node_online',
					'nmos_registry_online', 'nmos_registered', 'nmos_registration_failed', 'program_flow_present',
					'preview_flow_present', 'all_inputs_live', 'audio_ok', 'audio_failed',
				)
			} catch (error) {
				this.state.connected = false
				this.setVariableValues({ core_link_status: 'OFFLINE', routing_status: 'UNKNOWN' })
				this.checkFeedbacks('core_connected', 'core_clean', 'core_degraded', 'routing_moving')
				this.updateStatus(InstanceStatus.ConnectionFailure, String(error).slice(0, 80))
			}
		}
		tick()
		this.pollTimer = setInterval(tick, this.config.pollms || 300)
	}

	labelFor(id) {
		return [...this.state.sources, ...BANKS].find((source) => source.id === id)?.label || id || '—'
	}

	initActions() {
		const choices = sourceChoices(this.state.sources, BANKS)
		const defaultSource = choices[0]?.id || 'zoomiso-1'
		this.setActionDefinitions({
			program: {
				name: 'Cut source directly to Program',
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: defaultSource, choices }],
				callback: async (action) => this.api('/api/mxl/v1/cut', { source: action.options.source }),
			},
			preview: {
				name: 'Arm source in Preview',
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: defaultSource, choices }],
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
		const choices = sourceChoices(this.state.sources, BANKS)
		const defaultSource = choices[0]?.id || 'zoomiso-1'
		this.setFeedbackDefinitions({
			program_tally: {
				type: 'boolean',
				name: 'Program tally',
				defaultStyle: { bgcolor: combineRgb(200, 0, 0), color: combineRgb(255, 255, 255) },
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: defaultSource, choices }],
				callback: (feedback) => this.state.program === feedback.options.source,
			},
			preview_tally: {
				type: 'boolean',
				name: 'Preview tally',
				defaultStyle: { bgcolor: combineRgb(0, 150, 65), color: combineRgb(255, 255, 255) },
				options: [{ type: 'dropdown', id: 'source', label: 'Source', default: defaultSource, choices }],
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
			core_degraded: {
				type: 'boolean', name: 'Core has an active critical alarm',
				defaultStyle: { bgcolor: combineRgb(190, 0, 0), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.connected && !this.healthClean(),
			},
			routing_moving: {
				type: 'boolean', name: 'Core routed counter is moving',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.connected && this.state.routingMoving,
			},
			nmos_node_online: {
				type: 'boolean', name: 'NMOS Node API responding',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.nmosNodeOnline,
			},
			nmos_registry_online: {
				type: 'boolean', name: 'NMOS Registry Query API responding',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.nmosRegistryOnline,
			},
			nmos_registered: {
				type: 'boolean', name: 'NMOS Node is present in the Registry',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.nmosRegistered,
			},
			nmos_registration_failed: {
				type: 'boolean', name: 'NMOS Registry configured but Node is not registered',
				defaultStyle: { bgcolor: combineRgb(190, 0, 0), color: combineRgb(255, 255, 255) },
				options: [], callback: () => !!String(this.config.nmosregistryurl || '').trim() && !this.state.nmosRegistered,
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
			all_inputs_live: {
				type: 'boolean', name: 'All configured Core inputs live',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => {
					const sources = Array.isArray(this.state.health.sources) ? this.state.health.sources : []
					return sources.length > 0 && Number(this.state.health.sustained_unavailable_active || 0) === 0
				},
			},
			audio_ok: {
				type: 'boolean', name: 'Audio meter service healthy',
				defaultStyle: { bgcolor: combineRgb(0, 145, 60), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.audioState === 'ok',
			},
			audio_failed: {
				type: 'boolean', name: 'Audio meter service failed',
				defaultStyle: { bgcolor: combineRgb(190, 0, 0), color: combineRgb(255, 255, 255) },
				options: [], callback: () => this.state.audioState === 'unavailable',
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
			{ variableId: 'core_link_status', name: 'Core API connection' },
			{ variableId: 'core_routed', name: 'Core routed frames' },
			{ variableId: 'routing_status', name: 'Core routing motion' },
			{ variableId: 'core_window', name: 'Core common-window width' },
			{ variableId: 'core_skew', name: 'Core source skew in frames' },
			{ variableId: 'core_inputs', name: 'Core input count' },
			{ variableId: 'core_live_inputs', name: 'Live Core input count' },
			{ variableId: 'core_input_status', name: 'Live / configured Core inputs' },
			{ variableId: 'core_alarms', name: 'Active sustained alarms' },
			{ variableId: 'nmos_status', name: 'NMOS Node status' },
			{ variableId: 'nmos_node_status', name: 'NMOS Node API status' },
			{ variableId: 'nmos_registry_status', name: 'NMOS Registry API status' },
			{ variableId: 'nmos_devices', name: 'NMOS device count' },
			{ variableId: 'nmos_sources', name: 'NMOS source count' },
			{ variableId: 'nmos_flows', name: 'NMOS flow count' },
			{ variableId: 'nmos_senders', name: 'NMOS sender count' },
			{ variableId: 'nmos_receivers', name: 'NMOS receiver count' },
			{ variableId: 'nmos_clock', name: 'NMOS clock type' },
			{ variableId: 'audio_status', name: 'Audio meter status' },
			{ variableId: 'program_flow_status', name: 'Program flow status' },
			{ variableId: 'preview_flow_status', name: 'Preview flow status' },
		])
	}

	initPresets() {
		const presets = {}
		for (const source of normalizedSources(this.state.sources)) {
			const key = source.id.replace(/[^a-zA-Z0-9_-]/g, '_')
			const shortLabel = source.label.length > 10 ? source.label.slice(0, 10) : source.label
			presets[`program_${key}`] = {
				type: 'button', category: '1 · Program bus', name: `PGM ${source.label}`,
				style: { text: `${shortLabel}\nPGM`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(28, 32, 38) },
				steps: [{ down: [{ actionId: 'program', options: { source: source.id } }], up: [] }],
				feedbacks: [{ feedbackId: 'program_tally', options: { source: source.id }, style: { bgcolor: combineRgb(200, 0, 0) } }],
			}
			presets[`preview_${key}`] = {
				type: 'button', category: '2 · Preview bus', name: `PVW ${source.label}`,
				style: { text: `${shortLabel}\nPVW`, size: '14', color: combineRgb(255, 255, 255), bgcolor: combineRgb(28, 32, 38) },
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

		const healthPreset = (name, text, feedbackId, baseColor = combineRgb(150, 28, 28), extraFeedbacks = []) => ({
			type: 'button', category: '5 · EIC health', name,
			style: { text, size: '12', color: combineRgb(255, 255, 255), bgcolor: baseColor },
			steps: [{ down: [], up: [] }],
			feedbacks: [
				{ feedbackId, options: {}, style: { bgcolor: combineRgb(0, 145, 60) } },
				...extraFeedbacks,
			],
		})
		presets.health_core_link = healthPreset('Core API link', 'CORE API\n$(mxl:core_link_status)', 'core_connected')
		presets.health_core = healthPreset('Core alarm health', 'CORE\n$(mxl:core_status)', 'core_clean')
		presets.health_routing = healthPreset('Core routing motion', 'ROUTING\n$(mxl:routing_status)', 'routing_moving')
		presets.health_nmos = healthPreset(
			'NMOS registration', 'NMOS\n$(mxl:nmos_status)', 'nmos_registered', combineRgb(65, 70, 78),
			[{ feedbackId: 'nmos_registration_failed', options: {}, style: { bgcolor: combineRgb(190, 0, 0) } }],
		)
		presets.health_inputs = healthPreset('Live Core inputs', 'INPUTS\n$(mxl:core_input_status)', 'all_inputs_live')
		presets.health_program = healthPreset('Program flow declared', 'PGM DECLARED\n$(mxl:program_flow_status)', 'program_flow_present')
		presets.health_preview = healthPreset('Preview flow declared', 'PVW DECLARED\n$(mxl:preview_flow_status)', 'preview_flow_present')
		presets.health_audio = healthPreset(
			'Audio meter', 'AUDIO\n$(mxl:audio_status)', 'audio_ok', combineRgb(65, 70, 78),
			[{ feedbackId: 'audio_failed', options: {}, style: { bgcolor: combineRgb(190, 0, 0) } }],
		)

		const nmosPreset = (name, text, feedbackId = 'nmos_node_online') => ({
			type: 'button', category: '6 · NMOS intelligence', name,
			style: { text, size: '12', color: combineRgb(255, 255, 255), bgcolor: combineRgb(45, 72, 105) },
			steps: [{ down: [], up: [] }],
			feedbacks: [{ feedbackId, options: {}, style: { bgcolor: combineRgb(0, 125, 125) } }],
		})
		presets.nmos_node = nmosPreset('NMOS Node API', 'NODE\n$(mxl:nmos_node_status)')
		presets.nmos_registry = nmosPreset('NMOS Registry API', 'REGISTRY\n$(mxl:nmos_registry_status)', 'nmos_registry_online')
		presets.nmos_registered = nmosPreset('Node registration', 'NODE\n$(mxl:nmos_status)', 'nmos_registered')
		presets.nmos_devices = nmosPreset('NMOS devices', 'DEVICES\n$(mxl:nmos_devices)')
		presets.nmos_sources = nmosPreset('NMOS sources', 'SOURCES\n$(mxl:nmos_sources)')
		presets.nmos_flows = nmosPreset('NMOS flows', 'FLOWS\n$(mxl:nmos_flows)')
		presets.nmos_senders = nmosPreset('NMOS senders', 'SENDERS\n$(mxl:nmos_senders)')
		presets.nmos_receivers = nmosPreset('NMOS receivers', 'RECEIVERS\n$(mxl:nmos_receivers)')
		presets.nmos_clock = nmosPreset('NMOS reference clock', 'CLOCK\n$(mxl:nmos_clock)')
		this.setPresetDefinitions(presets)
	}
}

runEntrypoint(MXLCoreInstance, [])
