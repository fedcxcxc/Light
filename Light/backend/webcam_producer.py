from __future__ import annotations

import os
import time
from multiprocessing.synchronize import Event as EventType
from queue import Full

import cv2

from backend.camera_config import PROCESS_TOPOLOGY
from backend.shared_memory_ring import (
    FULL_RES_HEIGHT,
    FULL_RES_WIDTH,
    SMALL_RES_HEIGHT,
    SMALL_RES_WIDTH,
    ShelfSharedMemory,
    build_shelf_memory_layout,
)


class WebcamProducer:
    """单一摄像头生产者。

    只打开一次本机摄像头，然后把同一帧复制写入 5 层共享内存。
    每层再把同一帧复制到该层 10 个槽位，供 5 个 OCR 进程和 UI 分层读取。
    """

    def __init__(self, shutdown_event: EventType, status_queue, device_index: int = 0):
        self.shutdown_event = shutdown_event
        self.status_queue = status_queue
        self.device_index = device_index
        self._frame_id = 0
        self._capture = None
        self._memories: dict[int, ShelfSharedMemory] = {}

    def run(self):
        self._publish_status("starting", "single webcam producer is starting")
        self._create_all_shelf_memories()
        self._capture = self._open_capture()

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
                        f"单一摄像头源已广播第 {self._frame_id} 帧到 5 层共享内存",
                        result="FRAME_READY",
                    )
                time.sleep(0.033)
        finally:
            if self._capture is not None and self._capture.isOpened():
                self._capture.release()
            for memory in self._memories.values():
                memory.close()
                memory.unlink()
            self._publish_status("stopped", "single webcam producer stopped")

    def _create_all_shelf_memories(self):
        for shelf in range(1, PROCESS_TOPOLOGY.shelf_count + 1):
            self._memories[shelf] = ShelfSharedMemory(
                layout=build_shelf_memory_layout(shelf),
                create=True,
            )

    def _open_capture(self):
        capture = cv2.VideoCapture(self.device_index, cv2.CAP_DSHOW)
        if not capture.isOpened():
            capture = cv2.VideoCapture(self.device_index)
        if capture.isOpened():
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, FULL_RES_WIDTH)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FULL_RES_HEIGHT)
        return capture

    def _read_full_frame_rgb(self):
        if self._capture is None or not self._capture.isOpened():
            return self._build_waiting_frame("未检测到可用摄像头")

        ok, frame_bgr = self._capture.read()
        if not ok or frame_bgr is None:
            return self._build_waiting_frame("摄像头读取失败")

        resized = cv2.resize(frame_bgr, (FULL_RES_WIDTH, FULL_RES_HEIGHT), interpolation=cv2.INTER_LINEAR)
        return cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

    def _build_waiting_frame(self, message: str):
        frame = cv2.cvtColor(
            cv2.resize(
                cv2.imread(os.devnull) if False else self._blank_frame(),
                (FULL_RES_WIDTH, FULL_RES_HEIGHT),
            ),
            cv2.COLOR_BGR2RGB,
        )
        cv2.putText(frame, "Shared Webcam Source", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 3)
        cv2.putText(frame, message, (80, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (180, 220, 255), 2)
        return frame

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
            "camera_ids": ["WEB-01"],
            "camera_id": "WEB-01",
            "frame_timestamp": time.time(),
            "result": result,
            "timestamp": time.time(),
        }
        try:
            self.status_queue.put_nowait(payload)
        except Full:
            pass


def run_webcam_producer(shutdown_event: EventType, status_queue, device_index: int = 0):
    producer = WebcamProducer(
        shutdown_event=shutdown_event,
        status_queue=status_queue,
        device_index=device_index,
    )
    producer.run()
