# 代码评审报告 · jev-chat-windows

评审范围：`main.py`、`app/`（11 个模块）、`core/`（6 个模块）、`tools/`（12 个脚本）、`probe/`（7 个探针）、`jev.spec`、`build.bat`、`requirements.txt`、`.github/workflows/release.yml`。
评审日期：2026-09-26。

---

## 一、总体判断

这是一个**设计意图明确、注释质量罕见地高**的个人项目。作者在关键决策点都留了「为什么这么写 + 实测数据 + 踩过的坑」，这在同类项目里很少见。

**做得好的地方**（这些不是客套，是评审时逐条验证过的）：

| 项 | 证据 |
|---|---|
| 分层干净 | `core/` 平台无关、只用 stdlib；`app/` 管 Windows 与 UI；`tools/` 离线验证。`app/textsim.py` 单独拆出来，避免主进程为了比两个字符串而加载 40MB OCR 模型——这是个很好的判断 |
| 错误处理有分类 | `JevError(status, hint, retries)` 三件套贯穿到状态栏；「对端明确失败」与「自己的 bug」走两条文案路径（`main.py:139-142`） |
| 脱敏设计正确 | `jev_client.redact_secrets()` 遍历**所有** `*_API_KEY` 而非硬编码名单，并有自测证明 `CUSTOM_API_KEY` 这类新增 key 不会漏 |
| 重试策略有判据 | 5xx/408/429 重试、其余 4xx 不重试、**超时故意不重试**（`jev_client.py:177-187`，附实测代价 48 秒 → 15 秒） |
| 提示词注入双层防御 | prompt 里声明 + 出口 `_sanitize()` 硬过滤，且 `draft.py:322-330` 有对应测试 |
| 自动发送的闸门设计 | 多层前置闸门 + 倒计时代数 `_autoSerial` 解决旧定时器串台 + `main.auto_send_reply()` 在按发送键前重新确认（`main.py:351-378`）。方向永远偏「不发」，这是对的 |
| 发布链路有闸门 | CI 强制「发布包只能来自全新构建」，`check_release_bundle.py` 拦 `config.json`/聊天记录/`.env` |
| 无危险调用 | 全项目无 `subprocess`/`eval`/`pickle`/`os.system`；core 层零第三方网络库 |

**主要短板**集中在三处：**持久化的健壮性与密钥存放方式**、**UI 层的结构（一个类 1700 行）**、**测试基础设施（无可信的单入口、无静态检查）**。

下面按维度展开。优先级定义：**P0 = 必须优先处理**，P1 = 应该尽快，P2 = 可以排期。

---

## 二、P0 · 必须优先处理

### P0-1 config.json 非原子写入 + 读取失败静默重置（数据丢失）

**问题**

- `app/settings.py:445-455`：`save()` 直接 `open(_CONFIG, "w")` 覆盖写，没有「写临时文件 → `os.replace()`」这一步。
- `app/settings.py:52-59`：`_cfg()` 捕获 `(OSError, ValueError)` 后**返回 `{}`**。于是所有 getter 静默退回默认值。

**影响**

1. 写到一半崩溃/断电/磁盘满 → 文件被截断成半截 JSON。
2. 下次启动时 `_cfg()` 解析失败 → **所有设置静默变回默认**：关系背景、中转地址、模型名、候选条数、自动发送三个开关、群昵称……全部。
3. 最危险的一步：用户此时如果点一次「保存设置」，`save()` 会把这份「默认值」写回去，**把损坏但可能可救的文件彻底覆盖**。用户看到的是「我的配置全没了」，且全程**没有任何提示**。

**优化方向**

- 写入改原子：写 `config.json.tmp` → `flush` + `os.fsync` → `os.replace()`。
- 读取失败时不要静默吞掉：把损坏文件重命名为 `config.json.bad-<时间戳>`，保留现场，并**在状态栏/设置页显式告警**。
- 内存里保留「上一次成功解析的快照」，解析失败时用它而不是默认值——降级到「旧配置」比降级到「默认配置」安全得多。

---

### P0-2 API 密钥以明文存放在注册表，且被广播到所有新进程

**问题**

- `app/settings.py:316-327` `_set_key()`：`winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)` —— 明文 `REG_SZ` 写入 `HKCU\Environment`，随后 `SendMessageTimeoutW(WM_SETTINGCHANGE)` 广播。

**影响**

- 任何以当前用户身份运行的进程（含低权限恶意软件、任何脚本、任何 IDE 插件）都能从注册表或环境块里直接读到 key。
- 广播这一步让它进入**之后启动的所有进程**的环境变量——扩大了暴露面，而收益只是「新开的终端能看到」。
- 项目自己在 `docs/KICKOFF.md #6` 立了「key 只进环境变量，绝不落文件」的规矩。但**注册表明文 `REG_SZ` 在安全性上等价于落文件**，而且比一个 600 权限的文件更显眼、更容易被批量扫描。目前的实现与这条规矩的**意图**是矛盾的。
- README 里没有告诉用户「你的 key 会以明文存在 `HKCU\Environment`」——用户是在不知情的前提下填的。

**优化方向**

- 用 DPAPI 加密后再存（`ctypes` 调 `CryptProtectData`/`CryptUnprotectData`，密文绑当前用户），这是 Windows 上存本地密钥的标准做法。
- 或改用凭据管理器（`CredWrite`/`CredRead`）。
- 若决定保留现状（个人项目，权衡可接受），**至少要在 README 和设置页里写明**存放位置与明文性质，让用户自己做决定。
- 无论选哪条，都建议去掉那次 `WM_SETTINGCHANGE` 广播——它不是功能必需。

---

### P0-3 CI 硬编码回归脚本清单，新增脚本会被静默漏跑

**问题**

`.github/workflows/release.yml:88-89` 硬编码了 7 个文件名：

```
for f in check_auto_send check_draft_mode check_recorder check_retry \
         check_self_judge check_ui_layout check_overlay_runtime; do
```

同一份清单在 `README.md:495` 和 `docs/DESIGN.md:151` 又各抄了一遍。

**影响**

这是「测试永不执行」的经典形态：以后新增 `tools/check_xxx.py`，只要忘了改这三处中的任意一处，CI 照样全绿。**绿灯变成假信号**，比没有测试更危险。

**优化方向**

- 改成发现式：`for f in tools/check_*.py`（`check_release_bundle.py` 因为依赖构建产物，单独一步跑）。
- README / DESIGN 里只写「跑 `tools/` 下所有 `check_*.py`」，不再列名单。
- 加一条自检：CI 里断言「发现到的脚本数 ≥ 某个数」，防止通配符写错导致一个都没跑。

---

### P0-4 两个检查脚本本身不可信：一个在开发机必挂，一个是恒真断言

**P0-4a · `tools/check_auto_send.py:550` 环境耦合**

```python
assert settings.judge_engine() == "none"
```

该脚本**没有**像 `tools/check_draft_mode.py:176` 那样 stub 掉 `settings._get_key`。而 `app/settings.py:301-314` 的 `_get_key()` 是「先读 `os.environ`，再读 `HKCU\Environment`」。

**影响**：作者本机一旦配过 OpenRouter 密钥，`judge_engine()` 就返回 `openrouter`，这行断言必挂。结果是**只有干净的 CI runner 能过**。本地跑不过 → 被当成噪音 → 久而久之没人跑它。这正是「测试失去信任」的开始。

**方向**：像其它脚本一样 stub `_get_key`，让脚本在任何机器上行为一致。

**P0-4b · `tools/check_overlay_runtime.py:330` 恒真断言**

```python
assert ov.judgeSwitch.isChecked() is False or True
```

永远为真（代码里的注释也承认）。这类断言比没有断言更糟——它会在统计上制造「覆盖了」的错觉。

**方向**：删掉，或改成真正验证开关与 `judge_engine()` 的一致性。

---

## 三、P1 · 应该尽快处理

### P1-1 `app/overlay.py`：2018 行单文件，`Overlay` 类约 1700 行

**问题**：一个类同时承担窗口框架与标题栏拖拽、首页、设置页（4 张卡约 30 个控件）、自动发送倒计时状态机、聊天记录面板、多会话结果缓存、更新提示。其中 `_build_settings()` 单方法约 440 行（`overlay.py:706-1151`）。

**影响**：新增一个设置项要同时改**四处**——`_build_settings()` 建控件、`_load_settings()` 读值、`_save()` 写值、`_sync_*_fields()` 管显隐。这是最容易漏项的结构，而漏项的表现是「设置保存了但不生效」这类难查的问题（代码注释里已经记录了两次这类事故）。

**优化方向**

- 按「页」拆分：`ui/home_page.py`、`ui/settings_page.py`、`ui/auto_send_bar.py`，`Overlay` 只保留装配与状态分发。
- 成本最低的第一步：把设置项做成**声明式字段表**（`(配置键, getter, setter, 控件, 显隐条件)` 的列表），由框架统一驱动「读 / 写 / 显隐」，消灭手写四份。这一步不改变任何外部行为，风险低、收益立竿见影。

### P1-2 参数爆炸：`settings.save()` 27 个参数，`analyze()` 17 个

**问题**：`app/settings.py:388-402`；`core/engine.py:94-101`。调用点 `overlay._save()`（1423-1447 行）靠 20 多个关键字参数一一对应。

**影响**：「`None` = 保留当前值，`""` = 清空」这个约定只存在于注释里，没有任何类型约束。加一个设置要动 4 个文件；漏传一个参数的表现是静默保留旧值。

**优化方向**：引入 dataclass（`Settings` / `AnalyzeRequest`），`save(settings)` 传对象、`analyze(req)` 传对象。默认值语义由 dataclass 字段表达，可被类型检查器覆盖。

### P1-3 `_cfg()` 每次 getter 调用都重新读盘并 `json.loads`

**问题**：`app/settings.py:52-59` 无任何缓存。`main.analyze_bg()` 在 `main.py:122-137` 一次性调用了约 14 个 getter → **14 次文件读 + JSON 解析**。UI 线程上的 `_sync_auto_fields()` / `_sync_footer()` / `_mode_note()` 也在反复读。

**影响**：单次开销小（<1ms），但它在**热路径和 UI 线程**上，且随设置项增加线性增长。更要紧的是一个正确性问题：同一轮生成里读到的 14 个值，理论上可能来自**两个不同版本的配置文件**（用户刚好在这一刻保存）。

**优化方向**：`_cfg()` 按 `os.stat().st_mtime_ns` 做缓存失效；或在每轮生成开头一次性快照成 `Settings` 对象传下去（与 P1-2 的 dataclass 天然契合）。

### P1-4 `tools/` 零公共基础设施，重复代码已经导致过一次 CI 失败

**问题**：`sys.stdout` 的 UTF-8 包装在约 10 个脚本里各抄一份；`sys.path` 插入、临时目录、配置隔离同样各抄一份。

**影响**：这次 CI 在 cp1252 上崩，根因就是 `check_draft_mode.py` 和 `check_recorder.py` **漏抄了那一行**。这不是假设风险，是**已经发生的实证**：重复的东西一定会漂移。

**优化方向**：抽 `tools/_checkutil.py`，提供 UTF-8 stdout（含 stderr，见 P2-2）、`isolate_config()` 上下文管理器、`load_pure()` 等。

### P1-5 `app/ocr.py` 去重是 O(行数 × 500) 次字符串相似度计算

**问题**：`Reader._seen()`（`ocr.py:182-184`）线性扫描 `seen`（上限 500 条），每条都跑 `difflib.SequenceMatcher`。`new_lines()`（175-180 行）对**每一行**调用 `_seen` 两次。`recorder._is_dup()`（`recorder.py:163-168`）是同型结构。

**影响**：一帧 30 行 → 30 × 2 × 500 ≈ **3 万次 `SequenceMatcher`**。子进程里这块被 OCR 本身的 250–800ms 掩盖了，但滚动/刷屏时会叠加。**而 `recorder` 那条跑在主进程，会直接卡 UI。**

**优化方向**：先做廉价粗筛再算相似度——按 `(who, 文本长度)` 或首字符分桶，只对同桶候选算 `similar()`；或先用 `quick_ratio()` 预筛。这一层是纯函数、`tools/check_recorder.py` 已有覆盖，改动可验证。

### P1-6 `core/` 的 `try: from .x import ... except ImportError: from x import ...` 双份导入

**问题**：`core/engine.py:14-23`、`core/draft.py:13-18`、`core/judge.py:20-27`。

**影响**：两点。
1. PyInstaller 静态分析要同时处理两条分支，会产出 `missing module named draft` 之类的噪音警告——而 `jev.spec:11-23` 那段针对 `pyinstaller-hooks-contrib` 的防御代码说明作者**已经被打包静默失败坑过一次**，这类噪音会掩盖真问题。
2. 更隐蔽：`except ImportError` 会**吞掉被导入模块内部真正的 ImportError**（比如某个依赖没装好），把「依赖缺失」伪装成「换条路径重试」。

**优化方向**：`core/` 是有 `__init__.py` 的包，统一用相对导入。需要直接跑自测时用 `python -m core.draft`——`app/update.py:51` 的注释里已经是这个写法了，照它统一即可。

### P1-7 依赖完全不锁版本

**问题**：`requirements.txt:2-6` 只有 rapidocr 有范围，`numpy` / `windows-capture` / `PySide6-Fluent-Widgets` 全裸。没有 `pyproject.toml`，没有 lock 文件。

**影响**：
- CI 今天能出包、下个月可能就出不来或行为变了。
- `windows-capture` 是 Rust 编译的 `.pyd`（ABI 敏感）；`PySide6-Fluent-Widgets` 会拉**任意版本**的 PySide6（Qt ABI 敏感），而 `overlay.py` 大量依赖 qfluentwidgets 的内部行为（例如 `ComboBox.minimumSizeHint` 的算法、`SwitchButton` 的尺寸计算——注释里多处依赖这些细节做布局省略）。
- `build.bat` 和 CI 各自独立 `pip install` 一次，**不保证装出同一套版本**——「本地能跑、CI 崩」这类问题会持续出现。

**优化方向**：项目已经在用 uv，加 `uv.lock` 成本很低；CI 与 `build.bat` 都改走 `uv sync --frozen`。

### P1-8 `requirements.txt:6` 的注释是错的，照着删会打包即崩

```
pillow  # only for the synthetic images in probe/
```

但 `jev.spec:47` 明确写着「PIL 千万别排，读图要它」。注释与事实矛盾，而注释往往比代码更容易被信任。

**方向**：改注释，说明它是运行时光学依赖。

---

## 四、P2 · 可以排期

### 架构与可测试性

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-1 | 模块级全局状态：`chats`/`state`/`results`/`phase_q`/`history_rec`/`child`/`ov` 全是模块变量，函数靠它们通信；`ov` 甚至在函数定义之后才赋值 | `main.py:33-47, 591` | 无法实例化两份、无法单测、导入即绑定。方向：收进一个 `App` 类 |
| P2-2 | 只包装了 stdout，没包装 stderr | `tools/*.py` 约 10 处 | 本地 cp936/cp1252 下 `AssertionError` 里的中文会 `UnicodeEncodeError`，**把真正的错误掩盖掉**。CI 靠 `PYTHONIOENCODING=utf-8` 兜底，正好掩盖了这个缺陷 |
| P2-3 | `_rank` 的 `weight()` 用返回 `-v` 来实现降序 | `overlay.py:126-130` | 可读性差（负号当降序开关），容易在后续维护中被改错 |
| P2-4 | `_CHOICES` 的中文枚举映射与 `core/questions.py` 是两份手抄的对应关系 | `overlay.py:30-46` ↔ `questions.py` | 新增一个枚举值只改一处 → 界面显示「暂未判断」。方向：从 `JUDGE_QUESTIONS` 生成，或加一条断言测试 |
| P2-5 | `_fill` 里函数内 `import traceback` | `overlay.py:1531` | 应提到文件顶部 |
| P2-6 | `fill()` 和 `_copy()` 都会覆盖用户剪贴板 | `fill.py:63`、`overlay.py:1543` | 用户原来复制的内容被候选回复顶掉，且无提示。方向：至少提示，或考虑保存/恢复 |

### 性能与轮询

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-7 | `tick()` 50ms 自循环轮询三个队列 | `main.py:532-580` | 20Hz 空转，什么都没发生时也在跑 `drain()`（含 `get_nowait()` 的异常开销）。方向：间隔放宽到 100–200ms（人感知不到差别），或改事件驱动 |
| P2-8 | `worker` 采集态下 `time.sleep(0.05)` 固定轮询 | `worker.py:103` | 可接受（本就要轮询采集线程状态），但间隔硬编码 |
| P2-9 | `_noted` 集合无上限 | `recorder.py:75, 216-220` | 长时间运行缓慢增长。量级极小，但属无界增长。方向：`deque(maxlen=...)` |
| P2-10 | 6 处 `time.sleep` 硬编码 0.05–0.15s 的输入节奏 | `fill.py:79,84,87,89,96,122` | 注释说明是实测值（合理），但慢机器上仍有「粘贴未落地就回车」的窗口。方向：抽成常量并集中注释 |

### 可观测性

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-11 | **`console=False` 打包后 `print` 无处可去** | `jev.spec:84` + `main.py:579` | `tick()` 的兜底 `except Exception: traceback.print_exc()` 在打包版里**完全静默**。主循环里出异常，用户只看到界面没反应，没有任何线索。方向：写滚动日志文件（复用已有的 `redact_secrets()` 脱敏），或至少写进「聊天记录」面板 |
| P2-12 | 日志全是字符串塞进 `PlainTextEdit` | `overlay.py:1782-1788` | 无级别、无时间戳、无法导出。对个人项目可接受，但配合 P2-11 就成了排查黑洞 |
| P2-13 | `check_release_bundle.py:160` `scan_keys()` 命中**只提醒不失败** | `tools/check_release_bundle.py` | 文本里的真密钥不会被拦下发布。方向：高置信度模式（`sk-` + 20 位以上）应直接 `exit 1` |

### 测试与工程实践

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-14 | 无统一 runner、无 pytest、无覆盖率 | 全局 | 手写断言脚本本身质量不低（断言具体、退出码清晰），但缺单入口。见 P0-3 |
| P2-15 | CI 只有构建 job，**没有 lint / 类型检查 / `compileall`** | `.github/workflows/release.yml` | 2018 行的 `overlay.py` 改动没有任何静态防线。方向：加 ruff（配置成本极低，能抓未使用导入、未定义名、可疑比较） |
| P2-16 | `tempfile.mkdtemp()` 用完不删 | `tools/check_draft_mode.py:173,215` | 每次运行泄漏两个临时目录 |
| P2-17 | `os._exit(0)` 绕过 Qt 析构与 atexit | `tools/check_overlay_runtime.py:400` | 注释说是为躲 offscreen 段错误（合理权衡），但会掩盖退出期的清理错误。方向：记录为已知问题，或定位真正的段错误 |
| P2-18 | 断言依赖 offscreen 下的 `minimumSizeHint` | `tools/check_overlay_runtime.py:238` | 跨 Qt 版本/字体可能漂移 → flaky |
| P2-19 | `patch.multiple` 穷举 settings 接口 | `tools/preview_ui.py:228` | 新增任何 overlay 用到的 getter 就会 `TypeError`（注释已承认）。方向：改 `MagicMock` + 显式返回表 |
| P2-20 | `check_draft_mode.py:37` 用 `ast` 抠出 `_rank` 单独 `exec` | `tools/check_draft_mode.py` | 手法巧妙（绕开 PySide6），但 `_rank` 一改成方法/加装饰器就失效。属于有意识的取舍，建议在函数 docstring 里标注这个耦合 |
| P2-21 | 测试与实现常量耦合 | `tools/check_recorder.py:159` 硬编码 `_MAX_NAME + 7` | 改 `_MAX_NAME` 或哈希长度即碎。方向：从模块导入常量而非重算 |
| P2-22 | `check_auto_send` 的 check 之间存在顺序耦合 | `tools/check_auto_send.py:923` 附近 | 注释自认「造 Overlay 前先把配置复位」，即前一个 check 留下的 `_CONFIG` 会漏给后续。方向：每个 check 自建独立配置上下文 |

### 打包与分发

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-23 | `hiddenimports` 手工维护且已漏项 | `jev.spec:30-32` | 漏了 `app.recorder`、`app.textsim`、`core.judge`。目前靠静态分析能扫到，但既然已经在手工维护这份名单，漏项就是隐患。方向：补齐，或改用 `collect_submodules('app')` + `collect_submodules('core')` |
| P2-24 | `probe/` 与「只读」承诺不符 | `probe_win.py:109,114`、`probe_ocr_live.py:112` | `SetActive()` 抢焦点、把微信聊天截图写到 CWD（隐私，靠 `.gitignore` 兜）、`unminimize()` 改用户窗口 z-order。留在仓库里有复现价值（已确认**不进发布包**，`jev.spec` 只 `Analysis(["main.py"])`），但「跑一下看看」会真的动用户桌面。方向：docstring 改口径，或加 `--out` 显式确认 |
| P2-25 | `probe/` 里的 `find_wechat_hwnd` / `chat_area` / `who_said` 是 `app/` 逻辑的快照 | `probe/` 4 处 + `app/capture.py`、`app/ocr.py` | 已经漂移，容易误读。方向：文档标注「快照，非权威」 |

### 文案一致性

| # | 问题 | 位置 | 影响 / 方向 |
|---|---|---|---|
| P2-26 | 唯一的英文错误消息 | `jev_client.py:209-212`：`f"{env} is not set. Export it in the environment; do not put the key in a file."` | 这条会进状态栏和日志，而其余所有用户可见文案都是中文。方向：改中文 |

---

## 五、优先级汇总（可直接当待办用）

### 必须优先处理（P0）

1. **`config.json` 原子写入 + 读取失败不静默重置** — 数据丢失，用户无感知，且会被后续保存动作彻底覆盖。`settings.py:445-455` / `52-59`
2. **密钥明文注册表 + WM_SETTINGCHANGE 广播** — 与项目自己的安全规矩矛盾，用户不知情。`settings.py:316-327`
3. **CI 回归脚本清单改为发现式** — 否则新增测试永不执行，绿灯变假信号。`release.yml:88-89`
4. **修掉两个不可信的检查脚本** — `check_auto_send.py:550`（开发机必挂）+ `check_overlay_runtime.py:330`（恒真断言）

### 应该尽快（P1）

5. `overlay.py` 拆分；至少先做设置项的声明式字段表（P1-1）
6. `save()` / `analyze()` 改 dataclass 传参（P1-2）
7. `_cfg()` 加缓存 + 每轮生成快照（P1-3）
8. 抽 `tools/_checkutil.py`（P1-4）
9. `ocr` / `recorder` 去重加粗筛（P1-5）
10. `core/` 统一相对导入，去掉双份导入（P1-6）
11. 加 `uv.lock` 锁依赖（P1-7）
12. 修 `requirements.txt:6` 的错误注释（P1-8）

### 可以排期（P2）

13. 打包版加日志落盘（P2-11）——这一条建议提到 P1，因为它是「打包后出问题完全无法排查」的唯一原因
14. `main.py` 全局状态收进 `App` 类（P2-1）
15. 加 ruff 到 CI（P2-15）
16. 其余按上表逐条处理

---

## 六、一句话总结

**架构方向是对的，注释质量是超常的，问题集中在「持久化健壮性」「密钥存放」「UI 层结构」「测试可信度」四处。** 其中 P0 的 4 项都是「不修就会在某个时刻静默咬人」的类型——尤其 P0-1（配置全丢且无提示）和 P0-3（测试静默不跑），它们不会立刻报错，而是让问题在更晚、更难查的时候暴露。
