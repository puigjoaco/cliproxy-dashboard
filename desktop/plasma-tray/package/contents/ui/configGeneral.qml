import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import "../code/status.js" as RouterStatus

KCM.SimpleKCM {
    property alias cfg_dashboardUrl: urlField.text
    property string cfg_language
    readonly property string language: RouterStatus.language(cfg_language, Qt.locale().uiLanguages)

    Kirigami.FormLayout {
        QQC2.TextField {
            id: urlField
            Kirigami.FormData.label: RouterStatus.text(language, "urlLabel")
            placeholderText: RouterStatus.DEFAULT_URL
            inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
        }
        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: RouterStatus.normalizeUrl(urlField.text) === null
            type: Kirigami.MessageType.Warning
            text: RouterStatus.text(language, "urlInvalid")
        }
    }
}
