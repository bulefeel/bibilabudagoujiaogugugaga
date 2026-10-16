const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../../src/ziniao_automation/static/app.js'), 'utf8');
const actionCode = script.slice(script.indexOf('    const runAction = target.dataset.runAction;'), script.indexOf('\n  });', script.indexOf('    const runAction = target.dataset.runAction;')));

function harness({agreed = true, fail = false} = {}) {
  const calls = [], confirmations = [], timers = [], notices = [];
  const action = vm.runInNewContext(`(async target => {${actionCode}\n})`, {
    confirmAction: async message => { confirmations.push(message); return agreed; },
    api: async (...args) => { calls.push(args); if (fail) throw new Error('状态冲突'); },
    toast: (...args) => notices.push(args),
    setTimeout: fn => timers.push(fn),
    location: {reload() {}},
  });
  const target = {disabled: false, dataset: {runAction: 'resume-inventory', runId: 'same-run', confirm: '请先在对应紫鸟确认页面可正常查看，点击才将当前任务入队'}};
  return {calls, confirmations, timers, notices, target, run: () => action(target)};
}

test('manual confirmation cancellation never queues inventory work', async () => {
  const h = harness({agreed: false}); await h.run();
  assert.equal(h.confirmations.length, 1); assert.equal(h.calls.length, 0);
  assert.equal(h.target.disabled, false); assert.equal(h.timers.length, 0);
});
test('explicit confirmation queues the existing run through the correct endpoint', async () => {
  const h = harness(); await h.run();
  assert.match(h.confirmations[0], /对应紫鸟/);
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], '/api/runs/same-run/resume-inventory');
  assert.equal(h.calls[0][1].method, 'POST'); assert.equal(h.calls[0][1].body, '{}');
  assert.equal(h.target.disabled, true); assert.equal(h.timers.length, 1);
});
test('conflicting resume restores button without reloading or retrying', async () => {
  const h = harness({fail: true}); await h.run();
  assert.equal(h.calls.length, 1); assert.equal(h.target.disabled, false);
  assert.equal(h.timers.length, 0); assert.equal(h.notices[0][0], '状态冲突');
});
test('a paused run creates no background polling timer', () => {
  const poll = script.slice(script.indexOf('  const RUN_TERMINAL_STATUSES'), script.indexOf('  async function apiResult'));
  for (const status of ['PAUSED', 'FAILED']) {
    let timers = 0;
    const context = vm.createContext({$: () => ({dataset: {runId: 'same-run', runStatus: status}}), setInterval: () => timers++});
    vm.runInContext(poll + '\nstartRunDetailPolling();', context);
    assert.equal(timers, 0, status);
  }
});
