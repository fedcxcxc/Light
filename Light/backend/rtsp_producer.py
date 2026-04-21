from __future__ import annotations

import os
import shutil
import subprocess
import time
from multiprocessing.synchronize import Event as EventType
from queue import Full

import cv2
import numpy as np

from backend.camera_config import ACTIVE_RTSP_BACKEND, CameraConfig
from backend.shared_memory_ring import (
    FULL_RES_HEIGHT,
    FULL_RES_WIDTH,
    SMALL_RES_HEIGHT,
    SMALL_RES_WIDTH,
    ShelfSharedMemory,
    build_shelf_memory_layout,
)


class RtspShelfProducer:
    """单层 RTSP 生产者骨架。

    未来真实部署时，每一层启动一个该类实例：
    - 第 1 层 producer 管理 10 路 RTSP
    - 第 2 层 producer 管理 10 路 RTSP
    - ...
    - 第 5 层 producer 管理 10 路 RTSP

    当前阶段已经把接口、共享内存写入路径和注释补齐。
    你后续拿到真实 50 路 RTSP 后，主要只需要补齐两处：
    1. `_open_capture(camera)`：按你的方案接入 OpenCV / FFmpeg / GStreamer / NVDEC
    2. `_read_camera_frame(camera, capture)`：返回该相机的最新 BGR 帧
    """

    def __init__(
        self,
        shelf_id: int,
        camera_configs: list[CameraConfig],
        shutdown_event: EventType,
        status_queue,
    ):
        self.shelf_id = shelf_id
        self.camera_configs = camera_configs
        self.shutdown_event = shutdown_event
        self.status_queue = status_queue
        self._frame_id = 1
        self.decode_backend = ACTIVE_RTSP_BACKEND

        self.memory = ShelfSharedMemory(
            layout=build_shelf_memory_layout(shelf_id),
            create=True,
        )
        self._captures: dict[str, object] = {}
        self._last_reopen_at: dict[str, float] = {}
        self._offline_full_frame_rgb = self._build_blank_rgb_frame(FULL_RES_HEIGHT, FULL_RES_WIDTH)
        self._offline_small_frame_rgb = self._build_blank_rgb_frame(SMALL_RES_HEIGHT, SMALL_RES_WIDTH)

    def run(self):
        self._publish_status("starting", f"rtsp shelf producer G{self.shelf_id:02d} is starting")
        self._write_initial_offline_frames("RTSP 正在连接...")
        self._open_all_captures()

        try:
            while not self.shutdown_event.is_set():
                self._poll_all_cameras_once()
                time.sleep(0.03)
        finally:
            self._close_all_captures()
            self.memory.close()
            self.memory.unlink()
            self._publish_status("stopped", f"rtsp shelf producer G{self.shelf_id:02d} stopped")

    def _open_all_captures(self):
        for camera in self.camera_configs:
            try:
                self._captures[camera.camera_id] = self._open_capture(camera)
            except Exception as exc:
                self._captures[camera.camera_id] = None
                self._publish_status(
                    "warning",
                    f"{camera.camera_id} 打开 RTSP 失败: {exc}",
                    result="OPEN_FAILED",
                )

    def _write_initial_offline_frames(self, message: str):
        timestamp_ms = int(time.time() * 1000)
        for slot_index, camera in enumerate(self.camera_configs):
            full_frame_rgb = self._build_offline_frame(camera, message)
            small_frame_rgb = self._build_offline_small_frame(camera, message)
            self.memory.write_camera_frame(
                slot_index=slot_index,
                full_frame=full_frame_rgb,
                small_frame=small_frame_rgb,
                frame_id=self._frame_id,
                timestamp_ms=timestamp_ms,
                online=False,
                sync_flag=self.shelf_id,
            )

    def _close_all_captures(self):
        for capture in self._captures.values():
            self._close_capture(capture)

    def _poll_all_cameras_once(self):
        self._frame_id += 1
        timestamp_ms = int(time.time() * 1000)

        for slot_index, camera in enumerate(self.camera_configs):
            capture = self._captures.get(camera.camera_id)
            frame_bgr = self._read_camera_frame(camera, capture)

            if frame_bgr is None:
                full_frame_rgb = self._build_offline_frame(camera, "RTSP 未连接或读帧失败")
                small_frame_rgb = self._build_offline_small_frame(camera, "RTSP 未连接或读帧失败")
                online = False
            else:
                full_frame_rgb = self._prepare_full_frame_rgb(frame_bgr, camera)
                small_frame_rgb = cv2.resize(
                    full_frame_rgb,
                    (SMALL_RES_WIDTH, SMALL_RES_HEIGHT),
                    interpolation=cv2.INTER_AREA,
                )
                online = True

            self.memory.write_camera_frame(
                slot_index=slot_index,
                full_frame=full_frame_rgb,
                small_frame=small_frame_rgb,
                frame_id=self._frame_id,
                timestamp_ms=timestamp_ms,
                online=online,
                sync_flag=self.shelf_id,
            )

        if self._frame_id % 30 == 0:
            self._publish_status(
                "info",
                f"rtsp shelf producer G{self.shelf_id:02d} 已完成第 {self._frame_id} 轮共享内存写入",
                result="FRAME_READY",
            )

    def _open_capture(self, camera: CameraConfig):
        if not camera.source.strip():
            return None
        if self.decode_backend == "opencv_cpu":
            return self._open_opencv_capture(camera)
        return self._open_ffmpeg_qsv_capture(camera)

    def _read_camera_frame(self, camera: CameraConfig, capture) -> np.ndarray | None:
        if not camera.source.strip():
            return None

        if self.decode_backend == "opencv_cpu":
            frame = self._read_opencv_frame(camera, capture)
        else:
            frame = self._read_ffmpeg_qsv_frame(camera, capture)

        if frame is not None:
            return frame

        refreshed_capture = self._reopen_capture_if_needed(camera, capture)
        if refreshed_capture is capture:
            return None

        if self.decode_backend == "opencv_cpu":
            return self._read_opencv_frame(camera, refreshed_capture)
        return self._read_ffmpeg_qsv_frame(camera, refreshed_capture)

    def _close_capture(self, capture):
        if capture is None:
            return

        if isinstance(capture, dict) and capture.get("backend") == "ffmpeg_qsv":
            process = capture.get("process")
            if process is None:
                return
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
            try:
                if process.stderr is not None:
                    process.stderr.close()
            except Exception:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
            return

        if hasattr(capture, "release"):
            capture.release()

    def _open_opencv_capture(self, camera: CameraConfig):
        capture = cv2.VideoCapture(camera.source)
        if not capture.isOpened():
            raise RuntimeError("opencv VideoCapture 未打开")
        return capture

    def _read_opencv_frame(self, camera: CameraConfig, capture) -> np.ndarray | None:
        _ = camera
        if capture is None or not hasattr(capture, "isOpened") or not capture.isOpened():
            return None
        ok, frame_bgr = capture.read()
        if not ok or frame_bgr is None:
            return None
        return frame_bgr

    def _open_ffmpeg_qsv_capture(self, camera: CameraConfig):
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("未找到 ffmpeg，可先安装并加入 PATH")

        frame_size = FULL_RES_WIDTH * FULL_RES_HEIGHT * 3
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-hwaccel",
            "qsv",
            "-hwaccel_output_format",
            "qsv",
            "-i",
            camera.source,
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"hwdownload,format=nv12,scale={FULL_RES_WIDTH}:{FULL_RES_HEIGHT},format=bgr24",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=frame_size * 2,
        )
        return {
            "backend": "ffmpeg_qsv",
            "process": process,
            "frame_size": frame_size,
            "width": FULL_RES_WIDTH,
            "height": FULL_RES_HEIGHT,
        }

    def _read_ffmpeg_qsv_frame(self, camera: CameraConfig, capture) -> np.ndarray | None:
        _ = camera
        if not isinstance(capture, dict):
            return None

        process = capture.get("process")
        frame_size = int(capture.get("frame_size") or 0)
        width = int(capture.get("width") or 0)
        height = int(capture.get("height") or 0)
        if process is None or process.stdout is None or frame_size <= 0 or width <= 0 or height <= 0:
            return None
        if process.poll() is not None:
            return None

        raw = process.stdout.read(frame_size)
        if raw is None or len(raw) != frame_size:
            return None

        frame = np.frombuffer(raw, dtype=np.uint8)
        return frame.reshape((height, width, 3))

    def _reopen_capture_if_needed(self, camera: CameraConfig, capture):
        now = time.monotonic()
        last_reopen_at = self._last_reopen_at.get(camera.camera_id, 0.0)
        if now - last_reopen_at < 1.0:
            return capture

        self._last_reopen_at[camera.camera_id] = now
        self._close_capture(capture)
        try:
            refreshed_capture = self._open_capture(camera)
            self._captures[camera.camera_id] = refreshed_capture
            self._publish_status(
                "warning",
                f"{camera.camera_id} 读帧失败，已尝试重新连接 RTSP",
                result="REOPEN_RTSP",
            )
            return refreshed_capture
        except Exception as exc:
            self._captures[camera.camera_id] = None
            self._publish_status(
                "warning",
                f"{camera.camera_id} 重新连接 RTSP 失败: {exc}",
                result="REOPEN_FAILED",
            )
            return None

    def _prepare_full_frame_rgb(self, frame_bgr: np.ndarray, camera: CameraConfig) -> np.ndarray:
        resized = cv2.resize(frame_bgr, (FULL_RES_WIDTH, FULL_RES_HEIGHT), interpolation=cv2.INTER_LINEAR)
        annotated = resized.copy()
        cv2.putText(
            annotated,
            f"{camera.camera_id} RTSP Shelf {self.shelf_id:02d}",
            (80, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.8,
            (255, 255, 255),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            annotated,
            camera.source,
            (80, 190),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (180, 220, 255),
            2,
            cv2.LINE_AA,
        )
        return cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)

    def _build_offline_frame(self, camera: CameraConfig, message: str) -> np.ndarray:
        _ = camera
        _ = message
        return self._offline_full_frame_rgb

    def _build_offline_small_frame(self, camera: CameraConfig, message: str) -> np.ndarray:
        _ = camera
        _ = message
        return self._offline_small_frame_rgb

    @staticmethod
    def _build_blank_rgb_frame(height: int, width: int) -> np.ndarray:
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:, :, 0] = 44
        frame[:, :, 1] = 28
        frame[:, :, 2] = 15
        return frame

    @staticmethod
    def _draw_centered_offline_text(
        frame: np.ndarray,
        camera_text: str,
        status_text: str,
        camera_font_scale: float,
        camera_thickness: int,
        status_font_scale: float,
        status_thickness: int,
        gap: int,
    ):
        frame_height, frame_width = frame.shape[:2]
        (camera_size, camera_baseline) = cv2.getTextSize(
            camera_text,
            cv2.FONT_HERSHEY_SIMPLEX,
            camera_font_scale,
            camera_thickness,
        )
        (status_size, status_baseline) = cv2.getTextSize(
            status_text,
            cv2.FONT_HERSHEY_SIMPLEX,
            status_font_scale,
            status_thickness,
        )

        total_height = camera_size[1] + camera_baseline + gap + status_size[1] + status_baseline
        top_y = (frame_height - total_height) // 2

        camera_x = (frame_width - camera_size[0]) // 2
        camera_y = top_y + camera_size[1]
        status_x = (frame_width - status_size[0]) // 2
        status_y = camera_y + camera_baseline + gap + status_size[1]

        cv2.putText(
            frame,
            camera_text,
            (camera_x, camera_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            camera_font_scale,
            (18, 28, 40),
            camera_thickness + 2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            camera_text,
            (camera_x, camera_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            camera_font_scale,
            (236, 242, 250),
            camera_thickness,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            status_text,
            (status_x, status_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            status_font_scale,
            (18, 28, 40),
            status_thickness + 3,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            status_text,
            (status_x, status_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            status_font_scale,
            (110, 190, 255),
            status_thickness,
            cv2.LINE_AA,
        )

    def _publish_status(self, stage: str, message: str, result: str | None = None):
        payload = {
            "process_type": "stream",
            "group_id": self.shelf_id,
            "pid": os.getpid(),
            "stage": stage,
            "message": message,
            "camera_ids": [camera.camera_id for camera in self.camera_configs],
            "camera_id": None,
            "frame_timestamp": time.time(),
            "result": result,
            "timestamp": time.time(),
        }
        try:
            self.status_queue.put_nowait(payload)
        except Full:
            pass


def run_rtsp_shelf_producer(
    shelf_id: int,
    camera_configs: list[CameraConfig],
    shutdown_event: EventType,
    status_queue,
):
    producer = RtspShelfProducer(
        shelf_id=shelf_id,
        camera_configs=camera_configs,
        shutdown_event=shutdown_event,
        status_queue=status_queue,
    )
    producer.run()
