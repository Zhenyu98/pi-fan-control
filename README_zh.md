# Raspberry Pi 5 高级风扇控制器

[English](README.md) | 简体中文

一个用于 Raspberry Pi 5 官方风扇的高级温控服务，使用 Zone MPC 让风扇策略更主动、更低温、更可控。

它不是等温度越过几个固定阈值后再分段调速，而是根据当前温度、PWM、负载和短期历史预测接下来的温度轨迹，然后选择一个尽量低的 PWM，把机器维持在目标温度区间内。

你会得到的效果：

- 温度曲线更平滑，不容易冲高。
- 风扇可能更早启动，但不一定更吵。
- 控制脚本资源占用几乎无感。
- 在满足控温目标的前提下，Zone MPC 会倾向使用更低的风扇输出。

当前默认策略：

- 目标温度区间：`53-58 C`
- 稳定起转 PWM：`min_active_pwm=75`
- 单次 PWM 最大变化：`max_step=20`
- 满速阈值：`69 C`
- 安全阈值：`70 C`
- 模型文件：`/home/pi/fan-control/data/model_arx2_m2.json`
- 可选只读仪表盘：`http://<raspberry-pi-ip>:8766/`

## 为什么做这个

树莓派默认的内核风扇策略很稳，但它是被动的：温度到某个阈值，就切到一个固定 PWM 档位。这种策略简单可靠，但不知道“接下来温度会怎么走”。

这个项目每个控制周期都会问一个更接近真实需求的问题：

```text
在当前温度、风扇 PWM、CPU 负载和短期历史下，
未来几个控制周期内用多大 PWM，
能把温度维持在目标区间里，同时尽量少用风扇？
```

这就是 Zone MPC 的作用。

## 快速安装

建议安装到固定路径 `/home/pi/fan-control`，因为 systemd service 默认使用这个路径：

```bash
cd /home/pi
git clone https://github.com/Zhenyu98/pi-fan-control.git fan-control
cd /home/pi/fan-control
```

先运行不写 PWM 的 dry-run：

```bash
python3 /home/pi/fan-control/fan_control.py --dry-run --duration 20
```

确认能识别温度和风扇路径后，再安装服务：

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

查看状态：

```bash
systemctl status fan-control.service
systemctl status fan-control-dashboard.service
journalctl -u fan-control.service -n 50 --no-pager
systemctl list-timers fan-control-maintenance.timer
```

局域网访问只读仪表盘：

```text
http://<raspberry-pi-ip>:8766/
```

如果要停用用户态服务，并设置一个安全兜底 PWM：

```bash
sudo systemctl disable --now fan-control.service
sudo systemctl disable --now fan-control-dashboard.service
sudo systemctl disable --now fan-control-maintenance.timer
sudo python3 /home/pi/fan-control/fan_safe.py
```

## 工作原理

### Zone MPC

Zone MPC 不追求把温度死死控制在某一个点，而是控制在一个区间里。默认目标是 `53-58 C`。

控制器会枚举一组候选 PWM，预测未来几个周期的温度轨迹，然后给每个候选方案打分。它更喜欢：

- 温度留在 `53-58 C`；
- PWM 尽量低；
- PWM 变化不要太剧烈；
- 不出现持续超过 `58 C` 的预测；
- 不接近 `69 C` 满速阈值和 `70 C` 安全阈值。

所以它可能比默认策略更早让风扇动起来，但不是为了“更吵”，而是为了避免温度先冲上去再补救。

### 温度预测模型

当前实现使用二阶 ARX 热模型，也就是用当前和上一周期的温度、PWM、负载来预测下一步温度：

```text
T_next =
  a0 * T_now
+ a1 * T_previous
+ b0 * PWM_now
+ b1 * PWM_previous
+ c0 * load_now
+ c1 * load_previous
+ bias
```

这比只看当前温度和负载的一阶模型更适合短期 rollout，因为 MPC 决策依赖未来几步的预测，而不只是下一秒。

控制器还带有保守预测观察器：如果最近模型低估温度，它会加入最多 `3 C` 的预测裕度，让 MPC 更谨慎。

## 实测结果

历史 11 分钟随机压力测试，当时目标区间仍是 `50-60 C`：

```text
Run: /home/pi/fan-control/acceptance/random-stress-20260626-123759
Model: /home/pi/fan-control/data/model_arx2_m2.json
```

摘要：

- 温度最小/平均/最大：`46.85 / 55.34 / 61.15 C`
- 高于 `60 C` 的样本：`10.89%`
- 超过 `60 C` 的最大幅度：`1.15 C`
- 达到 `69 C` 满速阈值的样本：`0`
- 达到 `70 C` 安全阈值的样本：`0`
- 一步预测 MAE/RMSE/Bias：`0.65 / 1.00 / 0.02 C`
- 控制脚本 CPU 占用：单核 `0.045%`
- 最大 RSS：`14528 KiB`
- 服务重启次数变化：`0`
- journal warning：`0`

详细报告：

```text
/home/pi/fan-control/acceptance/arx2_zone_mpc_acceptance_20260626.md
```

## 文件说明

- `fan_control.py`：实时 Zone MPC 风扇控制服务入口。
- `fan_control_core.py`：热模型、预测观察器、Zone MPC 逻辑。
- `fan_control_shadow.py`：影子学习和安全模型提升。
- `fan_control_io.py`：sysfs 风扇路径发现和 I/O。
- `dashboard_server.py`：只读 HTTP 仪表盘 API 和静态文件服务。
- `dashboard.html`：显示温度、PWM、RPM、负载的实时仪表盘。
- `fan_safe.py`：systemd 停止后的安全兜底。
- `collect.py`：采集温度、PWM、负载和 RPM。
- `fit_model.py`：从采样数据拟合模型。
- `identify_model.py`：专门的负载/PWM 辨识实验。
- `compare_models.py`：模型对比报告。
- `evaluate.py`：压力测试和控制脚本资源占用评估。
- `random_stress_test.py`：随机压力阶段测试。
- `fan-control.service`：主 systemd 服务模板。
- `fan-control-dashboard.service`：监听 `8766` 端口的只读仪表盘服务。
- `fan-control-maintenance.service`：日志和实验产物清理服务。
- `fan-control-maintenance.timer`：每日清理定时器。

脚本启动时会自动扫描 `/sys/class/hwmon/hwmon*/name == pwmfan`，不依赖固定 `hwmonN` 编号。

## 影子学习

服务默认带 `--shadow-learn`。

影子学习不会直接控制风扇。实时控制器继续使用当前稳定模型，后台记录本地样本到：

```text
/home/pi/fan-control/data/shadow_samples.csv
```

后台会用滚动窗口拟合候选模型。只有满足这些条件才可能替换当前模型：

- 样本足够；
- 温度、PWM、负载变化足够；
- PWM 系数整体表示“散热”；
- load 系数整体表示“升温”；
- 高温段不能系统性低估；
- 预测误差改善达到阈值。

样本日志会轮转，只保留当前文件和一个 `.1` 文件，避免长期运行无限增长。

## 日志和清理

主服务默认每 `30` 秒输出一次常规状态日志：

```bash
python3 /home/pi/fan-control/fan_control.py --log-interval 30
```

重要事件仍然会立即记录：

- PWM 变化；
- 预测持续越界；
- 满速或安全策略触发；
- 影子学习候选模型通过或被拒绝。

清理工具：

```bash
python3 /home/pi/fan-control/fan_control_maintenance.py --dry-run
sudo python3 /home/pi/fan-control/fan_control_maintenance.py
```

默认策略：

- acceptance 运行目录保留最近 `14` 天，并至少保留最新 `5` 个；
- `data/evaluation-*.json` 保留最近 `14` 天，并至少保留最新 `5` 个；
- journal 执行 `journalctl --vacuum-time=14d --vacuum-size=200M`。

注意：journal vacuum 是 systemd-journald 的全局清理，不是只清这个服务。

## 只读仪表盘

仪表盘从 `data/shadow_samples.csv` 读取实时数据，不提供任何 PWM 写接口：

```bash
sudo systemctl enable --now fan-control-dashboard.service
curl http://127.0.0.1:8766/api/status
```

局域网访问：

```text
http://<raspberry-pi-ip>:8766/
```

接口：

- `/`：实时 HTML 仪表盘。
- `/api/status`：最新采样、服务状态、目标温度区间。
- `/api/latest?minutes=60&max_points=1800`：绘图用最近采样。
- `/api/summary?hours=4`：温度、PWM、RPM、负载和目标区间统计。

服务支持 `HEAD` 健康检查，不会影响主风扇控制服务。

## 安全和隐私

- 控制器自动发现风扇 hwmon 路径，避免重启后 `hwmonN` 变化导致写错路径。
- `fan_safe.py` 作为 systemd `ExecStopPost` 兜底，避免服务退出后风扇停在危险状态。
- `safety_temp` 会在高温时强制满速。
- 影子学习只记录本机热数据，不上传网络。
- raw 样本、日志、运行产物和缓存都被 `.gitignore` 排除，不属于公开发布范围。

## 常见问题

**它一定更安静吗？**

不保证。它的目标是更主动、更平滑、更可控。风扇可能更早启动，但 Zone MPC 会在满足温度目标的前提下尽量降低 PWM，不是简单满速压温度。

**影子学习会不会突然用一个坏模型控制风扇？**

不会。在线学习是影子模式，候选模型必须通过样本量、系数方向、高温段误差和预测改善等检查，才会被提升。

**还能用 `dtparam=fan_temp*` 吗？**

可以把它当 fallback。`fan-control.service` 运行时由用户态控制 PWM；服务停止时，`fan_safe.py` 和内核策略负责兜底。

**运行时需要联网吗？**

不需要。控制器只读写本机温度、负载、PWM 和 RPM。

## Roadmap

- 在更多环境温度和负载组合下采集辨识数据。
- 增加可选 RPM-aware 模型验证。
- 提供一键安装脚本，减少手动复制 systemd 文件。
- 增加轻量命令行或文本 UI，显示温度、PWM、RPM、预测裕度和当前原因。

## Agent 安装

如果你希望让 Codex、ChatGPT、Claude Code、Cursor 等智能体辅助安装，请先让它阅读：

```text
agent-setup.md
```

这个文件要求智能体先做 dry-run 和 smoke test，涉及系统目录、PWM/sysfs、服务启停、压力测试时必须先征求用户批准。

## License

本项目使用 MIT License，见 [LICENSE](LICENSE)。

## Acknowledgements

- [Raspberry Pi documentation](https://www.raspberrypi.com/documentation/)：平台和配置参考。
- [systemd](https://systemd.io/)：服务管理和 journal。
- [stress-ng](https://github.com/ColinIanKing/stress-ng)：验收测试中的压力负载工具。
