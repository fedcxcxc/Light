from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 标记摄像头数据源类型：当前支持本机摄像头和 RTSP 网络流。
SourceKind = Literal["webcam", "rtsp"]

# 标记系统当前运行的数据接入模式。
# - webcam：使用单一本机摄像头模拟 50 路
# - rtsp：使用 1 个 RTSP 地址模拟 50 路输入，便于先验证真实拉流链路
RuntimeSourceMode = Literal["webcam", "rtsp"]
RtspDecodeBackend = Literal["opencv_cpu", "ffmpeg_qsv"]
RtspCodec = Literal["h264_qsv", "hevc_qsv"]
# - single_demo：1 个 RTSP 地址模拟 50 路
# - layered_template：按 1~50 不同地址生成，适合 rtsp_producer 分层拉流
RtspConfigMode = Literal["single_demo", "layered_template"]
SINGLE_RTSP_DEMO_URL = "rtsp://admin:@192.168.1.10:554/h264/ch1/main/av_stream"

# 描述单路摄像头的基础配置，供拉流、OCR、前端展示统一使用。
@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    shelf: int
    position: int
    source_kind: SourceKind
    source: str
    enabled: bool = True
    stream_process_group: int = 0
    ocr_process_group: int = 0
    shared_memory_name: str = ""
    display_name: str = ""


# 描述当前系统的多进程拓扑结构和货架布局参数。
@dataclass(frozen=True)
class ProcessTopology:
    stream_processes: int = 5
    ocr_processes: int = 5
    ui_processes: int = 1
    cameras_per_shelf: int = 10
    shelf_count: int = 5


# 提供整个项目统一使用的拓扑默认值。
PROCESS_TOPOLOGY = ProcessTopology()

# 当前运行模式直接在代码里切换：
# - "webcam"：单摄像头模拟 50 路
# - "rtsp"：真实 RTSP 模式
ACTIVE_SOURCE_MODE: RuntimeSourceMode = "rtsp"

# RTSP 解码后端直接在代码里切换：
# - "opencv_cpu"：OpenCV CPU 解码
# - "ffmpeg_qsv"：FFmpeg + Intel QSV 硬件解码
ACTIVE_RTSP_BACKEND: RtspDecodeBackend = "opencv_cpu"

# FFmpeg QSV 使用的解码器：
# - "h264_qsv"
# - "hevc_qsv"
ACTIVE_RTSP_CODEC: RtspCodec = "h264_qsv"

# 当前 rtsp 模式使用哪种配置构造方式：
# - "single_demo"：给 stream_worker / 单流验证用
# - "layered_template"：给 rtsp_producer / 分层多路架构用
ACTIVE_RTSP_CONFIG_MODE: RtspConfigMode = "layered_template"


# 根据层号和位置号计算 1~50 的全局摄像头序号。
def _camera_index(shelf: int, position: int) -> int:
    return (shelf - 1) * PROCESS_TOPOLOGY.cameras_per_shelf + position


# 构造“1 个本机摄像头模拟 50 路监控位”的演示配置。
def build_simulated_camera_configs(webcam_device_index: int = 0) -> list[CameraConfig]:
    """使用本机一个摄像头模拟 50 路货架位摄像头配置。"""
    configs: list[CameraConfig] = []

    # 逐层、逐位置生成摄像头配置，让前后端都能复用这份结构化数据。
    for shelf in range(1, PROCESS_TOPOLOGY.shelf_count + 1):
        for position in range(1, PROCESS_TOPOLOGY.cameras_per_shelf + 1):
            index = _camera_index(shelf, position)
            camera_name = f"CAM-{index:02d}"
            configs.append(
                CameraConfig(
                    camera_id=camera_name,
                    shelf=shelf,
                    position=position,
                    source_kind="webcam",
                    source=f"webcam://{webcam_device_index}",
                    stream_process_group=shelf,
                    ocr_process_group=shelf,
                    shared_memory_name=f"light_s{shelf:02d}_p{position:02d}",
                    display_name=camera_name,
                )
            )
    return configs


# 构造“1 个 RTSP 地址模拟 50 路监控位”的演示配置。
def build_single_rtsp_demo_camera_configs(rtsp_url: str = SINGLE_RTSP_DEMO_URL) -> list[CameraConfig]:
    """使用 1 个 RTSP 地址模拟 50 路货架位摄像头配置。"""
    configs: list[CameraConfig] = []

    # 逐层、逐位置生成摄像头配置，让前后端都能复用这份结构化数据。
    for shelf in range(1, PROCESS_TOPOLOGY.shelf_count + 1):
        for position in range(1, PROCESS_TOPOLOGY.cameras_per_shelf + 1):
            index = _camera_index(shelf, position)
            camera_name = f"CAM-{index:02d}"
            configs.append(
                CameraConfig(
                    camera_id=camera_name,
                    shelf=shelf,
                    position=position,
                    source_kind="rtsp",
                    source=rtsp_url,
                    stream_process_group=shelf,
                    ocr_process_group=shelf,
                    shared_memory_name=f"light_s{shelf:02d}_p{position:02d}",
                    display_name=camera_name,
                )
            )
    return configs


# 构造后续切换到真实 50 路 RTSP 摄像头时使用的模板配置。
def build_rtsp_camera_configs(
    ip_prefix: str = "192.168.1",
    rtsp_port: int = 554,
    username: str = "admin",
    password: str = "",
    stream_path: str = "/h264/ch1/main/av_stream",
) -> list[CameraConfig]:
    """生成未来真实 50 路 RTSP 摄像头配置模板。"""
    configs: list[CameraConfig] = []

    real_camera_index = 10

    # 当前阶段只有 CAM-10 使用真实 RTSP，其余相机位保留占位配置并显示离线图。
    for shelf in range(1, PROCESS_TOPOLOGY.shelf_count + 1):
        for position in range(1, PROCESS_TOPOLOGY.cameras_per_shelf + 1):
            index = _camera_index(shelf, position)
            camera_name = f"CAM-{index:02d}"
            host = f"{ip_prefix}.{index}"
            camera_rtsp_url = f"rtsp://{username}:{password}@{host}:{rtsp_port}{stream_path}"
            is_real_camera = index == real_camera_index
            configs.append(
                CameraConfig(
                    camera_id=camera_name,
                    shelf=shelf,
                    position=position,
                    source_kind="rtsp",
                    source=camera_rtsp_url if is_real_camera else "",
                    enabled=True,
                    stream_process_group=shelf,
                    ocr_process_group=shelf,
                    shared_memory_name=f"light_s{shelf:02d}_p{position:02d}",
                    display_name=camera_name,
                )
            )
    return configs


# 从总配置中筛出指定货架层对应的全部启用摄像头。
def get_shelf_camera_configs(camera_configs: list[CameraConfig], shelf: int) -> list[CameraConfig]:
    return [config for config in camera_configs if config.shelf == shelf and config.enabled]


# 根据运行模式返回当前激活的数据源配置。
# 这样 runtime 不需要关心配置生成细节，只需要调用这一处入口。
def build_active_camera_configs() -> list[CameraConfig]:
    if ACTIVE_SOURCE_MODE == "rtsp":
        if ACTIVE_RTSP_CONFIG_MODE == "single_demo":
            return build_single_rtsp_demo_camera_configs()
        return build_rtsp_camera_configs()
    return build_simulated_camera_configs()


# 当前阶段默认使用环境变量决定输入模式：
# - webcam：单摄像头模拟 50 路
# - rtsp：未来真实 50 路 RTSP
CAMERA_CONFIGS = build_active_camera_configs()
