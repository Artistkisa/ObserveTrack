import test from 'node:test'
import assert from 'node:assert/strict'
import { activityState, createPoller } from './state.js'

test('waiting and incomplete collection are never shown as active work or no-change', () => {
  assert.equal(activityState({ latest_job: { status: 'waiting_rate_limit' } }), 'waiting_rate_limit')
  assert.equal(activityState({ latest_job: { status: 'completed', result: '{"status":"partial"}' } }), 'partial')
  assert.equal(activityState({ latest_job: { status: 'running', cancel: 1 } }), 'stopping')
})

test('shared request chain does not overlap and late replies cannot update unmounted views', async () => {
  let resolve, count = 0, emitted = 0, aborted = false
  const poller = createPoller(signal => { count++; signal.addEventListener('abort', () => { aborted = true }); return new Promise(done => { resolve = done }) }, () => { emitted++ })
  const first = poller.refresh()
  await poller.refresh()
  assert.equal(count, 1)
  poller.stop()
  resolve({})
  await first
  assert.equal(emitted, 0)
  assert.equal(aborted, true)
})
