from __future__ import annotations

import time
from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np

# Full-Res 默认使用约 3MP 的 4:3 画幅，供高精度 OCR 与追溯使用。
FULL_RES_WIDTH = 2048
FULL_RES_HEIGHT = 1536
FULL_RES_CHANNELS = 3

# Small-Res 默认使用等比例缩略图，供快速定位与粗筛使用。
SMALL_RES_WIDTH = 640
SMALL_RES_HEIGHT = 480
SMALL_RES_CHANNELS = 3

# 每层固定 10 路摄像头。
CAMERAS_PER_SHELF = 10

# 元数据区使用结构化 dtype，便于跨进程稳定访问。
META_DTYPE = np.dtype(
    [
        ("frame_id", np.uint64),
        ("timestamp_ms", np.uint64),
        ("full_width", np.uint32),
        ("full_height", np.uint32),
        ("small_width", np.uint32),
        ("small_height", np.uint32),
        ("online", np.uint8),
        ("sync_flag", np.uint8),
        ("full_ready", np.uint8),
        ("small_ready", np.uint8),
        ("reserved", np.uint8, 12),
    ]
)


@dataclass(frozen=True)
class ShelfMemoryNames:
    shelf: int
    full_res_name: str
    small_res_name: str
    meta_name: str


@dataclass(frozen=True)
class ShelfMemoryLayout:
    shelf: int
    camera_count: int = CAMERAS_PER_SHELF
    full_width: int = FULL_RES_WIDTH
    full_height: int = FULL_RES_HEIGHT
    small_width: int = SMALL_RES_WIDTH
    small_height: int = SMALL_RES_HEIGHT
    channels: int = FULL_RES_CHANNELS

    @property
    def full_frame_shape(self) -> tuple[int, int, int, int]:
        return (self.camera_count, self.full_height, self.full_width, self.channels)

    @property
    def small_frame_shape(self) -> tuple[int, int, int, int]:
        return (self.camera_count, self.small_height, self.small_width, self.channels)

    @property
    def meta_shape(self) -> tuple[int]:
        return (self.camera_count,)

    @property
    def full_nbytes(self) -> int:
        return int(np.prod(self.full_frame_shape, dtype=np.int64))

    @property
    def small_nbytes(self) -> int:
        return int(np.prod(self.small_frame_shape, dtype=np.int64))

    @property
    def meta_nbytes(self) -> int:
        return int(self.camera_count * META_DTYPE.itemsize)

    @property
    def names(self) -> ShelfMemoryNames:
        return ShelfMemoryNames(
            shelf=self.shelf,
            full_res_name=f"light_shelf_{self.shelf:02d}_full",
            small_res_name=f"light_shelf_{self.shelf:02d}_small",
            meta_name=f"light_shelf_{self.shelf:02d}_meta",
        )


class ShelfSharedMemory:
    """管理单层货架对应的三块共享内存。

    - Full-Res：3MP 原始图
    - Small-Res：缩略图
    - Meta：帧号、时间戳、同步标志等元数据
    """

    def __init__(self, layout: ShelfMemoryLayout, create: bool):
        self.layout = layout
        self.create = create
        self._full_shm = self._open_block(layout.names.full_res_name, layout.full_nbytes)
        self._small_shm = self._open_block(layout.names.small_res_name, layout.small_nbytes)
        self._meta_shm = self._open_block(layout.names.meta_name, layout.meta_nbytes)

        # 将共享内存映射成 numpy 视图，方便 OpenCV / NumPy 直接处理。
        self.full_frames = np.ndarray(
            layout.full_frame_shape,
            dtype=np.uint8,
            buffer=self._full_shm.buf,
        )
        self.small_frames = np.ndarray(
            layout.small_frame_shape,
            dtype=np.uint8,
            buffer=self._small_shm.buf,
        )
        self.meta = np.ndarray(
            layout.meta_shape,
            dtype=META_DTYPE,
            buffer=self._meta_shm.buf,
        )

        # 创建者负责初始化共享内存，确保 OCR 进程首次读取时状态明确。
        if create:
            self.full_frames.fill(0)
            self.small_frames.fill(0)
            self.meta.fill(0)

    def _open_block(self, name: str, size: int) -> shared_memory.SharedMemory:
        if self.create:
            return shared_memory.SharedMemory(name=name, create=True, size=size)
        return shared_memory.SharedMemory(name=name, create=False)

    @classmethod
    def attach_with_retry(
        cls,
        layout: ShelfMemoryLayout,
        retry_seconds: float = 10.0,
        retry_interval: float = 0.2,
    ) -> "ShelfSharedMemory":
        """等待共享内存被生产者创建后再附加。

        适用于 OCR / UI 这类消费者进程，避免它们早于生产者启动时直接抛出
        FileNotFoundError。
        """
        deadline = time.time() + retry_seconds
        last_error: FileNotFoundError | None = None
        while time.time() < deadline:
            try:
                return cls(layout=layout, create=False)
            except FileNotFoundError as exc:
                last_error = exc
                time.sleep(retry_interval)
        if last_error is not None:
            raise last_error
        raise FileNotFoundError(layout.names.full_res_name)

    def write_camera_frame(
        self,
        slot_index: int,
        full_frame: np.ndarray,
        small_frame: np.ndarray,
        frame_id: int,
        timestamp_ms: int,
        online: bool = True,
        sync_flag: int = 1,
    ):
        # 将单路相机的完整帧写入 Full-Res 区。
        self.full_frames[slot_index][:] = full_frame

        # 将对应缩略图写入 Small-Res 区，供 OCR 快速定位使用。
        self.small_frames[slot_index][:] = small_frame

        # 同步刷新该路元数据，供消费者判断是否有新帧到达。
        slot_meta = self.meta[slot_index]
        slot_meta["frame_id"] = frame_id
        slot_meta["timestamp_ms"] = timestamp_ms
        slot_meta["full_width"] = full_frame.shape[1]
        slot_meta["full_height"] = full_frame.shape[0]
        slot_meta["small_width"] = small_frame.shape[1]
        slot_meta["small_height"] = small_frame.shape[0]
        slot_meta["online"] = 1 if online else 0
        slot_meta["sync_flag"] = sync_flag
        slot_meta["full_ready"] = 1
        slot_meta["small_ready"] = 1

    def read_camera_meta(self, slot_index: int) -> np.void:
        return self.meta[slot_index]

    def read_full_frame(self, slot_index: int) -> np.ndarray:
        return self.full_frames[slot_index]

    def read_small_frame(self, slot_index: int) -> np.ndarray:
        return self.small_frames[slot_index]

    def close(self):
        self._full_shm.close()
        self._small_shm.close()
        self._meta_shm.close()

    def unlink(self):
        # 只有创建者负责释放共享内存命名对象，避免多进程重复 unlink。
        if not self.create:
            return
        self._full_shm.unlink()
        self._small_shm.unlink()
        self._meta_shm.unlink()


def build_shelf_memory_layout(shelf: int) -> ShelfMemoryLayout:
    """根据货架层号构造该层对应的共享内存布局。"""
    return ShelfMemoryLayout(shelf=shelf)
