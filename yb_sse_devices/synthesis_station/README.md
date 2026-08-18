# 合成工站（预留）

本目录用于后续接入合成工站，目前不包含设备注册代码。

建议后续保持以下结构：

```text
synthesis_station/
├── __init__.py
├── synthesis_station.py   # @device、@action 驱动实现
└── mock_server.py          # 可选：现场协议 Mock
```

对应测试放在仓库根目录的 `tests/synthesis_station/`。现场组网 JSON 不提交到
本设备包。
