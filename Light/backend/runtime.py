from __future__ import annotations

import signal
import threading
import time
from multiprocessing import Process, get_context
from queue import Empty
from typing import Any

from backend import app as ui_app
from backend.camera_config import ACTIVE_RTSP_CONFIG_MODE, ACTIVE_SOURCE_MODE, CAMERA_CONFIGS, CameraConfig
from backend.ocr_worker import run_ocr_worker
from backend.rtsp_producer import run_rtsp_shelf_producer
from backend.stream_worker import run_stream_worker
from backend.webcam_producer import run_webcam_producer


def _group_camera_configs(camera_configs: list[CameraConfig], attribute_name: str) -> dict[int, list[CameraConfig]]:
    """按照进程分组字段对相机配置进行归类。"""
    grouped: dict[int, list[CameraConfig]] = {}
    for camera in camera_configs:
        if not camera.enabled:
            continue
        group_id = getattr(camera, attribute_name)
        grouped.setdefault(group_id, []).append(camera)
    return grouped


class RuntimeSupervisor:
    """运行时总调度。

    当前支持两种输入模式：
    1. webcam：1 个本机摄像头生产者 + 5 个 OCR + 1 个 UI
    2. rtsp：默认使用 `rtsp_producer.py` 的分层 RTSP 生产者 + 5 个 OCR + 1 个 UI

    如果 `ACTIVE_RTSP_CONFIG_MODE` 设为 `single_demo`，则可以切到单 RTSP 广播流验证入口；
    如果设为 `layered_template`，则走 `rtsp_producer.py` 的按层生产者架构。
    """

    GRACEFUL_SHUTDOWN_TIMEOUT = 1.2
    FORCE_SHUTDOWN_TIMEOUT = 0.5
    SHUTDOWN_POLL_INTERVAL = 0.05

    def __init__(self, camera_configs: list[CameraConfig] | None = None):
        self.camera_configs = camera_configs or CAMERA_CONFIGS
        self.context = get_context("spawn")
        self.shutdown_event = self.context.Event()
        self.status_queue = self.context.Queue()
        self.processes: list[Process] = []
        self.latest_status_by_worker: dict[tuple[str, int], dict[str, Any]] = {}
        self._status_thread = threading.Thread(target=self._drain_status_queue, daemon=True)
        self._log_bridge = None
        self._stop_lock = threading.Lock()
        self._stopped = False

    def run(self) -> int:
        self._install_signal_handlers()
        self._start_worker_processes()
        self._status_thread.start()

        exit_code = 0
        try:
            ui_app.main(self)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        finally:
            self.stop()
        return exit_code

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

        self._emit_runtime_log("WARN", "收到停止指令，准备快速关闭全部子进程")
        self.shutdown_event.set()

        alive_processes = [process for process in self.processes if process.is_alive()]
        if not alive_processes:
            return

        graceful_deadline = time.monotonic() + self.GRACEFUL_SHUTDOWN_TIMEOUT
        while time.monotonic() < graceful_deadline:
            alive_processes = [process for process in alive_processes if process.is_alive()]
            if not alive_processes:
                return
            time.sleep(self.SHUTDOWN_POLL_INTERVAL)

        for process in alive_processes:
            process.terminate()
            self._emit_runtime_log("WARN", f"{process.name} 未在宽限期内退出，已执行强制终止")

        force_deadline = time.monotonic() + self.FORCE_SHUTDOWN_TIMEOUT
        while time.monotonic() < force_deadline:
            alive_processes = [process for process in alive_processes if process.is_alive()]
            if not alive_processes:
                return
            time.sleep(self.SHUTDOWN_POLL_INTERVAL)

        for process in alive_processes:
            if process.is_alive():
                self._emit_runtime_log("ERROR", f"{process.name} 在强制终止后仍未退出")

    def set_log_bridge(self, log_bridge):
        self._log_bridge = log_bridge
        self._emit_runtime_log("INFO", "运行时日志桥已接入前端面板")

    def _start_worker_processes(self):
        # 根据当前模式启动不同的生产者拓扑。
        if ACTIVE_SOURCE_MODE == "rtsp":
            self._start_rtsp_producers()
        else:
            self._start_webcam_producer()

        # OCR 进程不需要区分输入模式；
        # 它们始终只认“每层共享内存”这一统一协议。
        ocr_groups = _group_camera_configs(self.camera_configs, "ocr_process_group")
        for group_id, group_cameras in sorted(ocr_groups.items()):
            process = self.context.Process(
                target=run_ocr_worker,
                name=f"ocr-worker-{group_id}",
                args=(group_id, group_cameras, self.shutdown_event, self.status_queue),
            )
            process.start()
            self.processes.append(process)
            self._emit_runtime_log(
                "INFO",
                f"已启动 OCR 进程组 G{group_id:02d}，按层读取 {len(group_cameras)} 路共享图像",
            )

    def _start_webcam_producer(self):
        producer = self.context.Process(
            target=run_webcam_producer,
            name="single-webcam-producer",
            args=(self.shutdown_event, self.status_queue, 0),
        )
        producer.start()
        self.processes.append(producer)
        self._emit_runtime_log("INFO", "当前处于 webcam 模式：已启动单一摄像头生产者进程")

    def _start_rtsp_producers(self):
        if ACTIVE_RTSP_CONFIG_MODE == "single_demo":
            producer = self.context.Process(
                target=run_stream_worker,
                name="single-rtsp-stream-worker",
                args=(self.shutdown_event, self.status_queue, self.camera_configs),
            )
            producer.start()
            self.processes.append(producer)
            self._emit_runtime_log("INFO", "当前处于 rtsp 模式：已启动单 RTSP 源广播生产者进程")
            return

        self._start_layered_rtsp_producers()

    def _start_layered_rtsp_producers(self):
        # 原有真实 50 路 RTSP 分层入口仍保留，后续切回真实多路时可直接复用。
        stream_groups = _group_camera_configs(self.camera_configs, "stream_process_group")
        for group_id, group_cameras in sorted(stream_groups.items()):
            process = self.context.Process(
                target=run_rtsp_shelf_producer,
                name=f"rtsp-shelf-producer-{group_id}",
                args=(group_id, group_cameras, self.shutdown_event, self.status_queue),
            )
            process.start()
            self.processes.append(process)
            print(f"当前处于 rtsp 模式：已启动第 {group_id} 层 RTSP 生产者，负责 {len(group_cameras)} 路流")
            self._emit_runtime_log(
                "INFO",
                f"当前处于 rtsp 模式：已启动第 {group_id} 层 RTSP 生产者，负责 {len(group_cameras)} 路流",
            )

    def _drain_status_queue(self):
        while not self.shutdown_event.is_set():
            try:
                payload = self.status_queue.get(timeout=0.5)
            except Empty:
                continue

            worker_key = (payload["process_type"], payload["group_id"])
            self.latest_status_by_worker[worker_key] = payload
            self._forward_payload_to_log(payload)

    def _forward_payload_to_log(self, payload: dict[str, Any]):
        if self._log_bridge is None:
            return
        self._log_bridge.emit_payload(payload)

    def _emit_runtime_log(self, level: str, message: str):
        if self._log_bridge is None:
            return
        self._log_bridge.emit_text("RUNTIME", level, message, time.time())

    def _install_signal_handlers(self):
        def _handle_shutdown_signal(signum, _frame):
            _ = signum
            self.shutdown_event.set()
            self._emit_runtime_log("WARN", "主进程捕获到系统退出信号，开始停机")

        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handle_shutdown_signal)


def main() -> int:
    supervisor = RuntimeSupervisor()
    return supervisor.run()
