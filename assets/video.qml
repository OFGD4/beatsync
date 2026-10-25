// The preview picture, drawn by the graphics card. Loaded by ui/player.py into a QQuickWidget;
// the play / speed / full-screen controls are normal widgets floating on top of it.
import QtQuick
import QtMultimedia

VideoOutput {
    fillMode: VideoOutput.PreserveAspectFit
}
