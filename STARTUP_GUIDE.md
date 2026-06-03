# 小红书热点话题采集系统 — 启动手册

> 当前活跃阶段：**Phase 3 — 定时调度连续运行验证**  
> 系统状态：已验证正常，每次运行产出 60 条话题

---

## 一、环境要求

| 项目 | 要求 |
|------|------|
| Python | 3.10+ |
| 操作系统 | macOS（本机部署） |
| 虚拟环境 | `/Users/kika/Downloads/oppo-hotwords-master/venv` |
| 小红书账号 | 需要登录态（首次扫码，session 有效期约 2–4 周） |

---

## 二、首次启动（只需执行一次）

### 第 1 步：激活虚拟环境

```bash
source /Users/kika/Downloads/oppo-hotwords-master/venv/bin/activate
cd /Users/kika/lxlin/scheduled_scraping
```

> 如果依赖尚未安装，先运行：
> ```bash
> pip install -r requirements.txt
> python -m playwright install chromium
> ```

### 第 2 步：扫码登录（首次必做，session 失效时重做）

```bash
python login.py
```

浏览器会弹出小红书二维码，用手机 App 扫码即可。  
登录成功后，session 自动保存到 `browser_profile/xhs/storage_state.json`，后续采集无需重复登录。

### 第 3 步：验证登录状态

```bash
python login.py --check
```

输出 `Session 有效` 即可进入正式运行。

---

## 三、日常运行

### 方式 A：手动测试一次采集

```bash
# 激活环境
source /Users/kika/Downloads/oppo-hotwords-master/venv/bin/activate
cd /Users/kika/lxlin/scheduled_scraping

# 直接采集（最简单）
python scraper.py

# 通过调度器触发（含 session 检查 + 日志写入，更接近生产行为）
python scheduler.py --run-now
```

成功后查看输出：
```bash
ls -lh output/$(date +%Y%m%d)/
```

### 方式 B：启动定时任务（每天 10:40 自动运行）

**前台运行**（开发调试，关闭终端即停止）：
```bash
python scheduler.py
```

**后台守护进程**（生产推荐，终端关闭后继续运行）：
```bash
nohup python scheduler.py > output/logs/scheduler_daemon.log 2>&1 &
echo $! > output/scheduler.pid
echo "调度器已启动，PID: $(cat output/scheduler.pid)"
```

查看后台日志：
```bash
tail -f output/logs/scheduler_daemon.log
```

停止后台调度器：
```bash
kill $(cat output/scheduler.pid)
```

### 方式 C：macOS launchd（防休眠中断，最稳定）

适用于笔记本合盖后也需要定时触发的场景。

1. 创建 plist 文件（替换其中的路径）：

```xml
<!-- ~/Library/LaunchAgents/com.xhs.hotwords.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xhs.hotwords</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/kika/Downloads/oppo-hotwords-master/venv/bin/python</string>
        <string>/Users/kika/lxlin/scheduled_scraping/scheduler.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>10</integer>
        <key>Minute</key>
        <integer>40</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/kika/lxlin/scheduled_scraping/output/logs/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/kika/lxlin/scheduled_scraping/output/logs/launchd_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

2. 注册并启动：
```bash
launchctl load ~/Library/LaunchAgents/com.xhs.hotwords.plist
```

3. 注销（停止）：
```bash
launchctl unload ~/Library/LaunchAgents/com.xhs.hotwords.plist
```

---

## 四、定时时间配置

当前默认时间：**每天 10:40**（已在 `config.py` 中设置）。

修改方式（三种，任选其一）：

**方式 1：永久修改（改 `config.py`）**
```python
SCHEDULE_HOUR: int = int(os.getenv("SCHEDULE_HOUR", "10"))   # 改这里
SCHEDULE_MINUTE: int = int(os.getenv("SCHEDULE_MINUTE", "40")) # 改这里
```

**方式 2：环境变量临时覆盖（不改文件）**
```bash
SCHEDULE_HOUR=9 SCHEDULE_MINUTE=0 python scheduler.py
```

**方式 3：CLI 参数**
```bash
python scheduler.py --hour 9 --minute 0
```

---

## 五、输出文件说明

```
output/
├── 20260603/
│   └── xhs_creator_hot_20260603_1040.tsv   ← 主输出（60 条热点话题）
├── logs/
│   ├── 20260603.log                         ← 当天调度日志
│   └── scheduler_daemon.log                 ← 后台守护进程日志（nohup 模式）
├── flags/
│   └── session_expired_YYYYMMDD.flag        ← session 失效标志（出现即需重新登录）
└── alerts/
    └── alert_YYYYMMDD_HHMMSS.txt            ← 采集失败告警（0 条或异常）
```

### TSV 字段说明

| 字段 | 说明 | 示例值 |
|------|------|--------|
| `rank` | 话题在该分区的排名 | `1` |
| `topic` | 话题名称 | `高颜值巧克力` |
| `view_count` | 观看人数（格式化） | `14.8亿` |
| `category` | 所属分区 | `美食` |
| `collected_at` | 采集时间（ISO 格式） | `2026-06-03T10:40:12` |

---

## 六、Session 失效处理

**触发条件**：`output/flags/` 目录下出现 `session_expired_YYYYMMDD.flag`，或采集结果连续为 0 条。

**处理步骤**：
```bash
# 1. 重新扫码登录
python login.py

# 2. 确认恢复
python login.py --check

# 3. 手动补跑当天采集
python scheduler.py --run-now
```

---

## 七、接口变更应急处理

若采集结果为 0 条且 session 有效，说明平台 API 可能变更：

```bash
# 重新发现接口（有头模式，可观察浏览器行为）
python scraper.py --discover --no-headless

# 查看拦截到的 API 列表
python -c "
import json
import glob, os
latest = sorted(glob.glob('output/discover/*/api_responses.jsonl'))[-1]
print('文件:', latest)
with open(latest) as f:
    for line in f:
        r = json.loads(line)
        print(r['url'])
"
```

根据新发现的接口，更新 `config.py` 中的 `INSPIRE_API_PATTERNS`。

---

## 八、快速参考命令

```bash
# 激活环境
source /Users/kika/Downloads/oppo-hotwords-master/venv/bin/activate
cd /Users/kika/lxlin/scheduled_scraping

# 检查 session
python login.py --check

# 重新登录
python login.py

# 立即采集（测试）
python scheduler.py --run-now

# 查看今天的采集结果
cat output/$(date +%Y%m%d)/xhs_creator_hot_*.tsv | head -20

# 启动后台调度器
nohup python scheduler.py > output/logs/scheduler_daemon.log 2>&1 &
echo $! > output/scheduler.pid

# 停止后台调度器
kill $(cat output/scheduler.pid)

# 查看调度器日志
tail -f output/logs/scheduler_daemon.log
```
