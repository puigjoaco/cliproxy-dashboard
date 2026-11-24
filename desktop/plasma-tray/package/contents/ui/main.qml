pragma ComponentBehavior: Bound

import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import org.kde.plasma.plasma5support as Plasma5Support
import "../code/status.js" as RouterStatus

PlasmoidItem {
    id: root

    // An invalid saved address falls back to the default; the settings page says so.
    readonly property string dashboardUrl: RouterStatus.dashboardUrl(Plasmoid.configuration.dashboardUrl)
    readonly property string language: RouterStatus.language(Plasmoid.configuration.language, Qt.locale().uiLanguages)
    property var observation: RouterStatus.initial(Date.now())
    property var view: RouterStatus.describe(observation, Date.now(), language, dashboardUrl)
    property var request: null
    property bool openingDashboard: false
    readonly property url iconUrl: Qt.resolvedUrl("../icons/router-" + view.kind + "-22.svg")

    // Always shown next to the clock; NeedsAttention would make Plasma pulse the icon.
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    Plasmoid.icon: iconUrl
    Plasmoid.title: "Router personal"
    Plasmoid.hasConfigurationInterface: true
    toolTipMainText: view.title
    toolTipSubText: view.tooltip
    toolTipTextFormat: Text.PlainText
    activationTogglesExpanded: false
    preferredRepresentation: compactRepresentation
    // Plasma otherwise treats direct children as the only representation and
    // never instantiates compactRepresentation. Activation still never expands.
    fullRepresentation: EmptyRepresentation {}

    function openDashboard() {
        if (openingDashboard) return;
        openingDashboard = true;
        var path = decodeURIComponent(Qt.resolvedUrl("../code/open_dashboard.py").toString().replace(/^file:\/\//, ""));
        // Quote the installed path, including spaces or apostrophes in XDG_DATA_HOME.
        // dashboardUrl is a validated loopback address; quote it anyway.
        var quote = function (value) { return "'" + value.replace(/'/g, "'\\''") + "'"; };
        var command = "/usr/bin/python3 " + quote(path) + " --url " + quote(dashboardUrl);
        launcher.connectSource(command);
    }

    Plasma5Support.DataSource {
        id: launcher
        engine: "executable"
        connectedSources: []
        onNewData: function(source, data) {
            disconnectSource(source);
            root.openingDashboard = false;
            if (data["exit status"] !== 0 || data["exit code"] !== 0) Qt.openUrlExternally(root.dashboardUrl);
        }
    }

    function refreshView() {
        view = RouterStatus.describe(observation, Date.now(), language, dashboardUrl);
    }
    onLanguageChanged: refreshView()
    // A new address is a different dashboard: start over and check it now.
    onDashboardUrlChanged: {
        if (request) {
            var xhr = request;
            request = null;
            timeout.stop();
            xhr.onreadystatechange = function () {};
            xhr.abort();
        }
        observation = RouterStatus.initial(Date.now());
        refreshView();
        poll();
    }

    function finish(data) {
        timeout.stop();
        request = null;
        observation = RouterStatus.receive(observation, data, Date.now());
        refreshView();
    }

    function poll() {
        if (request !== null) return;
        var xhr = new XMLHttpRequest();
        request = xhr;
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== XMLHttpRequest.DONE || request !== xhr) return;
            var data = null;
            if (xhr.status === 200) {
                try {
                    var parsed = JSON.parse(xhr.responseText);
                    if (RouterStatus.validPayload(parsed)) data = parsed;
                } catch (error) { /* Invalid replies count as failed checks. */ }
            }
            finish(data);
        };
        xhr.open("GET", dashboardUrl + "/api/health");
        xhr.setRequestHeader("X-Router-Request", "1");
        timeout.start();
        xhr.send();
    }

    Connections {
        target: Plasmoid
        function onActivated() { root.openDashboard(); }
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: RouterStatus.text(root.language, "open")
            icon.name: "globe"
            onTriggered: root.openDashboard()
        },
        PlasmaCore.Action {
            text: RouterStatus.text(root.language, "copy")
            icon.name: "edit-copy"
            onTriggered: {
                clipboard.selectAll();
                clipboard.copy();
                clipboard.deselect();
            }
        },
        PlasmaCore.Action {
            // Saved in the widget's configuration; replaces following the system language.
            text: RouterStatus.text(root.language, "switchLanguage")
            icon.name: "languages"
            onTriggered: Plasmoid.configuration.language = RouterStatus.otherLanguage(root.language)
        }
    ]

    // TextEdit.copy uses Qt's native clipboard without a shell or an extra service.
    TextEdit {
        id: clipboard
        visible: false
        text: root.dashboardUrl
    }

    Timer {
        id: timeout
        interval: 5000
        onTriggered: {
            var xhr = root.request;
            root.request = null;
            if (xhr) xhr.abort();
            root.finish(null);
        }
    }
    Timer {
        interval: 30000
        running: true
        repeat: true
        onTriggered: root.poll()
    }
    Component.onCompleted: poll()
    Component.onDestruction: {
        if (request) {
            request.onreadystatechange = function () {};
            request.abort();
        }
    }

    compactRepresentation: MouseArea {
        implicitWidth: Kirigami.Units.iconSizes.smallMedium
        implicitHeight: Kirigami.Units.iconSizes.smallMedium
        acceptedButtons: Qt.LeftButton
        onClicked: mouse => { if (mouse.button === Qt.LeftButton) root.openDashboard(); }
        Kirigami.Icon {
            anchors.fill: parent
            source: Qt.resolvedUrl("../icons/router-" + root.view.kind + "-" + (width < 22 ? "16" : "22") + ".svg")
            smooth: false
        }
    }
}
