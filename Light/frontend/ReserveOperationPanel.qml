pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    radius: 24
    color: "#0d1525"
    border.width: 1
    border.color: "#243650"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 18

        Label {
            text: "右侧操作与日志"
            color: "#eff6ff"
            font.pixelSize: 26
            font.bold: true
        }

        Label {
            text: "上方保留操作位，下方改为日志输出区，便于观察摄像头轮询、OCR 调度与异常状态。"
            color: "#8ea4c0"
            font.pixelSize: 14
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 170
            radius: 20
            color: "#101c31"
            border.width: 1
            border.color: "#2d4568"

            Column {
                id: reserveCardContent
                anchors.centerIn: parent
                spacing: 8

                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "操作区预留"
                    color: "#7dd3fc"
                    font.pixelSize: 24
                    font.bold: true
                }

                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Buttons / Filters / Status"
                    color: "#cbd5e1"
                    font.pixelSize: 14
                }
            }
        }

        Item { Layout.fillHeight: true }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 320
            radius: 18
            color: "#0a1322"
            border.width: 1
            border.color: "#28405e"

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 12

                RowLayout {
                    Layout.fillWidth: true

                    Label {
                        text: "日志输出"
                        color: "#f8fafc"
                        font.pixelSize: 16
                        font.bold: true
                    }

                    Item { Layout.fillWidth: true }

                    Rectangle {
                        radius: 10
                        color: "#12304a"
                        border.width: 1
                        border.color: "#2f5b86"
                        Layout.preferredHeight: 24
                        Layout.preferredWidth: 62

                        Label {
                            anchors.centerIn: parent
                            text: "LIVE"
                            color: "#7dd3fc"
                            font.pixelSize: 11
                            font.bold: true
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 14
                    color: "#050b14"
                    border.width: 1
                    border.color: "#16304e"

                    ScrollView {
                        anchors.fill: parent
                        anchors.margins: 1
                        clip: true

                        ListView {
                            id: logListView
                            model: runtimeLogModel
                            spacing: 8
                            clip: true

                            onCountChanged: if (count > 0) positionViewAtEnd()
                            delegate: Rectangle {
                                required property string level
                                required property string timestamp
                                required property string source
                                required property string message
                                required property string cameraId
                                required property string frameTimestamp
                                required property string result

                                width: logListView.width
                                height: logColumn.implicitHeight + 14
                                radius: 10
                                color: "#08101c"
                                border.width: 1
                                border.color: level === "ERROR" ? "#7f1d1d" : (level === "WARN" ? "#7c5b16" : "#18354f")

                                Column {
                                    id: logColumn
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.leftMargin: 12
                                    anchors.rightMargin: 12
                                    spacing: 6

                                    Row {
                                        spacing: 8

                                        Label {
                                            text: timestamp
                                            color: "#7f93ad"
                                            font.pixelSize: 12
                                            font.family: "Consolas"
                                        }

                                        Label {
                                            text: source
                                            color: "#93c5fd"
                                            font.pixelSize: 12
                                            font.bold: true
                                            font.family: "Consolas"
                                        }

                                        Label {
                                            text: level
                                            color: level === "ERROR" ? "#fca5a5" : (level === "WARN" ? "#fcd34d" : "#7dd3fc")
                                            font.pixelSize: 12
                                            font.bold: true
                                            font.family: "Consolas"
                                        }
                                    }

                                    Row {
                                        spacing: 12
                                        visible: cameraId !== "-" || frameTimestamp !== "-" || result !== "-"

                                        Label {
                                            text: "CAM " + cameraId
                                            visible: cameraId !== "-"
                                            color: "#c4b5fd"
                                            font.pixelSize: 12
                                            font.family: "Consolas"
                                        }

                                        Label {
                                            text: "FRAME " + frameTimestamp
                                            visible: frameTimestamp !== "-"
                                            color: "#86efac"
                                            font.pixelSize: 12
                                            font.family: "Consolas"
                                        }

                                        Label {
                                            text: "RESULT " + result
                                            visible: result !== "-"
                                            color: result === "NG" ? "#fda4af" : (result === "OK" ? "#7dd3fc" : "#facc15")
                                            font.pixelSize: 12
                                            font.bold: true
                                            font.family: "Consolas"
                                        }
                                    }

                                    Label {
                                        text: message
                                        color: "#d7e3f4"
                                        font.pixelSize: 13
                                        wrapMode: Text.WordWrap
                                        width: parent.width
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
