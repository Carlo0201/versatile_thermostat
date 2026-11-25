// Runs the CURRENT Oikos card code on the scenario inputs and writes the reference outputs.
// Read only on the card: nothing in the SDK is modified. Usage:
//   node tests/tools/export_card_reference.mjs <sdk>/cards/_shared tests/fixtures/room_status/scenarios.json tests/fixtures/room_status/reference_card.json
// The card reads a few entity ids fixed in its own source; they are extracted from that source at
// run time (never written here) so the scenarios can keep placeholder ids.
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const [shared, scenariosPath, outPath] = process.argv.slice(2)
if (!shared || !scenariosPath || !outPath) throw new Error('usage: export_card_reference.mjs <shared dir> <scenarios.json> <out.json>')
const load = name => import(pathToFileURL(path.resolve(shared, name)).href)
const text = name => fs.readFileSync(path.resolve(shared, name), 'utf8')
const { heatingChecklist, groupChecklist } = await load('heatingChecklist.js')
const { explainRoom } = await load('roomExplanation.js')

const fixed = (file, pattern, label) => {
  const match = pattern.exec(text(file))
  if (!match) throw new Error(`cannot read the card constant ${label} from ${file}`)
  return match[1]
}
const CARD_MASTER = fixed('roomExplanation.js', /const MASTER = '([^']+)'/, 'MASTER')
const CARD_CENTRAL = fixed('heatingChecklist.js', /modes\(state\('([^']+)'\)\)/, 'central mode')
const CARD_START_TIMER = fixed('roomExplanation.js', /const START_TIMER = '([^']+)'/, 'START_TIMER')
const CARD_BOILER_DEMAND = fixed('roomExplanation.js', /const BOILER_DEMAND = '([^']+)'/, 'BOILER_DEMAND')

const input = JSON.parse(fs.readFileSync(scenariosPath, 'utf8'))
const nowMs = input.now * 1000
const realNow = Date.now
Date.now = () => nowMs // the card reads the wall clock in explainRoom / readNativeHeatPump
const changed = new Date(nowMs - 60000).toISOString()
const config = Object.fromEntries(input.config_section.rooms.map(room => [room.climate_entity_id, room]))

function snapshotOf(scenario) {
  const snapshot = {}
  for (const [id, entity] of Object.entries(scenario.states)) snapshot[id] = { state: entity.state, attributes: entity.attributes ?? {}, last_changed: changed }
  snapshot[CARD_MASTER] = { ...snapshot[input.master_entity_id] }
  snapshot[CARD_CENTRAL] = { ...snapshot[input.central_mode_entity_id] }
  snapshot[input.boiler_entity_id] = { state: scenario.boiler.state, attributes: scenario.boiler.attributes, last_changed: changed }
  // The card still reads two legacy signals to decide that a room is waiting for the boiler: the start
  // timer and the consent sensor. The native equivalents are the M1 deadline and the awaiting_consent
  // reason of the very same diagnostic, so they are fed from it (the integration has no such entities).
  const diagnostic = scenario.boiler.attributes
  if (Number.isFinite(diagnostic.deadline) && diagnostic.deadline > input.now) {
    snapshot[CARD_START_TIMER] = { state: 'active', attributes: { finishes_at: new Date(diagnostic.deadline * 1000).toISOString() }, last_changed: changed }
  }
  snapshot[CARD_BOILER_DEMAND] = { state: diagnostic.reason === 'awaiting_consent' ? 'on' : 'off', attributes: {}, last_changed: changed }
  if (scenario.pdc) snapshot[input.pdc_entity_id] = { state: scenario.pdc.state, attributes: scenario.pdc.attributes, last_changed: changed }
  return snapshot
}

const rows = list => (list ?? []).map(item => ({ id: item.id, status: item.status, ...(item.codes ? { codes: item.codes } : {}), ...(item.note ? { note: item.note } : {}) }))
const out = []
for (const scenario of input.scenarios) {
  const snapshot = snapshotOf(scenario)
  const pdc = config[scenario.climate_entity_id]?.pdc
  const room = { entityId: scenario.climate_entity_id, ...(pdc ? { heatPumpSplitEntityId: pdc.split_entity_id, heatPumpSessionEntityId: pdc.session_entity_id, ...(scenario.pdc ? { heatPumpNativeDiagnosticEntityId: input.pdc_entity_id } : {}) } : {}) }
  const source = { boilerSource: 'native', boilerDiagnosticEntityId: input.boiler_entity_id }
  const getState = id => snapshot[id]?.state ?? null
  const getAttr = (id, name) => snapshot[id]?.attributes?.[name] ?? null
  const entry = { name: scenario.name }
  try {
    const model = heatingChecklist(room, snapshot, nowMs, true, source)
    const grouped = groupChecklist(model)
    entry.checklist = { radiator: rows(model.radiator.rows), boiler: rows(model.boiler.rows), pump: model.pump ? rows(model.pump.rows) : null }
    entry.groups = {
      stages: grouped.stages.map(stage => ({ key: stage.key, status: stage.status, passed: stage.passed, total: stage.total })),
      blocker: grouped.blocker ? { id: grouped.blocker.id, stage: grouped.blocker.stage, status: grouped.blocker.status } : null,
    }
    const explained = explainRoom(room, { snapshot, getState, getAttr, boilerSource: source })
    entry.explain = {
      state: explained.state, reason: explained.reason, detailReasons: explained.detailReasons,
      deadline: explained.deadline == null ? null : explained.deadline / 1000, gap: explained.gap,
    }
  } catch (error) {
    entry.error = String(error?.stack ?? error).split('\n').slice(0, 3).join(' | ')
  }
  out.push(entry)
}
Date.now = realNow

const files = ['heatingChecklist.js', 'roomExplanation.js', 'heatingStatus.js', 'nativeBoilerControl.js', 'heatPump.js', 'climate.js', 'boilerControl.js']
const hashes = Object.fromEntries(files.map(file => [file, createHash('sha256').update(fs.readFileSync(path.resolve(shared, file))).digest('hex')]))
fs.writeFileSync(outPath, JSON.stringify({ generated_from: hashes, now: input.now, scenarios: out }, null, 1) + '\n')
console.log(`${out.length} scenarios, ${out.filter(entry => entry.error).length} with errors -> ${outPath}`)
