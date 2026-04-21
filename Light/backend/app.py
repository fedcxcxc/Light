import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QDateTime,
    QModelIndex,
    QObject,
    QRectF,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtQuick import QQuickPaintedItem
from PySide6.QtQuickControls2 import QQuickStyle

# 统一读取项目中的逻辑相机配置与系统拓扑参数。
# CAMERA_CONFIGS 提供 50 个视频格子的基础信息，PROCESS_TOPOLOGY 提供层数、每层路数等运行时常量。
from backend.camera_config import ACTIVE_RTSP_BACKEND, ACTIVE_SOURCE_MODE, CAMERA_CONFIGS, PROCESS_TOPOLOGY

# 共享内存访问入口：
# - ShelfSharedMemory：附加并访问某一层的 Full/Small/Meta 三块共享内存
# - build_shelf_memory_layout：根据层号生成该层的共享内存布局定义
from backend.shared_memory_ring import ShelfSharedMemory, build_shelf_memory_layout

# 左侧小窗统一使用的缩略帧尺寸。
# UI 里 50 个小窗都按这个尺寸缓存和绘制，避免在 paint 阶段做重复缩放。
TILE_FRAME_WIDTH = 320
TILE_FRAME_HEIGHT = 180


class CameraListModel(QAbstractListModel):
    # 提供给 QML delegate 使用的模型角色。
    # 每个视频格子会通过这些 role 读取自己的层号、位置和展示名称。
    ShelfRole = Qt.UserRole + 1
    PositionRole = Qt.UserRole + 2
    CameraNameRole = Qt.UserRole + 3
    CameraImageRole = Qt.UserRole + 4

    def __init__(self, camera_configs=None):
        super().__init__()

        # _items 是前端直接消费的扁平列表；
        # 每个元素对应一个视频窗口，不直接暴露完整 CameraConfig 给 QML。
        self._items = []

        configs = camera_configs or CAMERA_CONFIGS

        # 根据配置文件构造摄像头位列表，前端宫格只依赖这份统一配置。
        for camera in configs:
            # 被禁用的逻辑相机位不进入 UI 模型。
            if not camera.enabled:
                continue
            self._items.append(
                {
                    "shelf": camera.shelf,
                    "position": camera.position,
                    "cameraName": camera.display_name or camera.camera_id,
                    "cameraImage": camera.camera_id,
                }
            )

    def rowCount(self, parent=QModelIndex()):
        # 平面列表模型：只有顶层有行，子节点查询直接返回 0。
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        # 按行号与角色返回对应字段，供 QML delegate 绑定。
        if not index.isValid():
            return None

        item = self._items[index.row()]
        if role == self.ShelfRole:
            return item["shelf"]
        if role == self.PositionRole:
            return item["position"]
        if role == self.CameraNameRole:
            return item["cameraName"]
        if role == self.CameraImageRole:
            return item["cameraImage"]
        return None

    def roleNames(self):
        return {
            self.ShelfRole: b"shelf",
            self.PositionRole: b"position",
            self.CameraNameRole: b"cameraName",
            self.CameraImageRole: b"cameraImage",
        }


class RuntimeLogModel(QAbstractListModel):
    # 右侧日志面板使用的角色定义。
    # 一条日志会被拆成时间、级别、来源、消息、相机号、帧时间戳、结果等字段。
    TimestampRole = Qt.UserRole + 1
    LevelRole = Qt.UserRole + 2
    SourceRole = Qt.UserRole + 3
    MessageRole = Qt.UserRole + 4
    CameraIdRole = Qt.UserRole + 5
    FrameTimestampRole = Qt.UserRole + 6
    ResultRole = Qt.UserRole + 7

    def __init__(self, max_entries: int = 200):
        super().__init__()

        # _items 存放已经格式化好的日志文本，方便 QML 直接展示。
        self._items: list[dict[str, str]] = []

        # 限制日志最大保留条数，避免长时间运行后列表无限增长。
        self._max_entries = max_entries

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        item = self._items[index.row()]
        if role == self.TimestampRole:
            return item["timestamp"]
        if role == self.LevelRole:
            return item["level"]
        if role == self.SourceRole:
            return item["source"]
        if role == self.MessageRole:
            return item["message"]
        if role == self.CameraIdRole:
            return item["cameraId"]
        if role == self.FrameTimestampRole:
            return item["frameTimestamp"]
        if role == self.ResultRole:
            return item["result"]
        return None

    def roleNames(self):
        return {
            self.TimestampRole: b"timestamp",
            self.LevelRole: b"level",
            self.SourceRole: b"source",
            self.MessageRole: b"message",
            self.CameraIdRole: b"cameraId",
            self.FrameTimestampRole: b"frameTimestamp",
            self.ResultRole: b"result",
        }

    @Slot(str, str, str, str, str, str, float)
    def append_log_entry(
        self,
        source: str,
        level: str,
        message: str,
        camera_id: str,
        frame_timestamp: str,
        result: str,
        timestamp: float = 0.0,
    ):
        # 后台线程与运行时桥最终都会把日志投递到这里。
        # 这里负责做时间格式化、字段兜底和模型插入通知。
        if timestamp > 0:
            time_text = QDateTime.fromMSecsSinceEpoch(int(timestamp * 1000)).toString("HH:mm:ss")
        else:
            time_text = QDateTime.currentDateTime().toString("HH:mm:ss")

        item = {
            "timestamp": time_text,
            "level": (level or "INFO").upper(),
            "source": source or "SYSTEM",
            "message": message or "-",
            "cameraId": camera_id or "-",
            "frameTimestamp": frame_timestamp or "-",
            "result": result or "-",
        }

        # 通过标准 Qt begin/end 接口增量插入一条新日志。
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self.endInsertRows()

        # 超出上限时从头部移除旧日志，保证新日志持续可见。
        overflow = len(self._items) - self._max_entries
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            del self._items[:overflow]
            self.endRemoveRows()


class RuntimeLogBridge(QObject):
    # 后台线程或运行时总控通过该 signal 把日志投递回 UI 线程。
    logReceived = Signal(str, str, str, str, str, str, float)

    def __init__(self, log_model: RuntimeLogModel):
        super().__init__()
        self.logReceived.connect(log_model.append_log_entry, Qt.ConnectionType.QueuedConnection)

    def emit_payload(self, payload: dict):
        source = self._build_source(payload)
        level = self._resolve_level(payload)
        message = payload.get("message", "")
        camera_id = payload.get("camera_id") or ""
        frame_timestamp = self._format_frame_timestamp(payload.get("frame_timestamp"))
        result = payload.get("result") or ""
        timestamp = float(payload.get("timestamp") or 0.0)
        self.logReceived.emit(source, level, message, camera_id, frame_timestamp, result, timestamp)

    def emit_text(
        self,
        source: str,
        level: str,
        message: str,
        timestamp: float | None = None,
        camera_id: str = "",
        frame_timestamp: float | None = None,
        result: str = "",
    ):
        formatted_frame_timestamp = self._format_frame_timestamp(frame_timestamp)
        self.logReceived.emit(
            source,
            level.upper(),
            message,
            camera_id,
            formatted_frame_timestamp,
            result,
            float(timestamp or time.time()),
        )

    @staticmethod
    def _resolve_level(payload: dict) -> str:
        stage = str(payload.get("stage", "info")).lower()
        if stage in {"error", "failed", "fatal"}:
            return "ERROR"
        if stage in {"warn", "warning"}:
            return "WARN"
        return "INFO"

    @staticmethod
    def _format_frame_timestamp(frame_timestamp) -> str:
        if not frame_timestamp:
            return ""
        return QDateTime.fromMSecsSinceEpoch(int(float(frame_timestamp) * 1000)).toString("HH:mm:ss.zzz")

    @staticmethod
    def _build_source(payload: dict) -> str:
        process_type = str(payload.get("process_type", "runtime")).upper()
        group_id = payload.get("group_id")
        if process_type == "RUNTIME":
            return "RUNTIME"
        if group_id is None:
            return process_type
        return f"{process_type}-G{int(group_id):02d}"


class SharedFrameController(QObject):
    # 共享内存新帧刷新后通知所有绑定控件重绘。
    frameChanged = Signal()

    def __init__(self):
        super().__init__()
        self._memories: dict[int, ShelfSharedMemory] = {}
        self._pending_shelves = set(range(1, PROCESS_TOPOLOGY.shelf_count + 1))
        self._last_frame_ids: dict[tuple[int, int], int] = {}
        self._tile_frames: dict[tuple[int, int], QImage] = {}
        self._placeholder = self._build_waiting_frame("正在等待共享内存画面...")

        # 第一个定时器负责非阻塞地尝试附加尚未就绪的共享内存层。
        self._attach_timer = QTimer(self)
        self._attach_timer.timeout.connect(self._try_attach_pending_memories)
        self._attach_timer.start(250)

        # 第二个定时器只负责刷新已经成功附加的共享内存画面。
        # 约 40ms 一次，目标是接近 25fps 的 UI 预览刷新节奏。
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_from_shared_memory)
        self._refresh_timer.start(40)

    def get_tile_frame(self, shelf: int, position: int) -> QImage:
        return self._tile_frames.get((shelf, position), self._placeholder)

    def stop(self):
        if self._attach_timer.isActive():
            self._attach_timer.stop()
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
        for memory in self._memories.values():
            memory.close()

    def _try_attach_pending_memories(self):
        attached_any = False
        for shelf in list(self._pending_shelves):
            try:
                self._memories[shelf] = ShelfSharedMemory(
                    layout=build_shelf_memory_layout(shelf),
                    create=False,
                )
            except FileNotFoundError:
                continue
            self._pending_shelves.remove(shelf)
            attached_any = True

        if not self._pending_shelves and self._attach_timer.isActive():
            self._attach_timer.stop()

        if attached_any:
            self.frameChanged.emit()

    def _refresh_from_shared_memory(self):
        changed = False
        for shelf, memory in self._memories.items():
            for position in range(1, PROCESS_TOPOLOGY.cameras_per_shelf + 1):
                slot_index = position - 1
                meta = memory.read_camera_meta(slot_index)
                frame_id = int(meta["frame_id"])
                if frame_id == 0:
                    continue
                key = (shelf, position)
                if self._last_frame_ids.get(key) == frame_id:
                    continue

                small_frame = memory.read_small_frame(slot_index)
                self._tile_frames[key] = self._numpy_rgb_to_qimage(small_frame)
                self._last_frame_ids[key] = frame_id
                changed = True

        if changed:
            self.frameChanged.emit()

    @staticmethod
    def _numpy_rgb_to_qimage(frame: np.ndarray) -> QImage:
        height, width, channels = frame.shape
        bytes_per_line = width * channels
        return QImage(frame.data, width, height, bytes_per_line, QImage.Format.Format_RGB888).copy()

    @staticmethod
    def _build_waiting_frame(message: str):
        image = QImage(TILE_FRAME_WIDTH, TILE_FRAME_HEIGHT, QImage.Format.Format_RGB32)
        image.fill(QColor("#08111f"))

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#1e3a5f"), 2))
        painter.drawRoundedRect(4, 4, TILE_FRAME_WIDTH - 8, TILE_FRAME_HEIGHT - 8, 10, 10)

        title_font = QFont("Segoe UI", 10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#eff6ff"))
        painter.drawText(12, 50, "Shared Memory Preview")

        body_font = QFont("Segoe UI", 8)
        painter.setFont(body_font)
        painter.setPen(QColor("#9fb3ca"))
        painter.drawText(12, 80, message)
        painter.end()
        return image


class SharedMemoryFrameItem(QQuickPaintedItem):
    # 当前绑定的控制器或相机槽位变化后，通知 QML 属性系统。
    controllerChanged = Signal()
    shelfChanged = Signal()
    positionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controller = None
        self._shelf = 1
        self._position = 1

        self.setRenderTarget(QQuickPaintedItem.RenderTarget.FramebufferObject)
        self.setAntialiasing(False)

    def get_controller(self):
        return self._controller

    def set_controller(self, controller):
        if self._controller is controller:
            return
        if self._controller is not None:
            try:
                self._controller.frameChanged.disconnect(self.update)
            except (RuntimeError, TypeError):
                pass

        self._controller = controller
        if self._controller is not None:
            self._controller.frameChanged.connect(self.update)
        self.controllerChanged.emit()
        self.update()

    def get_shelf(self):
        return self._shelf

    def set_shelf(self, shelf: int):
        if self._shelf == shelf:
            return
        self._shelf = shelf
        self.shelfChanged.emit()
        self.update()

    def get_position(self):
        return self._position

    def set_position(self, position: int):
        if self._position == position:
            return
        self._position = position
        self.positionChanged.emit()
        self.update()

    controller = Property(QObject, get_controller, set_controller, notify=controllerChanged)
    shelf = Property(int, get_shelf, set_shelf, notify=shelfChanged)
    position = Property(int, get_position, set_position, notify=positionChanged)

    def paint(self, painter: QPainter):
        if self.width() <= 0 or self.height() <= 0:
            return

        painter.fillRect(QRectF(0, 0, self.width(), self.height()), QColor("#060b14"))
        if self._controller is None:
            return

        image = self._controller.get_tile_frame(self._shelf, self._position)
        if image.isNull():
            return

        scaled = image.scaled(
            int(self.width()),
            int(self.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) / 2
        y = (self.height() - scaled.height()) / 2
        painter.drawImage(QRectF(x, y, scaled.width(), scaled.height()), scaled)


# 向 QML 注册自定义视频绘制控件。
qmlRegisterType(SharedMemoryFrameItem, "LightComponents", 1, 0, "SharedMemoryFrameItem")


def main(supervisor=None):
    # 使用可自定义的非原生控件风格，避免原生 style 的定制限制。
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(sys.argv)

    engine = QQmlApplicationEngine()
    project_dir = Path(__file__).resolve().parent.parent
    qml_file = project_dir / "frontend" / "main.qml"

    # 准备前端需要的核心上下文对象：摄像头位模型 + 共享内存预览控制器 + 日志模型。
    camera_model = CameraListModel()
    shared_frame_controller = SharedFrameController()
    runtime_log_model = RuntimeLogModel()
    runtime_log_bridge = RuntimeLogBridge(runtime_log_model)

    runtime_source_mode = ACTIVE_SOURCE_MODE
    rtsp_backend = ACTIVE_RTSP_BACKEND

    runtime_log_bridge.emit_text(
        "UI",
        "INFO",
        f"前端界面初始化完成，当前 source_mode={runtime_source_mode}, rtsp_backend={rtsp_backend}",
    )
    if supervisor is not None:
        supervisor.set_log_bridge(runtime_log_bridge)

    engine.rootContext().setContextProperty("cameraModel", camera_model)
    engine.rootContext().setContextProperty("sharedFrameController", shared_frame_controller)
    engine.rootContext().setContextProperty("runtimeLogModel", runtime_log_model)
    engine.rootContext().setContextProperty("runtimeSourceMode", runtime_source_mode)
    engine.rootContext().setContextProperty("rtspBackend", rtsp_backend)
    engine.load(qml_file)
    if not engine.rootObjects():
        shared_frame_controller.stop()
        sys.exit(-1)

    # 应用退出时，确保共享内存读取定时器和句柄都被正确回收。
    exit_code = app.exec()
    shared_frame_controller.stop()
    sys.exit(exit_code)
