pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: monitorPanel

    property int columns: 10
    property int rows: 5
    property int gridSpacing: 6
    property real tileWidth: (cameraGridFrame.width - gridSpacing * (columns - 1)) / columns
    property real tileHeight: (cameraGridFrame.height - gridSpacing * (rows - 1)) / rows

    radius: 14
    color: "#09111d"
    border.width: 1
    border.color: "#18283f"

    Item {
        id: cameraGridFrame
        anchors.fill: parent
        anchors.margins: 4

        Grid {
            id: cameraGrid
            anchors.centerIn: parent
            width: monitorPanel.columns * monitorPanel.tileWidth + (monitorPanel.columns - 1) * monitorPanel.gridSpacing
            height: monitorPanel.rows * monitorPanel.tileHeight + (monitorPanel.rows - 1) * monitorPanel.gridSpacing
            columns: monitorPanel.columns
            rows: monitorPanel.rows
            spacing: monitorPanel.gridSpacing

            Repeater {
                model: cameraModel

                delegate: Item {
                    id: cameraTileDelegate
                    required property int shelf
                    required property int position
                    required property string cameraName
                    required property string cameraImage

                    width: monitorPanel.tileWidth
                    height: monitorPanel.tileHeight

                    CameraTile {
                        anchors.fill: parent
                        shelf: cameraTileDelegate.shelf
                        position: cameraTileDelegate.position
                        cameraName: cameraTileDelegate.cameraName
                        cameraImage: cameraTileDelegate.cameraImage
                    }
                }
            }
        }
    }
}
