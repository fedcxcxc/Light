pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    property bool rightPanelCollapsed: false
    property real rightPanelExpandedWidth: 260
    property real rightPanelCollapsedWidth: 44
    property real contentSpacing: 8
    property real rightPanelWidth: rightPanelCollapsed ? rightPanelCollapsedWidth : rightPanelExpandedWidth
    property bool panelAnimating: false

    width: 1760
    height: 980
    visible: true
    title: qsTr("吸尘器质量检测大屏") + " [" + runtimeSourceMode + " | " + rtspBackend + "]"
    color: "#08111f"

    onRightPanelCollapsedChanged: {
        panelAnimating = true
        rightPanelWidthAnimation.to = rightPanelCollapsed ? rightPanelCollapsedWidth : rightPanelExpandedWidth
        rightPanelWidthAnimation.start()
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#09111f" }
            GradientStop { position: 0.55; color: "#0c1728" }
            GradientStop { position: 1.0; color: "#111c30" }
        }

        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.width: 0

            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                width: parent.width
                height: parent.height
                color: "transparent"

                Canvas {
                    id: backgroundGrid
                    anchors.fill: parent
                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.reset()
                        ctx.strokeStyle = "rgba(80, 180, 255, 0.05)"
                        ctx.lineWidth = 1
                        for (let x = 0; x < backgroundGrid.width; x += 48) {
                            ctx.beginPath()
                            ctx.moveTo(x, 0)
                            ctx.lineTo(x, backgroundGrid.height)
                            ctx.stroke()
                        }
                        for (let y = 0; y < backgroundGrid.height; y += 48) {
                            ctx.beginPath()
                            ctx.moveTo(0, y)
                            ctx.lineTo(backgroundGrid.width, y)
                            ctx.stroke()
                        }
                    }
                }
            }
        }
    }

    ShelfMonitorPanel {
        id: shelfMonitorPanel
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.right: rightPanelContainer.left
        anchors.margins: 10
        anchors.rightMargin: window.contentSpacing
        layer.enabled: window.panelAnimating
        layer.smooth: true
    }

    Item {
        id: rightPanelContainer
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 10
        width: window.rightPanelWidth
        layer.enabled: window.panelAnimating
        layer.smooth: true

        ReserveOperationPanel {
            anchors.fill: parent
            visible: !window.rightPanelCollapsed
            opacity: window.rightPanelCollapsed ? 0 : 1

            Behavior on opacity {
                NumberAnimation {
                    duration: 120
                    easing.type: Easing.OutCubic
                }
            }
        }

        Rectangle {
            anchors.fill: parent
            visible: window.rightPanelCollapsed
            radius: 22
            color: "#0d1525"
            border.width: 1
            border.color: "#243650"
        }

        Rectangle {
            id: collapseButton
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.leftMargin: window.rightPanelCollapsed ? 7 : -17
            anchors.topMargin: 12
            z: 2
            width: 34
            height: 34
            radius: 17
            color: collapseMouseArea.pressed ? "#1f3a5b" : (collapseMouseArea.containsMouse ? "#17304d" : "#13233a")
            border.width: 1
            border.color: "#33557f"

            Text {
                anchors.centerIn: parent
                text: window.rightPanelCollapsed ? ">" : "<"
                color: "#7dd3fc"
                font.pixelSize: 18
                font.bold: true
            }

            MouseArea {
                id: collapseMouseArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: window.rightPanelCollapsed = !window.rightPanelCollapsed
            }
        }
    }

    NumberAnimation {
        id: rightPanelWidthAnimation
        target: window
        property: "rightPanelWidth"
        duration: 140
        easing.type: Easing.OutCubic
        onStopped: window.panelAnimating = false
    }
}
