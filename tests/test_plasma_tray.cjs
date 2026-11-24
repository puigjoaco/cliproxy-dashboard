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
const describe = (data, lang = 'es', url) => status.describe(observe(data), now, lang, url);
let count = 0;
function test(name, fn) { fn(); count++; console.log('PASS', name); }
test('initial state, first failure and second failure; a success resets the failure count', () => {
    let state = status.initial(now);
    assert.equal(status.describe(state, now, 'es').kind, 'starting');
    state = status.receive(state, null, now + 30000);
    assert.equal(status.describe(state, now + 30000, 'es').kind, 'starting');
    state = status.receive(state, null, now + 60000);
    assert.equal(status.describe(state, now + 60000, 'es').statusLine, 'El panel no responde');
    state = status.receive(state, healthy(), now + 60000);
    assert.equal(status.describe(state, now + 60000, 'es').kind, 'ok');
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
    assert.equal(status.describe(state, now, 'es').statusLine, 'El router no responde');
    state.failures = 2;
    assert.equal(status.describe(state, now, 'es').statusLine, 'El panel no responde');
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
    assert.equal(status.describe(state, now + 60000, 'es').kind, 'starting');
    assert.equal(status.describe(state, now + 180000, 'es').kind, 'starting');
    assert.deepEqual(status.describe(state, now + 180001, 'es').details, ['El estado no se actualiza desde hace 3 min']);
});
test('stale gateway checks; real warnings retain priority while checking', () => {
    const data = healthy(); data.health.gateway.status = 'stale';
    assert.equal(describe(data).kind, 'starting');
    data.health.backup.status = 'failed'; assert.equal(describe(data).kind, 'warn');
    const state = observe(healthy());
    assert.equal(status.describe(state, now + 180000, 'es').kind, 'warn');
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
test('every status reads in English too, with the same structure', () => {
    const view = describe(healthy(), 'en');
    assert.equal(view.tooltip, 'Working\n2 accounts · 1 at their limit\nClick to open or close the dashboard');
    const data = healthy(); data.health.accounts[0].status = 'sign_in_required';
    data.health.usage_collection.status = 'failed'; data.health.backup.status = 'failed';
    data.health.accounts.push({provider: 'claude', email: 'c@example.com', status: 'enable_failed'});
    assert.deepEqual(describe(data, 'en').details, ['a@example.com (Codex) needs to sign in again',
        "c@example.com couldn't be re-enabled", 'No Codex account available right now', 'and 3 more warnings']);
    data.health.accounts = [data.health.accounts[1]]; data.health.backup.status = 'healthy';
    data.health.usage_collection = {status: 'failed', last_success_at: null};
    assert.deepEqual(describe(data, 'en').details, ['No Codex account available right now', 'Usage log not updated since —']);
    data.health.backup = {status: 'stale'};
    assert.equal(describe(data, 'en').details[2], 'Last backup is more than 36 hours old');
    const down = healthy(); down.health.gateway.status = 'down';
    assert.deepEqual([describe(down, 'en').statusLine, describe(down, 'en').details],
        ['Router not responding', ["Codex and Claude can't use your accounts right now"]]);
    const empty = healthy(); empty.health.accounts = [];
    assert.deepEqual(describe(empty, 'en').details, ['No accounts connected']);
    // Every key exists in both languages.
    for (const key of ['checking', 'open', 'copy', 'switchLanguage', 'urlLabel', 'urlInvalid', 'click'])
        for (const lang of ['es', 'en']) assert.ok(status.text(lang, key) && !status.text(lang, key).includes('undefined'), key);
});
test('language follows a saved choice, else Spanish only for a Spanish system language', () => {
    for (const [configured, system, expected] of [['', ['es-CL', 'en-US'], 'es'], ['', 'es_MX', 'es'], ['', ['es'], 'es'],
        ['', ['en-US', 'es-CL'], 'en'], ['', ['de-DE'], 'en'], ['', [], 'en'], [undefined, undefined, 'en'],
        ['', ['estonian'], 'en'], ['en', ['es-CL'], 'en'], ['es', ['en-US'], 'es'], ['fr', ['es-ES'], 'es']])
        assert.equal(status.language(configured, system), expected, `${configured} ${system}`);
    // Qt hands uiLanguages over as an array-like sequence.
    assert.equal(status.language('', {length: 1, 0: 'es-AR'}), 'es');
    assert.equal(status.otherLanguage('es'), 'en'); assert.equal(status.otherLanguage('en'), 'es');
    assert.equal(status.text('es', 'switchLanguage'), 'Switch to English');
    assert.equal(status.text('en', 'switchLanguage'), 'Cambiar a español');
    assert.equal(status.describe(status.initial(now), now, 'xx').statusLine, 'Checking status…');
});
test('the dashboard address is configurable but must stay on this computer', () => {
    for (const [input, expected] of [['http://127.0.0.1:8320', 'http://127.0.0.1:8320'],
        [' HTTP://LOCALHOST:9000/ ', 'http://localhost:9000'], ['https://127.0.0.1:443', 'https://127.0.0.1:443'],
        ['http://ROUTER.LOCALHOST:9000/', 'http://router.localhost:9000'], ['http://localhost', 'http://localhost'],
        ['http://127.0.0.1:08320', 'http://127.0.0.1:8320']])
        assert.equal(status.normalizeUrl(input), expected, input);
    for (const input of ['', null, undefined, 'ftp://127.0.0.1:8320', 'http://example.com:8320', 'http://192.168.1.2:8320',
        'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:8320/api', 'http://user@127.0.0.1:8320',
        'http://127.0.0.1:8320?x=1', 'http://127.0.0.1.evil.test', 'http://127.0.0.010:8320', 'http://127.0.0.256',
        'http://127.0.0.2:9000', 'http://127.1.2.3', 'http://[::1]:8321',
        'http://router.localhost.evil.test', 'http://foo.localhost', 'http://LOCALHOST.:9000',
        'javascript:alert(1)', 'http://localhost:8320#x'])
        assert.equal(status.normalizeUrl(input), null, String(input));
    assert.equal(status.dashboardUrl('http://example.com'), status.DEFAULT_URL);
    assert.equal(status.dashboardUrl('http://localhost:9000/'), 'http://localhost:9000');
    let state = status.initial(now);
    state = status.receive(status.receive(state, null, now), null, now);
    assert.deepEqual(status.describe(state, now, 'es', 'http://localhost:9000').details, ['No se puede leer el estado en localhost:9000']);
    assert.deepEqual(status.describe(state, now, 'en').details, ["Can't read the status at 127.0.0.1:8320"]);
});
function activator(windows, options = {}) {
    const callbacks = [];
    const ready = [];
    const listeners = new Set();
    const removed = new Set();
    const workspace = {activeWindow: null, windowList: () => windows, windowAdded: {
        connect: fn => listeners.add(fn), disconnect: fn => listeners.delete(fn)
    }, windowRemoved: {
        connect: fn => removed.add(fn), disconnect: fn => removed.delete(fn)
    }};
    const context = {workspace, expectedPid: 1234, expectedClass: 'chrome-127.0.0.1__-Default', expiresAt: Date.now() + 8000,
        callbackService: 'test', waitForWindow: true, toggleExisting: false,
        callDBus: (...args) => (args.at(-2) === 'ready' ? ready : callbacks).push(args), ...options};
    vm.runInNewContext(fs.readFileSync(require.resolve(
        '../desktop/plasma-tray/package/contents/code/activate.js'), 'utf8'), context);
    return {workspace, callbacks, ready, add: window => [...listeners].forEach(fn => fn(window)),
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
        assert.equal(result.ready.length, 0, 'closing an existing window must never request another launch');
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
test('a warm toggle requests one launch only when absent and activates the newly added window', () => {
    const result = activator([], {toggleExisting: true});
    assert.equal(result.ready.length, 1);
    assert.equal(result.callbacks.length, 0);
    const window = dashboardWindow();
    window.closeWindow = () => { throw new Error('new windows must not close'); };
    result.add(window);
    assert.equal(result.workspace.activeWindow, window);
    assert.equal(result.callbacks[0].at(-2), 'activated');
    result.add(dashboardWindow());
    assert.equal(result.ready.length, 1);
    assert.equal(result.callbacks.length, 1);
});
test('the window class follows the configured dashboard host', () => {
    const local = dashboardWindow(), named = {...dashboardWindow(), resourceClass: 'chrome-localhost__-Default'};
    const result = activator([local, named], {expectedClass: 'chrome-localhost__-Default'});
    assert.equal(result.workspace.activeWindow, named);
    assert.equal(local.skipTaskbar, undefined, 'the old host is not this dashboard');
});
console.log(`Plasma tray: ${count}/${count} passed`);
