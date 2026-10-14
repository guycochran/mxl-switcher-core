const assert = require('node:assert/strict')
const { FALLBACK_SOURCES, normalizedSources, sourceChoices, sourceSignature } = require('./source_catalog')

assert.equal(normalizedSources([]), FALLBACK_SOURCES)

const rows = [
	{ id: 'playout-tc', label: 'Playout', kind: 'playout', capabilities: ['video', 'timecode'] },
	{ id: 'keyed-tc', label: 'Keyed', kind: 'graphics', capabilities: ['video', 'graphics'] },
]
assert.deepEqual(normalizedSources(rows), [
	{ ...rows[0], number: 1 },
	{ ...rows[1], number: 2 },
])
assert.deepEqual(sourceChoices(rows, [{ id: 'supersource-a', label: 'ME 1' }]), [
	{ id: 'playout-tc', label: 'Playout' },
	{ id: 'keyed-tc', label: 'Keyed' },
	{ id: 'supersource-a', label: 'ME 1' },
])
assert.notEqual(sourceSignature(rows), sourceSignature(rows.slice(0, 1)))
console.log('Dynamic Companion source-catalog tests passed.')
