# Uni-Lab 电导工站设备包

电导率自动化测试工站的独立 Uni-Lab-OS 外部设备包。驱动通过
`@device` 和 `@action` 注册，不修改 Uni-Lab-OS 内置 `unilabos/devices`，
也不包含任何现场部署用的设备图 JSON。

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
  --devices ./conductivity_station \
  --external_devices_only

python -m pytest tests -q
```

## 启动

部署设备图由现场单独维护，不随设备包提交：

```bash
unilab \
  --devices ./conductivity_station \
  --external_devices_only \
  -g <现场设备图.json>
```

驱动默认连接 `127.0.0.1:19091`，可在现场设备图中覆盖 `ip`、`port`、
超时、编码、分帧符和 `station_action_names`。

## Mock

仅启动 TCP 工站 Mock：

```bash
python -m conductivity_station.mock_server
```

启动 TCP 工站 Mock 与最小 Uni-Lab Job API Mock：

```bash
python -m conductivity_station.mock_unilab
```
