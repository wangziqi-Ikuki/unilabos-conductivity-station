# YB SSE Labpackage

电导率自动化测试工站的独立 Uni-Lab-OS 外部设备包。驱动通过
`@device` 和 `@action` 注册，不修改 Uni-Lab-OS 内置 `unilabos/devices`，
也不包含任何现场部署用的设备图 JSON。

本仓库遵循 [LabDeviceTemplate](https://github.com/Xuwznln/LabDeviceTemplate)
的外部设备包规范：设备通过 `device_id` 和 `config` 初始化，业务方法使用
`@action` 注册，状态使用 `@topic_config` 发布，辅助公共方法使用
`@not_action` 排除。

## 设备目录规划

```text
YB_SSE_Labpackage/
└── yb_sse_devices/
    ├── __init__.py                # 主动导入 ConductivityStation 与 Deck
    ├── protocol.py                # 13 步骤名、库位与设备字段
    ├── conductivity.py            # 电导工站驱动
    ├── mock_server.py             # TCP Mock 与本地交互页
    ├── resources/                 # 三层 2×5 料架与占位耗材
    ├── synthesis_station.py       # 预留：合成工站
    └── characterization/          # 预留：其他表征设备
```

预留目录目前不注册设备，也不会产生占位动作。现场设备图 JSON 仍由部署环境单独维护。

## 已注册动作

查询：`station_status`、`material_status`、`batch_status`、`batch_result`、
`query_batch`、`test_connection`

控制：`start_batch` 启动整批自动实验（工站连续跑完 1–13 步，不必再串联分步）；
`stop_current_batch` 停批。13 个具名动作只用于手动单步
（`disassemble_mold` … `transfer_tested_material_to_tray`）。工序 0 人工备料
不是 TCP 动作，不注册。

## 状态 property

工站健康：`status`（IDLE / BUSY / FAULT / OFFLINE）、`station_health`、`connected`，以及七路 0/1
（`robot_arm`、`scanner`、`lid_open_close_mechanism`、
`powder_adding_mechanism`、`tablet_pressing_mechanism`、
`electrochemical_workstation`、`stack_rack`）。

物料余量：`funnel_remaining`、`pending_bottles`、`pending_molds`（均为在位格数 0–10）。
HMI `#1`–`#5` 对应 A01–A05，`#6`–`#10` 对应 B01–B05。

批次：`batch_running`、`current_test_step`（1–13，无批次为 0）、
`finished_count`、`failed_count`。

## 本地验证

```bash
unilab --check_mode \
  --devices ./yb_sse_devices \
  --external_devices_only

python -m pytest tests -q
```

## 启动

动作一律由 UniLab edge 发送。真实工站与虚拟工站都是 TCP 服务端。

```bash
unilab \
  --devices ./yb_sse_devices \
  --external_devices_only \
  -g <设备图.json>
```

真机：设备图 `ip`/`port` 填合作方工站（界面常见 `8091`），不要开 `use_mock`。

虚拟：先另开终端启动 Mock，设备图填同一地址：

```bash
python -m yb_sse_devices.mock_server --host 127.0.0.1 --port 19091
```

默认 TCP `127.0.0.1:19091`，交互页 `http://127.0.0.1:19092/`，可点库位、拨设备在线、改监听端口，并显示 13 步进度与最近一条 TCP 动作。库位默认全空，备料后发 `start_batch` 即可整批自动跑完；`--demo` 可预装 3 瓶 / 3 模 / 3 漏斗。交互 Mock **默认自动推进**，对应整批实验；只要手动单步时加 `--no-auto-advance` 或取消勾选。页上有「初始化 / 清零」。

设备图（如 `conductivity_station.json`）可设：

- `connect_timeout` / `response_timeout`：TCP 连接与等待响应超时
- `occupancy_poll_interval`：占用轮询秒，默认 30，`0` 关闭
- `load_demo_occupancy` / `occupancy`：启动时写入库位占位（虚拟机）

UniLab 动作 `set_slot_occupancy(layer, slot, occupied)`、`load_demo_materials` 也可改占位。

仅开 unilab、不要第二终端时，设备图设 `use_mock: true`，驱动会在本进程拉起 TCP Mock。

模板式初始化示例：

```python
ConductivityStation(
    device_id="CONDUCTIVITY_STATION",
    config={"ip": "127.0.0.1", "port": 19091},
)
```
