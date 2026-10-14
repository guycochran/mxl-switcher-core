const FALLBACK_SOURCES = Array.from({ length: 8 }, (_, index) => ({
	id: `zoomiso-${index + 1}`,
	label: `Input ${index + 1}`,
	number: index + 1,
}))

function normalizedSources(rows) {
	if (!Array.isArray(rows) || rows.length === 0) return FALLBACK_SOURCES
	return rows
		.filter((row) => row && typeof row.id === 'string' && row.id)
		.map((row, index) => ({
			id: row.id,
			label: row.label || row.id,
			number: index + 1,
			kind: row.kind || 'other',
			capabilities: Array.isArray(row.capabilities) ? row.capabilities : ['video'],
		}))
}

function sourceChoices(rows, banks) {
	return [...normalizedSources(rows), ...banks].map(({ id, label }) => ({ id, label }))
}

function sourceSignature(rows) {
	return JSON.stringify(normalizedSources(rows).map(({ id, label, kind, capabilities }) => ({ id, label, kind, capabilities })))
}

module.exports = { FALLBACK_SOURCES, normalizedSources, sourceChoices, sourceSignature }
