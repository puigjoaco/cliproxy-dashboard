'use strict';
const assert = require('node:assert/strict');
const status = require('../desktop/plasma-tray/package/contents/code/status.js');
const fs = require('node:fs');
const vm = require('node:vm');
const now = new Date('2026-11-23T23:30:00-03:00').getTime();
const ago = ms => new Date(now - ms).toISOString();
const healthy = () => ({ok: false, health: {
    gateway: {status: 'healthy', checked_at: ago(10000)},
    codex: {status: 'down'},
    usage_collection: {status: 'healthy', last_success_at: ago(60000)},
    backup: {status: 'healthy', last_success_at: ago(3600000)},
    accounts: [{provider: 'codex', email: 'a@example.com', status: 'healthy'},
        {provider: 'codex', email: 'b@example.com', status: 'limited'}]
}});
const observe = data => status.receive(status.initial(now), data, now);
const describe = data => status.describe(observe(data), now);
let count = 0;
function test(name, fn) { fn(); count++; console.log('PASS', name); }
test('initial state, first failure and second failure; a success resets the failure count', () => {
    let state = status.initial(now);
    assert.equal(status.describe(state, now).kind, 'starting');
    state = status.receive(state, null, now + 30000);
    assert.equal(status.describe(state, now + 30000).kind, 'starting');
    state = status.receive(state, null, now + 60000);
    assert.equal(status.describe(state, now + 60000).statusLine, 'El panel no responde');
    state = status.receive(state, healthy(), now + 60000);
    assert.equal(status.describe(state, now + 60000).kind, 'ok');
});
test('ok and local codex are ignored; a weekly-limited account is informational', () => {
    const view = describe(healthy());
    assert.equal(view.kind, 'ok'); assert.equal(view.attention, false);
    assert.deepEqual(view.details, ['2 cuentas · 1 en su límite']);
    assert.equal(view.tooltip, 'Funcionando\n2 cuentas · 1 en su límite\nClic para abrir o cerrar el panel');
});
test('dashboard failure has priority over gateway down, which has priority over warnings', () => {
    const data = healthy(); data.health.gateway.status = 'down'; data.health.backup.status = 'failed';
    const state = observe(data);
    assert.equal(status.describe(state, now).statusLine, 'El router no responde');
    state.failures = 2;
    assert.equal(status.describe(state, now).statusLine, 'El panel no responde');
});
test('each account problem has its approved Spanish detail', () => {
    for (const [state, text] of Object.entries({sign_in_required: 'a@example.com (Codex) necesita volver a iniciar sesión',
        enable_failed: 'a@example.com no se pudo reactivar', disable_unconfirmed: 'a@example.com tiene un estado sin confirmar',
        unknown: 'a@example.com tiene un estado sin confirmar', stale: 'a@example.com tiene un estado sin confirmar',
        unavailable: 'a@example.com no está disponible', intentionally_disabled: 'a@example.com no está disponible'})) {
        const data = healthy(); data.health.accounts[0].status = state;
        const view = describe(data); assert.equal(view.kind, 'warn'); assert.equal(view.details[0], text);
    }
});
test('a provider with no usable accounts and an empty connected inventory need attention', () => {
    const data = healthy(); data.health.accounts[0].status = 'limited';
    assert.deepEqual(describe(data).details, ['Ninguna cuenta de Codex disponible ahora']);
    data.health.accounts = []; assert.deepEqual(describe(data).details, ['No hay cuentas conectadas']);
});
test('unhealthy usage and failed or old backups need attention', () => {
    const data = healthy(); data.health.usage_collection.status = 'stale';
    assert.match(describe(data).details[0], /^El registro de uso no se actualiza desde las \d\d:\d\d$/);
    data.health.usage_collection.last_success_at = null;
    assert.equal(describe(data).details[0], 'El registro de uso no se actualiza desde las —');
    data.health.usage_collection.status = 'healthy'; data.health.backup.status = 'failed';
    assert.deepEqual(describe(data).details, ['La copia de seguridad falló']);
    data.health.backup = {status: 'healthy', last_success_at: ago(36 * 3600000 + 1)};
    assert.deepEqual(describe(data).details, ['La última copia de seguridad tiene más de 36 horas']);
});
test('unknown empty startup is normal, then warns after three minutes', () => {
    const data = healthy(); data.health.gateway.status = 'unknown'; data.health.accounts = [];
    data.health.usage_collection.status = 'unknown'; data.health.backup.status = 'unknown';
    const state = observe(data);
    assert.equal(status.describe(state, now + 60000).kind, 'starting');
    assert.equal(status.describe(state, now + 180000).kind, 'starting');
    assert.deepEqual(status.describe(state, now + 180001).details, ['El estado no se actualiza desde hace 3 min']);
});
test('stale gateway checks; real warnings retain priority while checking', () => {
    const data = healthy(); data.health.gateway.status = 'stale';
    assert.equal(describe(data).kind, 'starting');
    data.health.backup.status = 'failed'; assert.equal(describe(data).kind, 'warn');
    const state = observe(healthy());
    assert.equal(status.describe(state, now + 180000).kind, 'warn');
});
test('tooltips cap details at three, with correct singular and plural overflow', () => {
    const data = healthy(); data.health.accounts[0].status = 'sign_in_required';
    data.health.usage_collection.status = 'failed'; data.health.backup.status = 'failed';
    assert.deepEqual(describe(data).details.slice(-1), ['y 1 aviso más']);
    data.health.accounts.push({provider: 'claude', email: 'c@example.com', status: 'enable_failed'});
    assert.deepEqual(describe(data).details.slice(-1), ['y 3 avisos más']);
    assert.equal(describe(data).details.length, 4);
});
test('malformed API data is rejected', () => {
    for (const value of [null, {}, {health: {}}, {health: {gateway: {status: 'healthy'}}}])
        assert.equal(status.validPayload(value), false);
    assert.equal(status.validPayload(healthy()), true);
});
function activator(windows, options = {}) {
    const callbacks = [];
    const listeners = new Set();
    const removed = new Set();
    const workspace = {activeWindow: null, windowList: () => windows, windowAdded: {
        connect: fn => listeners.add(fn), disconnect: fn => listeners.delete(fn)
    }, windowRemoved: {
        connect: fn => removed.add(fn), disconnect: fn => removed.delete(fn)
    }};
    const context = {workspace, expectedPid: 1234, expiresAt: Date.now() + 8000,
        callbackService: 'test', waitForWindow: true, toggleExisting: false,
        callDBus: (...args) => callbacks.push(args), ...options};
    vm.runInNewContext(fs.readFileSync(require.resolve(
        '../desktop/plasma-tray/package/contents/code/activate.js'), 'utf8'), context);
    return {workspace, callbacks, add: window => [...listeners].forEach(fn => fn(window)),
        remove: window => [...removed].forEach(fn => fn(window)), listeners};
}
const dashboardWindow = (pid = 1234) => ({pid, resourceClass: 'chrome-127.0.0.1__-Default',
    minimized: true, internalId: 'dedicated-window'});
test('activation restores exactly one dedicated window and ignores ordinary Chrome', () => {
    const ordinary = {pid: 1234, resourceClass: 'google-chrome', minimized: true};
    const sameAppOtherProfile = dashboardWindow(4321);
    const dedicated = dashboardWindow();
    const result = activator([ordinary, sameAppOtherProfile, dedicated, dashboardWindow()]);
    assert.equal(result.workspace.activeWindow, dedicated);
    assert.equal(dedicated.minimized, false);
    assert.equal(dedicated.skipTaskbar, true);
    assert.equal(dedicated.skipSwitcher, true);
    assert.equal(dedicated.demandsAttention, false);
    assert.equal(ordinary.minimized, true);
    assert.equal(ordinary.skipTaskbar, undefined, 'an ordinary window of the dedicated profile stays in the taskbar');
    assert.equal(ordinary.skipSwitcher, undefined);
    assert.equal(sameAppOtherProfile.minimized, true);
    assert.equal(result.callbacks.length, 1);
    assert.equal(result.callbacks[0].at(-2), 'activated');
    assert.equal(result.listeners.size, 0);
});
test('new-window activation is bounded and a script left by a crash cannot raise windows later', () => {
    const active = activator([]);
    const window = dashboardWindow();
    active.add(window);
    assert.equal(active.workspace.activeWindow, window);
    active.add(dashboardWindow());
    assert.equal(active.callbacks.length, 1);
    const stale = activator([], {expiresAt: Date.now() - 1});
    stale.add(dashboardWindow());
    assert.equal(stale.workspace.activeWindow, null);
    assert.equal(stale.callbacks.length, 0);
});
test('existing-window lookup reports absence without activating another profile', () => {
    const result = activator([dashboardWindow(4321)], {waitForWindow: false, toggleExisting: true});
    assert.equal(result.callbacks[0].at(-2), 'missing');
    assert.equal(result.listeners.size, 0);
});
test('toggle really closes an existing window without activating it, including when minimized', () => {
    for (const minimized of [true, false]) {
        const window = dashboardWindow();
        let closes = 0;
        window.minimized = minimized;
        window.closeWindow = () => { closes++; };
        const result = activator([window], {waitForWindow: false, toggleExisting: true});
        assert.equal(closes, 1);
        assert.equal(result.workspace.activeWindow, null);
        assert.equal(result.callbacks.length, 0, 'close is confirmed only after windowRemoved');
        result.remove(window);
        assert.equal(result.callbacks[0].at(-2), 'closed');
        result.add(dashboardWindow());
        assert.equal(result.callbacks.length, 1);
    }
});
test('a delayed Chrome app id is hidden immediately and activated when its class becomes ready', () => {
    const callbacks = new Set();
    const window = dashboardWindow();
    window.resourceClass = 'google-chrome';
    window.windowClassChanged = () => {};
    window.windowClassChanged.connect = fn => callbacks.add(fn);
    window.windowClassChanged.disconnect = fn => callbacks.delete(fn);
    const result = activator([]);
    result.add(window);
    assert.equal(window.skipTaskbar, true);
    assert.equal(window.skipSwitcher, true);
    assert.equal(result.workspace.activeWindow, null);
    window.resourceClass = 'chrome-127.0.0.1__-Default';
    [...callbacks].forEach(fn => fn());
    assert.equal(result.workspace.activeWindow, window);
    assert.equal(result.callbacks[0].at(-2), 'activated');
    assert.equal(callbacks.size, 0);
});
console.log(`Plasma tray: ${count}/${count} passed`);
