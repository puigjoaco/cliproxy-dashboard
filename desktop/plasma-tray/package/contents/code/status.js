// Shared by QML and the Node regression test. No router state is changed here.
var DASHBOARD_URL = 'http://127.0.0.1:8320';
var UNKNOWN_MS = 3 * 60 * 1000;
var BACKUP_MS = 36 * 60 * 60 * 1000;

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

function describe(state, now) {
    var health = state.health;
    var details = [], warnings = [], kind = 'starting', line = 'Comprobando el estado…';
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
        kind = 'down'; line = 'El panel no responde';
        details = ['No se puede leer el estado en 127.0.0.1:8320'];
    } else if (gateway.status === 'down') {
        kind = 'down'; line = 'El router no responde';
        details = ['Codex y Claude no pueden usar tus cuentas ahora'];
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
            var email = account.email || account.key || providerName(provider);
            if (account.status === 'limited') {
                limited++;
            } else if (account.status === 'sign_in_required') {
                warnings.push(email + ' (' + providerName(provider) + ') necesita volver a iniciar sesión');
            } else if (account.status === 'enable_failed') {
                warnings.push(email + ' no se pudo reactivar');
            } else if (['unknown', 'stale', 'disable_unconfirmed'].indexOf(account.status) >= 0) {
                if (!unknown) warnings.push(email + ' tiene un estado sin confirmar');
            } else if (account.status !== 'healthy') {
                warnings.push(email + ' no está disponible');
            }
        });
        if (!bootstrap) {
            Object.keys(providers).forEach(function (provider) {
                if (providers[provider].usable === 0 && !unknown)
                    warnings.push('Ninguna cuenta de ' + providerName(provider) + ' disponible ahora');
            });
            if (!accounts.length) warnings.push('No hay cuentas conectadas');
        }
        var usage = health && health.usage_collection;
        if (usage && usage.status !== 'healthy' && !(bootstrap && ['unknown', 'stale'].indexOf(usage.status) >= 0))
            warnings.push('El registro de uso no se actualiza desde las ' + timeText(usage.last_success_at));
        if (health && !usage && !bootstrap)
            warnings.push('El registro de uso no se actualiza desde las —');
        var backup = health && health.backup;
        if (backup && backup.status === 'failed') warnings.push('La copia de seguridad falló');
        else if (backup && (backup.status === 'stale' || now - Date.parse(backup.last_success_at) > BACKUP_MS))
            warnings.push('La última copia de seguridad tiene más de 36 horas');
        if (unknown && since !== null && now - since > UNKNOWN_MS)
            warnings.push('El estado no se actualiza desde hace ' + Math.floor((now - since) / 60000) + ' min');
        if (warnings.length) {
            kind = 'warn'; line = 'Necesita atención'; details = warnings;
        } else if (!unknown && health) {
            kind = 'ok'; line = 'Funcionando';
            // /api/health does not say which allowance window is exhausted.
            details = [accounts.length + ' cuentas · ' + limited + ' en su límite'];
        }
    }
    var visible = details.slice(0, 3);
    var more = details.length - visible.length;
    if (more) visible.push('y ' + more + (more === 1 ? ' aviso más' : ' avisos más'));
    return {kind: kind, attention: kind === 'down' || kind === 'warn', statusLine: line,
        details: visible, title: 'Router personal',
        tooltip: [line].concat(visible, ['Clic para abrir el panel']).join('\n')};
}

if (typeof module !== 'undefined') module.exports = {initial: initial, validPayload: validPayload,
    receive: receive, describe: describe};
