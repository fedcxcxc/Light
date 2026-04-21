from __future__ import annotations

import os
import shutil
import subprocess
import time
from multiprocessing.synchronize import Event as EventType
from queue import Full

import cv2
import numpy as np

from backend.camera_config import CameraConfig, PROCESS_TOPOLOGY
from backend.shared_memory_ring import (
    FULL_RES_HEIGHT,
    FULL_RES_WIDTH,
    SMALL_RES_HEIGHT,
    SMALL_RES_WIDTH,
    ShelfSharedMemory,
    build_shelf_memory_layout,
)


class StreamWorker:
    """单 RTSP 源 FFmpeg 解码广播生产者。

    - 只启动一次 FFmpeg 拉取 RTSP
    - 从 stdout 读取 rawvideo 帧
    - 把同一帧广播到 5 层共享内存、每层 10 个槽位
    """

    def __init__(self, shutdown_event: EventType, status_queue, camera_configs: list[CameraConfig]):
        self.shutdown_event = shutdown_event
        self.status_queue = status_queue
        self.camera_configs = camera_configs
        self._frame_id = 0
        self._process: subprocess.Popen | None = None
        self._memories: dict[int, ShelfSharedMemory] = {}
        self._source_url = self._resolve_source_url(camera_configs)
        self._frame_size = FULL_RES_WIDTH * FULL_RES_HEIGHT * 3

    def run(self):
        self._publish_status(
            "starting",
            f"single rtsp stream worker is starting with ffmpeg qsv decode: {self._source_url}",
        )
        self._create_all_shelf_memories()
        self._process = self._open_ffmpeg_process(self._source_url)

        try:
            while not self.shutdown_event.is_set():
                full_frame_rgb = self._read_full_frame_rgb()
                small_frame_rgb = cv2.resize(
                    full_frame_rgb,
                    (SMALL_RES_WIDTH, SMALL_RES_HEIGHT),
                    interpolation=cv2.INTER_AREA,
                )
                self._frame_id += 1
                timestamp_ms = int(time.time() * 1000)
                self._broadcast_frame(full_frame_rgb, small_frame_rgb, timestamp_ms)

                if self._frame_id % 15 == 0:
                    self._publish_status(
                        "info",
                        f"单 RTSP 源已通过 FFmpeg 广播第 {self._frame_id} 帧到 5 层共享内存",
                        result="FRAME_READY",
                    )
                time.sleep(0.01)
        finally:
            self._close_ffmpeg_process()
            for memory in self._memories.values():
                memory.close()
                memory.unlink()
            self._publish_status("stopped", "single rtsp stream worker stopped")

    def _create_all_shelf_memories(self):
        for shelf in range(1, PROCESS_TOPOLOGY.shelf_count + 1):
            self._memories[shelf] = ShelfSharedMemory(
                layout=build_shelf_memory_layout(shelf),
                create=True,
            )

    @staticmethod
    def _resolve_source_url(camera_configs: list[CameraConfig]) -> str:
        if not camera_configs:
            raise RuntimeError("未提供 RTSP 摄像头配置")
        source_url = camera_configs[0].source.strip()
        if not source_url:
            raise RuntimeError("RTSP 地址为空")
        return source_url

    def _open_ffmpeg_process(self, source_url: str):
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("未找到 ffmpeg，可先安装并加入 PATH")

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
            source_url,
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
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=self._frame_size * 2,
        )

    def _close_ffmpeg_process(self):
        process = self._process
        self._process = None
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

    def _read_full_frame_rgb(self):
        if self._process is None or self._process.stdout is None:
            return self._build_waiting_frame("FFmpeg 未启动")
        if self._process.poll() is not None:
            return self._build_waiting_frame("FFmpeg 已退出")

        raw = self._process.stdout.read(self._frame_size)
        if raw is None or len(raw) != self._frame_size:
            return self._build_waiting_frame("FFmpeg 读帧失败")

        frame_bgr = np.frombuffer(raw, dtype=np.uint8).reshape((FULL_RES_HEIGHT, FULL_RES_WIDTH, 3))
        return cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    def _build_waiting_frame(self, message: str):
        frame = self._blank_frame()
        cv2.putText(frame, "Shared RTSP Source", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 3)
        cv2.putText(frame, message, (80, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (180, 220, 255), 2)
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    @staticmethod
    def _blank_frame():
        frame = cv2.UMat(FULL_RES_HEIGHT, FULL_RES_WIDTH, cv2.CV_8UC3).get()
        frame[:, :, 0] = 15
        frame[:, :, 1] = 28
        frame[:, :, 2] = 44
        return frame

    def _broadcast_frame(self, full_frame_rgb, small_frame_rgb, timestamp_ms: int):
        for shelf, memory in self._memories.items():
            for slot_index in range(PROCESS_TOPOLOGY.cameras_per_shelf):
                memory.write_camera_frame(
                    slot_index=slot_index,
                    full_frame=full_frame_rgb,
                    small_frame=small_frame_rgb,
                    frame_id=self._frame_id,
                    timestamp_ms=timestamp_ms,
                    online=True,
                    sync_flag=shelf,
                )

    def _publish_status(
        self,
        stage: str,
        message: str,
        result: str | None = None,
    ):
        payload = {
            "process_type": "stream",
            "group_id": 0,
            "pid": os.getpid(),
            "stage": stage,
            "message": message,
            "camera_ids": [camera.camera_id for camera in self.camera_configs] or ["RTSP-DEMO-01"],
            "camera_id": self.camera_configs[0].camera_id if self.camera_configs else "RTSP-DEMO-01",
            "frame_timestamp": time.time(),
            "result": result,
            "timestamp": time.time(),
        }
        try:
            self.status_queue.put_nowait(payload)
        except Full:
            pass


def run_stream_worker(shutdown_event: EventType, status_queue, camera_configs: list[CameraConfig]):
    """多进程入口：启动单 RTSP 源 FFmpeg 解码广播生产者。"""
    worker = StreamWorker(
        shutdown_event=shutdown_event,
        status_queue=status_queue,
        camera_configs=camera_configs,
    )
    worker.run()
