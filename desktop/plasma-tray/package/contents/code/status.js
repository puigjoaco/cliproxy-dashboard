// Shared by QML and the Node regression test. No router state is changed here.
var DEFAULT_URL = 'http://127.0.0.1:8320';
var UNKNOWN_MS = 3 * 60 * 1000;
var BACKUP_MS = 36 * 60 * 60 * 1000;

var STRINGS = {
    es: {
        checking: 'Comprobando el estado…',
        dashboardDown: 'El panel no responde',
        dashboardUnreadable: 'No se puede leer el estado en {address}',
        routerDown: 'El router no responde',
        routerDownDetail: 'Codex y Claude no pueden usar tus cuentas ahora',
        signIn: '{account} ({provider}) necesita volver a iniciar sesión',
        enableFailed: '{account} no se pudo reactivar',
        unconfirmed: '{account} tiene un estado sin confirmar',
        unavailable: '{account} no está disponible',
        noneUsable: 'Ninguna cuenta de {provider} disponible ahora',
        noAccounts: 'No hay cuentas conectadas',
        usageStale: 'El registro de uso no se actualiza desde las {time}',
        backupFailed: 'La copia de seguridad falló',
        backupOld: 'La última copia de seguridad tiene más de 36 horas',
        statusStale: 'El estado no se actualiza desde hace {minutes} min',
        attention: 'Necesita atención',
        working: 'Funcionando',
        summary: '{accounts} cuentas · {limited} en su límite',
        moreOne: 'y 1 aviso más',
        moreMany: 'y {count} avisos más',
        click: 'Clic para abrir o cerrar el panel',
        open: 'Abrir panel del router',
        copy: 'Copiar dirección del panel',
        // Written in the language it switches to, so either reader finds it.
        switchLanguage: 'Switch to English',
        urlLabel: 'Dirección del panel:',
        urlInvalid: 'Usa una dirección http o https de este equipo, por ejemplo http://127.0.0.1:8320. Mientras tanto se usa esa.'
    },
    en: {
        checking: 'Checking status…',
        dashboardDown: 'Dashboard not responding',
        dashboardUnreadable: "Can't read the status at {address}",
        routerDown: 'Router not responding',
        routerDownDetail: "Codex and Claude can't use your accounts right now",
        signIn: '{account} ({provider}) needs to sign in again',
        enableFailed: "{account} couldn't be re-enabled",
        unconfirmed: '{account} has an unconfirmed status',
        unavailable: '{account} is unavailable',
        noneUsable: 'No {provider} account available right now',
        noAccounts: 'No accounts connected',
        usageStale: 'Usage log not updated since {time}',
        backupFailed: 'Backup failed',
        backupOld: 'Last backup is more than 36 hours old',
        statusStale: 'Status not updated for {minutes} min',
        attention: 'Needs attention',
        working: 'Working',
        summary: '{accounts} accounts · {limited} at their limit',
        moreOne: 'and 1 more warning',
        moreMany: 'and {count} more warnings',
        click: 'Click to open or close the dashboard',
        open: 'Open router dashboard',
        copy: 'Copy dashboard address',
        switchLanguage: 'Cambiar a español',
        urlLabel: 'Dashboard address:',
        urlInvalid: 'Use an http or https address on this computer, such as http://127.0.0.1:8320. Until then, that address is used.'
    }
};

// A saved choice wins; otherwise Spanish for a Spanish system language, English for any other.
function language(configured, systemLanguages) {
    if (configured === 'es' || configured === 'en') return configured;
    // Qt passes uiLanguages as an array-like sequence, not always a real Array.
    var first = typeof systemLanguages === 'string' ? systemLanguages
        : systemLanguages && systemLanguages.length ? systemLanguages[0] : '';
    first = String(first || '').toLowerCase();
    return /^es(?:[-_@.]|$)/.test(first) ? 'es' : 'en';
}

function otherLanguage(lang) {
    return lang === 'es' ? 'en' : 'es';
}

function text(lang, key, values) {
    var table = STRINGS[lang] || STRINGS.en;
    return String(table[key]).replace(/\{(\w+)\}/g, function (match, name) {
        return values && values[name] !== undefined ? String(values[name]) : match;
    });
}

// The dashboard binds 127.0.0.1: accept its allowed http(s) host names at the
// root path, without credentials, paths or queries. Returns the canonical form or null.
function normalizeUrl(value) {
    var match = /^(https?):\/\/(localhost|router\.localhost|127\.0\.0\.1)(?::(\d{1,5}))?\/?$/i
        .exec(String(value === undefined || value === null ? '' : value).trim());
    if (!match) return null;
    var port = match[3] === undefined ? null : Number(match[3]);
    if (port !== null && (port < 1 || port > 65535)) return null;
    return match[1].toLowerCase() + '://' + match[2].toLowerCase() + (port === null ? '' : ':' + port);
}

function dashboardUrl(configured) {
    return normalizeUrl(configured) || DEFAULT_URL;
}

function initial(now) {
    return {health: null, failures: 0, unknownSince: now, lastSuccess: null};
}

function validPayload(data) {
    return !!(data && data.health && data.health.gateway &&
        typeof data.health.gateway.status === 'string' && Array.isArray(data.health.accounts));
}

function receive(previous, data, now) {
    var next = {
        health: previous.health,
        failures: data ? 0 : previous.failures + 1,
        unknownSince: previous.unknownSince,
        lastSuccess: previous.lastSuccess
    };
    if (data) {
        next.health = data.health;
        next.lastSuccess = now;
    }
    var gateway = next.health && next.health.gateway;
    var unknown = !gateway || ['healthy', 'down'].indexOf(gateway.status) < 0;
    if (unknown && next.unknownSince === null) next.unknownSince = now;
    if (!unknown) next.unknownSince = null;
    return next;
}

function providerName(id) {
    return {codex: 'Codex', claude: 'Claude', antigravity: 'Antigravity', gemini: 'Gemini',
        openai: 'OpenAI', anthropic: 'Claude', xai: 'Grok'}[id] || id || 'Router';
}

function timeText(value) {
    if (!value) return '—';
    var date = new Date(value);
    return isNaN(date.getTime()) ? '—' : ('0' + date.getHours()).slice(-2) + ':' + ('0' + date.getMinutes()).slice(-2);
}

function describe(state, now, lang, url) {
    lang = STRINGS[lang] ? lang : 'en';
    var t = function (key, values) { return text(lang, key, values); };
    var health = state.health;
    var details = [], warnings = [], kind = 'starting', line = t('checking');
    var gateway = health && health.gateway || {};
    var unknown = !health || ['healthy', 'down'].indexOf(gateway.status) < 0;
    var since = state.unknownSince;
    // Re-evaluate age between successful checks, including a temporarily lost dashboard.
    var staleAt = Date.parse(gateway.checked_at);
    if (gateway.status === 'healthy' && isFinite(staleAt) && now - staleAt > UNKNOWN_MS) {
        unknown = true;
        since = since === null ? staleAt : since;
    }
    if (state.failures >= 2) {
        kind = 'down'; line = t('dashboardDown');
        details = [t('dashboardUnreadable', {address: dashboardUrl(url).replace(/^https?:\/\//, '')})];
    } else if (gateway.status === 'down') {
        kind = 'down'; line = t('routerDown');
        details = [t('routerDownDetail')];
    } else {
        var accounts = health && health.accounts || [];
        // The dashboard starts with unknown gateway and empty inventory. That is a check,
        // not proof that accounts were removed or that the collector/backup failed.
        var bootstrap = unknown && accounts.length === 0;
        var providers = {}, limited = 0;
        accounts.forEach(function (account) {
            var provider = account.provider || 'Router';
            if (!providers[provider]) providers[provider] = {total: 0, usable: 0};
            providers[provider].total++;
            if (account.status === 'healthy') providers[provider].usable++;
            var name = account.email || account.key || providerName(provider);
            if (account.status === 'limited') {
                limited++;
            } else if (account.status === 'sign_in_required') {
                warnings.push(t('signIn', {account: name, provider: providerName(provider)}));
            } else if (account.status === 'enable_failed') {
                warnings.push(t('enableFailed', {account: name}));
            } else if (['unknown', 'stale', 'disable_unconfirmed'].indexOf(account.status) >= 0) {
                if (!unknown) warnings.push(t('unconfirmed', {account: name}));
            } else if (account.status !== 'healthy') {
                warnings.push(t('unavailable', {account: name}));
            }
        });
        if (!bootstrap) {
            Object.keys(providers).forEach(function (provider) {
                if (providers[provider].usable === 0 && !unknown)
                    warnings.push(t('noneUsable', {provider: providerName(provider)}));
            });
            if (!accounts.length) warnings.push(t('noAccounts'));
        }
        var usage = health && health.usage_collection;
        if (usage && usage.status !== 'healthy' && !(bootstrap && ['unknown', 'stale'].indexOf(usage.status) >= 0))
            warnings.push(t('usageStale', {time: timeText(usage.last_success_at)}));
        if (health && !usage && !bootstrap)
            warnings.push(t('usageStale', {time: '—'}));
        var backup = health && health.backup;
        if (backup && backup.status === 'failed') warnings.push(t('backupFailed'));
        else if (backup && (backup.status === 'stale' || now - Date.parse(backup.last_success_at) > BACKUP_MS))
            warnings.push(t('backupOld'));
        if (unknown && since !== null && now - since > UNKNOWN_MS)
            warnings.push(t('statusStale', {minutes: Math.floor((now - since) / 60000)}));
        if (warnings.length) {
            kind = 'warn'; line = t('attention'); details = warnings;
        } else if (!unknown && health) {
            kind = 'ok'; line = t('working');
            // /api/health does not say which allowance window is exhausted.
            details = [t('summary', {accounts: accounts.length, limited: limited})];
        }
    }
    var visible = details.slice(0, 3);
    var more = details.length - visible.length;
    if (more) visible.push(more === 1 ? t('moreOne') : t('moreMany', {count: more}));
    return {kind: kind, attention: kind === 'down' || kind === 'warn', statusLine: line,
        details: visible, title: 'Router personal',
        tooltip: [line].concat(visible, [t('click')]).join('\n')};
}

if (typeof module !== 'undefined') module.exports = {DEFAULT_URL: DEFAULT_URL, initial: initial,
    validPayload: validPayload, receive: receive, describe: describe, language: language,
    otherLanguage: otherLanguage, text: text, normalizeUrl: normalizeUrl, dashboardUrl: dashboardUrl};
