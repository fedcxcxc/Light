pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

GridView {
    id: cameraGrid

    clip: true
    interactive: false
    model: cameraModel
    cellWidth: Math.floor((width - 9 * 10) / 10)
    cellHeight: Math.floor((height - 4 * 10) / 5)
    boundsBehavior: Flickable.StopAtBounds

    delegate: CameraTile {}
}
