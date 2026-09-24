# 设计说明（开发者向）

> 这份文档是 README 搬出来的内部说明：工作原理、为什么这么设计、每个模块的取舍、项目结构。
> 只想用这个工具的话，看 [README](../README.md) 就够了。

## 目录

- [工作原理](#工作原理)
- [模型](#模型)
- [为什么走 OCR](#为什么走-ocr)
- [为什么起草不那么像 AI](#为什么起草不那么像-ai)
- [项目结构](#项目结构)

## 工作原理

```
WGC 截微信窗口（GPU 合成窗口也能截，被遮挡也能截）
  → 像素锚点定位消息区（认底色和分隔线，不写死坐标，深浅主题通用）
  → OCR 面板头部的会话名当 key（头部像素没变就不重跑），记录、上下文、候选都按会话分开存
  → RapidOCR 只认消息区那一块
  → 按气泡颜色分 me / her，灰字（引用块、时间戳、群里的发言人名、链接卡片）过滤掉，
    发言人名摘出来挂到它下面那条消息上
  → 跟上一帧比，滚动翻出来的旧消息不重复上报
  → 冒出新的 her 消息才调 core.engine.analyze()
  → 起草 3 条候选（条数可在设置里调到 1~3）
  → 判断那一档（设置里选，或标题栏一键切）：
      完整：Jev 一次判断 + 排序 → 摘要 + 校准过的概率
      自判：拿你配的起草模型再调一次 → 摘要 + 排序（不给概率）
      不判断：跳过，起草 3 条就收工
  → 悬浮窗给判断摘要（如有）+ 3 条候选 → 点「填入微信」
  → 自动发送开着、且这道消息满足条件（单聊直接找我 / 群里 @我）：
      倒计时 N 秒（可取消）→ 再看一眼输入框确实是空的 → 按发送键 → 2.5 秒后回看，确认真的发出去了
```

截图和 OCR 跑在独立子进程里（一帧 OCR 250~800ms，放 Qt 主线程界面会僵），父进程只管界面和网络调用。

### 模型

| 环节 | 服务 | 默认模型 | key |
| --- | --- | --- | --- |
| 起草 3 条候选 | OpenRouter（默认） | `deepseek/deepseek-v4.1-flash` | `OPENROUTER_API_KEY` |
| 起草 3 条候选 | DeepSeek 直连（更快，可选） | `deepseek-flash`（DeepSeek-V4.1-Flash） | `DEEPSEEK_API_KEY` |
| 起草 3 条候选 | 自定义（任意 OpenAI 兼容地址，可选） | 自己填；留空用 `deepseek/deepseek-v4.1-flash` | `CUSTOM_API_KEY` |
| 判断 + 排序（完整模式） | OpenRouter（`/api/alpha/decisions`），**可选** | `typesafe/jev-1.13` | `OPENROUTER_API_KEY` |
| 判断 + 排序（自判模式） | 任意 OpenAI 兼容地址（`/chat/completions`） | 留空 = 跟起草同一个 | 留空 = 用起草那把，或 `JUDGE_API_KEY` |

起草走哪家在设置里选。判断那一步有两套完全不同的实现，选哪套在设置里定：

- **完整模式**走 OpenRouter 私有的 decisions 协议（`/api/alpha/decisions`），**没有替代品**。它给你
  **校准过的**概率，也就是卡片上那个百分比。
- **自判模式**走普通 `/chat/completions` + prompt 约束（`core/judge.py`）：模型能读对话、能选题、
  能把候选排序——这些它真做了，所以如实产出；但**它给的百分比是编的**，所以这一档只产出排序、
  `scores` 一律 `None`，界面因此不显示百分比。
- **不判断**：跳过这一步，返回 `judged=False`、`best_index=None`、`scores` 全 `None`，**不编排序**。

判断失败（不管哪一档）都**不会把候选一起丢掉**：如实返回 `judged=False` + `judge_error`（带状态码和
提示），3 条候选照常给你。

起草是**盲起草**——不把 Jev 的判断喂给它，让它自己读对话；7 道判断题加一道「哪条候选最合适」一次问完，
概率就是卡片上的百分比。温度 1.2，`max_tokens` 400；思考模式默认关，开了会带上思考开关、`max_tokens`
提到 4000（DeepSeek 把思考过程也算进去，400 会把答案截断）。模型只给出 1~2 条时会带着它的回答追问一次
补齐，还不够就按实际条数走（少于 2 条就不排序）。自判那一次用 `temperature: 0`——判断要可复现，不要发挥。

**地址和模型名都能改，不用动源码。** 设置页「起草模型来源」选「自定义（任意 OpenAI 兼容地址）」就能填
基础地址（填到 `/v1` 即可，程序自己补 `/chat/completions`）和模型名；判断那步另有一组「判断服务地址 /
判断模型」——**留空时的默认值跟着引擎变**：完整模式走 OpenRouter 官方端点，自判模式跟起草完全一样。
地址不是密钥，落在 `config.json`；`CUSTOM_API_KEY`、`JUDGE_API_KEY` 跟另外两个 key 一样只进注册表。
地址格式不对（不是 `http://` / `https://` 开头）保存会被拒；真到运行时不可用了，程序退回默认地址并写日志，
不会把整条链打死。

### 为什么走 OCR

微信 Windows 4.x（进程 `Weixin.exe`，窗口类 `Qt51514QWindowIcon`）界面自绘在一块 GPU 合成画布上
（`MMUIRenderSubWindowHW`）。UIA 树只有 2 个节点、**没有控件树**——`probe/probe_win.py`、
`probe/probe_win2.py` 实测证伪。

所以唯一干净的非侵入采集路 = 截自己的微信窗口 + 本地 OCR。离线、零 token。

### 为什么起草不那么像 AI

- system prompt 是中文写的反模板规则：不总结不复述、不解释自己为什么这么回、不用「首先/其次/总之」和
  「亲/您/加油哦」这类客套、不排比不凑三段式、句尾别习惯性加句号、允许不完整的句子和口头语、
  三条不是「温暖版/负责版/行动版」而是同一个人三个心情下随手打的（其中一条可以只有几个字）。
- 喂口吻样本：把你自己最近 12 条短消息原样给它，照着你的用词、句长、标点习惯写；设置里的
  「说话风格」再补一句你自己的描述。
- 收尾还做了清洗：剥掉编号、方括号、引号和照抄的「me:」前缀，去掉句尾句号（`？！～` 留着，那是语气）。


## 项目结构

```
main.py                 入口：父进程只管界面，子进程采集，队列传消息（IDE 直接 Run）
                        auto_pick()/start_auto()/auto_send_reply() 是自动发送的三道口径：
                        先按「该不该发」（开关 + @我 + 第一批）筛；判断开着用推荐位，判断关着/失败时
                        发第一条并在倒计时条说明；再按「界面对不对得上」筛，最后发送前逐条重新确认
                        （会话没切、输入框是空的）
                        sync_history() 管记录器的建/收（开关关着时它就是 None，连去重窗口都不占内存）；
                        drain() 里只多一个分叉：history 照旧存三元组喂模型，带时间戳的那一份另给记录器
app/                    UI + 采集层
  capture.py            找微信窗口 + WGC 盯帧 + 像素锚点定位消息区；帧全程内存。
                        另存一个 latest 帧，供输入框内容检测用（不能等 settled——你打字时消息区一像素不变）
  ocr.py                RapidOCR 读消息区 → 按颜色分 me/her/灰字 → 滚动去重；另读头部的会话名。
                        灰字那一路分两支：**居中**的当时间戳解析（parse_time：`14:15` / `昨天 14:15` /
                        `星期二 14:15` / `9月20日 14:15`，认不出一律返回 None，绝不猜），
                        **靠左的**才是群聊发言人名——老逻辑一个字没动。read()/new_lines() 的输出多带一个 ts，
                        但判重键仍是 (who, name, text) 三元组，跟改动前完全一样
  worker.py             采集子进程主循环（截图 → 定位 → OCR → 去重 → 丢队列）；另按 250ms 轮询
                        最新帧判断「输入框里有没有字」，只在状态变化时上报（不跑 OCR，纯像素）。
                        上报的新行是 4 元组 (who, name, text, ts)，ts 给记录器用，分析那半边不看它
  fill.py               填字 + 可选按发送键：写剪贴板 → 点输入框 → Ctrl+V（→ 可选 Enter / Ctrl+Enter）
  overlay.py            置顶悬浮窗：会话/回复对象、判断摘要、3 条候选、聊天记录、设置页（PySide6 + Fluent）
                        三档模式的界面全在这里：_rank() 是「怎么排、标不标推荐」的唯一口径（自判按模型给的
                        ranking 排、脏索引丢掉、漏排补齐，一条候选都不丢）；judged=False 时不标推荐、
                        不显示百分比、标签用「候选 N」；set_failed() 是失败收尾（恢复候选可点 + 展开日志面板）
                        自动发送的界面也在这里：begin_auto()/_auto_tick()/_cancel_auto() 是倒计时条，
                        _sync_auto_fields() 管两个开关的显隐联动，_sync_footer() 让页脚的承诺跟着开关变
  settings.py           四个 key 只进注册表，其余设置（含自定义地址/模型名/重试次数/判断引擎/自动发送/
                        两个超时/候选条数）落 config.json；draft_problem()/draft_ready() 是「起草能不能跑」
                        的唯一口径，main 和界面共用；judge_engine() 是「判断实际会走哪一档」的唯一口径
                        （含运行时降级），stored_judge_engine() 是「用户选了什么」——降级不写回文件，
                        否则补上密钥也回不去完整模式；save_judge_engine() 只改判断那一个键，给标题栏
                        那个一键开关用（走 save() 的二十来个形参极易漏项、把用户设置清成默认）；
                        retries() 是重试次数的唯一口径（0~5，脏值退默认），界面和 core 共用；
                        candidate_count()/auto_send_on()/auto_send_delay()/send_key()/my_name()/
                        draft_timeout()/judge_timeout() 同样是各自那格设置的唯一口径（夹取 + 脏值退默认）；
                        save_history()/history_dir() 是聊天记录导出的开关和落盘位置（程序目录下，
                        不用 os.getcwd()——双击 exe 时那个可能是桌面）
  recorder.py           聊天记录导出（**不依赖 Qt、不联网**）：按「会话名/日期.csv」分层落盘，
                        四列 时间/方向/发送者/内容；最近 50 条内查重（含模糊匹配，OCR 抖动算同一条），
                        跨运行读回最近 2 个 CSV 的尾部接着查（子进程重启整屏重报只有这层挡得住）；
                        攒 20 条或 5 秒落盘，退出再 flush。**写盘失败只记进 problems、绝不外抛**——
                        记录是旁路，拖垮采集和自动发送才是真事故
  textsim.py            文本相似度（similar），**只依赖标准库**：ocr.py（子进程）和 recorder.py（主进程）
                        都要用它，留在 ocr.py 里会让主进程为了比两个字符串就 import 40MB 的 RapidOCR
core/                   Jev 判断内核，平台无关，跟安卓原版同一套口径
  engine.py             唯一入口 analyze(...) → 候选 + 判断；按 judge_engine 分三档路由，
                        判断失败时如实返回 judged=False + judge_error，但**候选照常给**（不连候选一起丢）
  judge.py              自判模式：走普通 /chat/completions + prompt 约束，用任意 OpenAI 兼容模型做判断和排序。
                        **只产出排序、不产出概率**（scores 一律 None）——自评的百分比是编的，给出来就是假信息；
                        temperature=0（判断要可复现）；出网细节全走 jev_client；带 __main__ 自测
  jev_client.py         Jev 判断 API 客户端（stdlib、脱敏）；**所有出网的唯一入口**：端点归一化、
                        请求头（request_headers：含必须显式带的 User-Agent）、状态码提示表、
                        重试策略（retryable_status + post_json）、响应体形状防御（parse_json）都在这，
                        起草和判断共用；JevError 带 status/hint/retries 三个字段供状态栏用；带 __main__ 自测
  questions.py          7 道判断题 + build_state() + build_rank_question()
  draft.py              起草 3 条候选（OpenRouter / DeepSeek 直连 / 自定义 OpenAI 兼容地址）；
                        _content() 把响应体形状错误转成带 hint 的 JevError，不让 KeyError 逃出去
tools/
  demo.py               端到端冒烟：拿一段写死的对话跑完整链（需 key + 联网）
  跑全部离线检查（7 份，全部通过才 exit 0）：
    for f in check_auto_send check_draft_mode check_recorder check_retry check_self_judge \
             check_ui_layout check_overlay_runtime; do
      python tools/$f.py || echo "FAIL $f"; done
    Qt 那两份需要 QT_QPA_PLATFORM=offscreen（无显示器时）。
  preview_ui.py         用合成数据预览界面，不采集不联网不碰微信；--screenshot 出图，
                        --judge-engine / --judge-error / --scroll-bottom 能把三档和判断失败那一种界面都截出来，
                        --state auto / --auto-send 能把自动发送和倒计时条截出来
  make_icon.py          生成 docs/icon.ico（打包图标），图标已提交，换颜色才用重跑
  check_ui_layout.py    静态检查 overlay.py 里有没有「构造了控件但忘了加进布局」的属性
                        （Qt 里这种错是静默的：控件会变成飘在桌面上的顶层窗口，装不了 Qt 时只能静态查）
  check_overlay_runtime.py  把 overlay.py 真跑起来验证（离屏，不需要显示器）：重试次数控件真在布局里、
                        读写闭环、值域夹取、set_failed() 的恢复/展开时序、三档模式的卡片标签与排序、
                        判断失败那一种渲染、说明文字里没有漏出来的 markdown 标记、设置页在 320/400/440/640
                        四个宽度下内容不溢出、标题栏「判断」开关的回调与防回环、候选条数控件、群昵称警告三态。
                        比静态检查强——静态查不出「值域写错」「忘了读写」「时序反了」。需要 PySide6
  check_draft_mode.py   三档模式（完整 / 自判 / 起草）的离线回归：engine 按 judge_engine 分流、
                        自判一律不给概率、判断失败不丢候选、overlay._rank 的三种排序口径（含 ranking 去重和
                        补齐）、settings.draft_problem() 判定、judge_engine() 的降级与「存值不被污染」、
                        candidate_count 真透传到 draft_candidates。不需要 Qt、不联网
  check_self_judge.py   自判模式端到端：起一个本地桩服务，第 1 次调用回候选、第 2 次回判断 JSON，
                        检查真的调了两次、只走 /chat/completions、判断 prompt 里真带了题目和候选、
                        temperature=0、scores 全 None、判断失败不丢候选、坏格式真的重试到用尽。
                        不需要 Qt、不出网（只连回环）
  check_retry.py        重试语义端到端：起一个真会失败的本地 HTTP 服务，数它被打了几次——
                        该重试的（5xx/408/429/掐线/响应体格式不对）真重试、不该重试的（4xx）一次都不打、
                        退避序列是 1/2/4/4、**超时只打 1 次且零退避等待**（起一个真会拖延的服务测）、
                        设置里的次数真透传到了起草那一路。不需要 Qt、不联网
  check_auto_send.py    自动发送九组离线回归：输入框内容检测（灰字占位符 vs 真打的字 vs 光标）、
                        _at_me() 文本匹配、auto_pick() 的十几种该发/不该发组合、设置的默认值与夹取
                        （含脏值和往返读写）、倒计时走路与八种取消时机、设置页字段显隐与页脚承诺、
                        main 两道门禁（start_auto / auto_send_reply）。不需要 Qt、不联网
  check_recorder.py     聊天记录导出离线回归：parse_time 的十几种写法与「认不出就返回 None」、
                        Reader 灰字两支分流（时间戳不能顶掉发言人名、ts 不进判重键）、
                        safe_name 转义（非法字符/保留名/超长带哈希）、四列与「程序」方向、
                        去重（窗口内 / 模糊 / 跨运行 / 窗口外照记）、时间继承与墙钟兜底、
                        跨天按消息真实时间分文件、BOM 只写一次、写盘失败不抛且留待重试、
                        定时与批量 flush、main.drain() 的分叉、main.sync_history() 的建/收与收尾提示、
                        settings 开关的默认值与落盘。
                        全部写在临时目录里，跑完删掉，**绝不碰真实配置和真实聊天记录**；不需要 Qt、不联网
probe/                  一次性探针，结论已写进本文，留着是为了可复现
  probe_win.py          UIA 能不能读微信聊天文字 → 证伪（树是空的）
  probe_win2.py         UIA 证伪 v2：分清「树是空的」和「有树没文字」，顺带试 LegacyIAccessible
  probe_notify.py       微信来消息走不走 Windows 通知平台（能监听到就零 OCR）
  probe_ocr.py          OCR 读不读得准中文气泡、左右说话人分不分得开
  probe_ocr_speed.py    RapidOCR 一帧多久、裁小能快多少（结论：det_limit_type 必须 'max'）
  probe_ocr_live.py     WGC 持续盯窗口 + 变了就 OCR，新文字实时打控制台
  probe_printwindow.py  试 PrintWindow + PW_RENDERFULLCONTENT 能不能绕开 Win10 黄框（未验证）
jev.spec                PyInstaller 打包定义（onedir），build.bat 和 CI 共用这一份
build.bat               本地一键打包（双击就行）
.github/workflows/release.yml  推 v* tag → windows-latest 上打包 → zip 挂到 Release
requirements.txt        依赖（纯 ASCII 注释：中文 Windows 上 pip 按 GBK 读会炸）
docs/KICKOFF.md         最初的需求和硬约束说明
docs/icon.ico           程序图标，tools/make_icon.py 生成
docs/ui_*.png           README 里那几张截图，tools/preview_ui.py --screenshot 出的
                        （ui_self_judge.png / ui_judge_engine.png 要配 --judge-engine self
                         和 --scroll-bottom 才出得来，见 preview_ui.py 的 docstring）
config.json             你自己的设置，不进仓库（在 .gitignore 里）
聊天记录/                开了「保存聊天记录到本地」才会出现，按「会话名/日期.csv」分层。
                        是你自己的对话内容，**不进仓库**（也在 .gitignore 里）
```

`tools/` 和 `probe/` 里的脚本都按「项目根在 `PYTHONPATH` 里」写（PyCharm 默认会把内容根加进去）。
命令行跑 `tools/demo.py` 得自己带上：`set PYTHONPATH=. && python tools/demo.py`。

自己把项目根插进 `sys.path` 的那几个（`check_auto_send` / `check_draft_mode` / `check_overlay_runtime` /
`check_recorder` / `check_retry` / `check_self_judge` / `preview_ui`）：
命令行直接跑就行，回归检查和看界面不该还要你先想起来设环境变量
（`check_self_judge.py` 另外还自带一个本地桩服务）。`check_ui_layout.py`、`demo.py`、`make_icon.py`
不走这条路，得自己带上 `PYTHONPATH`。`app/`、`core/`、`main.py` 里没有任何 `sys.path` 补丁。

