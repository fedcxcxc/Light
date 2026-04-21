pragma ComponentBehavior: Bound

import QtQuick
import LightComponents 1.0

Rectangle {
    id: cameraTile

    required property int shelf
    required property int position
    required property string cameraName
    required property string cameraImage

    radius: 10
    color: "#152742"
    border.width: 1
    border.color: "#5f8fc0"

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: "transparent"
        border.width: 1
        border.color: "#b8f2ff"
        opacity: 0.30
    }

    Rectangle {
        anchors.fill: parent
        anchors.margins: 2
        radius: 9
        color: "#0b1526"
        border.width: 1
        border.color: "#294766"
        opacity: 0.98
    }

    Rectangle {
        id: cameraPreviewFrame
        anchors.fill: parent
        anchors.margins: 5
        radius: 7
        color: "#01060d"
        border.width: 1
        border.color: "#1f3f5f"
        clip: true

        SharedMemoryFrameItem {
            anchors.fill: parent
            controller: sharedFrameController
            shelf: cameraTile.shelf
            position: cameraTile.position
        }
    }
}
