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
    ├── __init__.py                # 主动导入 ConductivityStation
    ├── conductivity.py            # 当前电导工站驱动
    ├── mock_server.py             # 当前电导工站 TCP Mock
    ├── mock_unilab.py             # 当前最小 Uni-Lab API Mock
    ├── synthesis_station/         # 预留：合成工站
    └── characterization/          # 预留：其他表征设备
        ├── xrd/                    # 预留：XRD
        └── raman/                  # 预留：Raman
```

预留目录目前不注册设备，也不会产生占位动作。后续接入设备时，在相应目录内
增加带 `@device`、`@action` 装饰器的驱动模块，并添加协议 Mock 和测试即可。
现场设备图 JSON 仍由部署环境单独维护，不放入设备包。

## 已注册动作

- `station_status`
- `material_status`
- `batch_status`
- `batch_result`
- `start_batch`
- `stop_current_batch`
- `manual_run`
- `clear_current_batch`

## 本地验证

```bash
unilab --check_mode \
  --devices ./yb_sse_devices \
  --external_devices_only

python -m pytest tests -q
```

## 启动

部署设备图由现场单独维护，不随设备包提交：

```bash
unilab \
  --devices ./yb_sse_devices \
  --external_devices_only \
  -g <现场设备图.json>
```

驱动默认连接 `127.0.0.1:19091`，可在现场设备图中覆盖 `ip`、`port`、
超时、编码、分帧符和 `station_action_names`。

模板式初始化示例：

```python
ConductivityStation(
    device_id="CONDUCTIVITY_STATION",
    config={"ip": "127.0.0.1", "port": 19091},
)
```

## Mock

仅启动 TCP 工站 Mock：

```bash
python -m yb_sse_devices.mock_server
```

启动 TCP 工站 Mock 与最小 Uni-Lab Job API Mock：

```bash
python -m yb_sse_devices.mock_unilab
```
