# Agent Setup / 智能体安装说明

## Copy-Paste Prompt / 复制给智能体的提示

```text
请阅读 https://github.com/Zhenyu98/pi-fan-control 的 README_zh.md，并帮我在树莓派 5 上安装 fan-control 服务。
目标：安全运行 53-58°C Zone MPC 风扇控制服务，并可选启用只读 dashboard。
在修改文件、写入系统目录、启用服务、运行压力测试或触碰 PWM/sysfs 之前，先给我计划并等待批准。
默认只运行非破坏性检查；完成后报告改动文件、执行命令、服务状态和验证结果。
```

## Prerequisites / 前置条件

- Raspberry Pi 5，官方风扇接在板载风扇接口。
- Raspberry Pi OS，带 Python 3 和 systemd。
- 有 `sudo` 权限，用于安装 systemd unit 和写入 PWM/sysfs。
- 先确认 `/boot/firmware/config.txt` 没有会争夺控制权的自定义风扇策略，或明确它只作为 fallback。
- 如果要启用 dashboard，确认局域网访问 `8766` 端口是可接受的。

## Setup Steps / 安装步骤

1. 阅读 `README_zh.md`、service 文件和当前 `/sys/class/hwmon` 风扇路径。
2. 先运行不写 PWM 的 dry-run：

```bash
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 20
```

3. 用户批准后再安装 service：

```bash
sudo cp /home/pi/fan-control/fan-control.service /etc/systemd/system/fan-control.service
sudo cp /home/pi/fan-control/fan-control-dashboard.service /etc/systemd/system/fan-control-dashboard.service
sudo cp /home/pi/fan-control/fan-control-maintenance.service /etc/systemd/system/fan-control-maintenance.service
sudo cp /home/pi/fan-control/fan-control-maintenance.timer /etc/systemd/system/fan-control-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now fan-control.service
sudo systemctl enable --now fan-control-dashboard.service
sudo systemctl enable --now fan-control-maintenance.timer
```

4. 做 smoke test：

```bash
systemctl status fan-control.service
systemctl status fan-control-dashboard.service
journalctl -u fan-control.service -n 50 --no-pager
cat /sys/class/hwmon/hwmon*/name
curl http://127.0.0.1:8766/api/status
```

## Success Signal / 成功信号

- `fan-control.service` 是 `active/running`。
- `fan-control-dashboard.service` 是 `active/running`，或用户明确选择不启用。
- 日志包含 `mode=zone-mpc` 和预测字段。
- dry-run 或日志显示目标区间为 `53.0-58.0C`。
- smoke test 期间没有 warning 及以上级别报错。
- 温度或负载升高时，非零 PWM 能带动 RPM 上升。
- dashboard 只读 API 能返回最新样本和 `zone.low_c=53.0`、`zone.high_c=58.0`。

## Safety Rules / 安全规则

- 不读取或打印密钥。
- 没有明确批准，不 push、不发布、不删除、不部署。
- 测试结束后不要让风扇停在危险状态。
- dry-run 和 service smoke test 通过前，不运行长时间压力测试。
- 温度达到安全阈值时，优先满速散热并停止实验。
- dashboard 只能作为观测面，不能添加 PWM 写接口。
