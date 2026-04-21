from __future__ import annotations

import os
import time
from multiprocessing.synchronize import Event as EventType
from queue import Full

import numpy as np

from backend.camera_config import CameraConfig
from backend.shared_memory_ring import ShelfSharedMemory, build_shelf_memory_layout


# 单个 OCR 进程的骨架实现，负责管理一组摄像头的识别任务。
class OcrWorker:
    """单个 OCR 进程的骨架实现。

    当前阶段已经接入“按层独立内存空间”的共享内存读取逻辑：
    - 读取 Meta 区判断是否有新帧
    - 读取 Small-Res 区执行快速定位占位流程
    - 为后续按需读取 Full-Res 做好接口预留
    """

    # 初始化 OCR 进程需要的分组配置、退出信号和状态队列。
    def __init__(
        self,
        group_id: int,
        camera_configs: list[CameraConfig],
        shutdown_event: EventType,
        status_queue,
    ):
        self.group_id = group_id
        self.camera_configs = camera_configs
        self.shutdown_event = shutdown_event
        self.status_queue = status_queue
        self._loop_count = 0
        self._last_frame_ids = [0 for _ in camera_configs]
        self.memory: ShelfSharedMemory | None = None

    # OCR 子进程主循环：周期性扫描本组摄像头并上报状态。
    def run(self):
        # OCR 进程作为消费者，需要等待本层共享内存由生产者创建完成后再附加。
        self.memory = ShelfSharedMemory.attach_with_retry(
            layout=build_shelf_memory_layout(self.group_id),
            retry_seconds=15.0,
            retry_interval=0.2,
        )

        # 进程启动时先上报初始化状态，便于 UI 主进程后续接入监控。
        self._publish_status("starting", "ocr worker is starting")
        self._publish_status(
            "info",
            f"ocr group G{self.group_id:02d} 已附加分层共享内存并加载 {len(self.camera_configs)} 路摄像头配置",
        )

        try:
            while not self.shutdown_event.is_set():
                self._process_camera_group()
                self._loop_count += 1
                if self._loop_count % 20 == 0:
                    self._publish_status(
                        "info",
                        f"ocr group G{self.group_id:02d} 已完成第 {self._loop_count} 轮任务扫描",
                    )
                # 当前 OCR 节奏控制为约 5fps：每轮任务扫描后短暂休眠 200ms。
                time.sleep(0.2)
        finally:
            if self.memory is not None:
                self.memory.close()
            self._publish_status("stopped", "ocr worker stopped")

    # 顺序处理当前分组中的全部摄像头，并抽样输出示例结果。
    def _process_camera_group(self):
        for slot_index, camera in enumerate(self.camera_configs):
            frame_timestamp = time.time()
            result = self._process_camera(camera, slot_index)

            # 只对每轮中的第一路摄像头做节流上报，避免日志过于密集。
            if slot_index == 0 and self._loop_count % 30 == 0:
                self._publish_status(
                    "info",
                    f"{camera.camera_id} OCR 已完成，结果为 {result}",
                    camera_id=camera.camera_id,
                    frame_timestamp=frame_timestamp,
                    result=result,
                )

    # 处理单路摄像头的 OCR 任务：先读元数据，再读缩略图执行快速分析占位。
    def _process_camera(self, camera: CameraConfig, slot_index: int) -> str:
        if self.memory is None:
            return "NO_MEMORY"

        meta = self.memory.read_camera_meta(slot_index)
        frame_id = int(meta["frame_id"])

        # frame_id 未更新时直接跳过，避免重复处理同一帧。
        if frame_id == 0 or frame_id == self._last_frame_ids[slot_index]:
            return "NO_NEW_FRAME"

        self._last_frame_ids[slot_index] = frame_id

        # 先读取 Small-Res 执行快速定位占位逻辑。
        small_frame = self.memory.read_small_frame(slot_index)
        avg_brightness = int(np.mean(small_frame))

        # 预留 Full-Res 读取入口，后续做高精度 OCR 与异常复核时使用。
        _full_frame = self.memory.read_full_frame(slot_index)

        # 当前先用亮度阈值模拟 OCR / 检测结果，后续替换成真实 OpenCV 算法。
        if avg_brightness > 180:
            result = "OK"
        elif avg_brightness > 80:
            result = "TEXT: TRACKING"
        else:
            result = "LOW_LIGHT"

        _ = camera
        return result

    # 将 OCR 进程当前状态封装成消息并尝试写入状态队列。
    def _publish_status(
        self,
        stage: str,
        message: str,
        camera_id: str | None = None,
        frame_timestamp: float | None = None,
        result: str | None = None,
    ):
        payload = {
            "process_type": "ocr",
            "group_id": self.group_id,
            "pid": os.getpid(),
            "stage": stage,
            "message": message,
            "camera_ids": [camera.camera_id for camera in self.camera_configs],
            "camera_id": camera_id,
            "frame_timestamp": frame_timestamp,
            "result": result,
            "timestamp": time.time(),
        }
        try:
            self.status_queue.put_nowait(payload)
        except Full:
            # OCR 侧状态上报不应影响后续正式识别节奏。
            pass


# 作为多进程 target 的入口函数，负责创建并运行 OCR Worker。
def run_ocr_worker(
    group_id: int,
    camera_configs: list[CameraConfig],
    shutdown_event: EventType,
    status_queue,
):
    """多进程入口函数，供 runtime.py 直接作为 target 调用。"""
    worker = OcrWorker(
        group_id=group_id,
        camera_configs=camera_configs,
        shutdown_event=shutdown_event,
        status_queue=status_queue,
    )
    worker.run()
