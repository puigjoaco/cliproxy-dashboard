// Launcher supplies expectedPid, expiresAt, callbackService, waitForWindow, toggleExisting.
// Both the PID and app id must match: normal Chrome windows are never targets.
var finished = false;
var closing = null;
var watched = [];
function matches(window) {
    return Number(window.pid) === expectedPid &&
        String(window.resourceClass) === 'chrome-127.0.0.1__-Default';
}
function finish(result, window) {
    if (finished) return;
    finished = true;
    workspace.windowAdded.disconnect(added);
    workspace.windowRemoved.disconnect(removed);
    watched.forEach(function(item) { item.window.windowClassChanged.disconnect(item.changed); });
    watched = [];
    callDBus(callbackService, '/Activation', 'org.kde.routerpersonal.Activation',
        'finished', result, window ? String(window.internalId) : '');
}
function activate(window) {
    // Use an absolute deadline, including if KWin starts this script late.
    if (finished || Date.now() > expiresAt) return;
    if (!matches(window)) return;
    window.skipTaskbar = true;
    window.skipSwitcher = true;
    window.demandsAttention = false;
    if (toggleExisting) {
        if (closing) return;
        closing = window;
        window.closeWindow();
        return;
    }
    window.minimized = false;
    workspace.activeWindow = window;
    finish(workspace.activeWindow === window ? 'activated' : 'failed', window);
}
function added(window) {
    if (finished || Date.now() > expiresAt || Number(window.pid) !== expectedPid) return;
    // Chrome can publish its final app id after windowAdded. This PID belongs
    // exclusively to our profile, so hide it immediately and await the app id.
    window.skipTaskbar = true;
    window.skipSwitcher = true;
    window.demandsAttention = false;
    if (typeof window.windowClassChanged === 'function') {
        var changed = function() { activate(window); };
        window.windowClassChanged.connect(changed);
        watched.push({window: window, changed: changed});
    }
    activate(window);
}
function removed(window) {
    watched = watched.filter(function(item) {
        if (item.window !== window) return true;
        item.window.windowClassChanged.disconnect(item.changed);
        return false;
    });
    if (!finished && Date.now() <= expiresAt && window === closing) finish('closed', window);
}
workspace.windowAdded.connect(added);
workspace.windowRemoved.connect(removed);
// Existing windows already have their final app id: match both PID and app id
// before touching them. Provisional PID-only hiding is only for new windows.
workspace.windowList().forEach(activate);
if (!finished && !closing && !waitForWindow) finish('missing', null);
