# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR 头部会话名和消息 → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str/datetime（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。"""
import ctypes
import time
import traceback

import numpy as np

from app.capture import Capture, chat_area, input_has_text, unminimize
from app.ocr import Reader, read_title, similar

_INPUT_POLL = 0.25  # 输入框最快 0.25 秒看一次。纯像素判断只要几毫秒，但没必要每帧都算


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


def run(q, hwnd, enabled, watch_input=None):
    """enabled 置位=采集，清掉=暂停。暂停时停掉 WGC 会话（Windows 那圈黄色采集边框也跟着没了），
    恢复时重开一个；readers 一直留着，去重状态不丢，恢复后不会把屏幕上的旧消息再报一遍。

    watch_input 置位时额外盯着输入框有没有内容（自动发送要发之前确认框是空的）。它跟 enabled 分开，
    是因为自动发送默认关着——没开的人不该白付这份计算。"""
    ctypes.windll.user32.SetProcessDPIAware()
    cap = None
    readers = {}  # {会话名: Reader}，一个会话一套去重状态
    title, head = "", None  # 当前会话名 / 上一帧的头部像素
    last_area = None  # 上次发给父进程的 4 元组，变了才再发一次
    warned = False  # 消息区识别失败是否已经报过，拖窗口时别每帧刷一条
    last_input, last_input_at = None, 0.0  # 上次报给父进程的「输入框有没有字」/ 上次检查的时刻
    while True:
        if not enabled.is_set():
            if cap is not None:
                cap.stop()
                cap = None
                q.put(("paused",))
            enabled.wait()
            continue
        if cap is None:
            try:
                cap = Capture(hwnd)
            except Exception as e:
                q.put(("dead", "无法开始采集：" + (" ".join(str(e).split())[:120] or type(e).__name__)))
                enabled.clear()  # 自己清掉，下一圈就去等着，别一秒重试几十次
                continue
            q.put(("resumed",))
        if not cap.alive():
            break
        try:
            unminimize(hwnd)
            full = cap.settled()
            if full is not None:
                area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好，只按尺寸变化算一次会锁死
                if area is None:
                    if not warned:
                        q.put(("status", "消息区认不出来（窗口太小？）"))
                        warned = True
                else:
                    warned = False
                    cap.area = area  # 采集线程拿它做 diff
                    x0, y0, x1, y1, bg, y_pane = area
                    rect = (x0, y0, x1, y1)
                    if rect != last_area:
                        q.put(("area", rect))
                        last_area = rect
                    crop = full[y_pane:y0, x0:x1]  # 头部：会话名在这里
                    if head is None or not np.array_equal(crop, head):  # 名字没动就别白跑一次 OCR
                        head = crop
                        name = read_title(crop)
                        # OCR 抖一下（「小分队」↔「小分认」）不能分裂出一个新会话
                        name = next((k for k in readers if similar(k, name)), name) if name else ""
                        # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
                        name = name or title or "当前会话"
                        if name != title:
                            title = name
                            q.put(("chat", title))
                    reader = readers.setdefault(title, Reader())
                    # new 是 [(who, name, text, ts)]，ts 是这条消息的聊天时间（datetime，可能为 None，
                    # 由父进程按「继承上一条」补）。它只给聊天记录导出用，分析那半不看它。
                    new = reader.new_lines(reader.read(full[y0:y1, x0:x1], bg))
                    if new:
                        q.put(("lines", title, new, rect))
            # 输入框有没有内容。**必须用 latest 帧**：消息区停稳才走上面那段，而用户打字时
            # 消息区一个像素都没变、settled() 根本不返回——拿停稳帧去判断只会永远看到「空」，
            # 那正好是最危险的误判方向（以为框是空的，一粘贴就把人家的草稿拼上去发了）。
            if watch_input is not None and watch_input.is_set():
                now = time.monotonic()
                frame, area = cap.latest, cap.area
                if frame is not None and area is not None and now - last_input_at >= _INPUT_POLL:
                    last_input_at = now
                    has = input_has_text(frame, area)
                    if has != last_input:
                        last_input = has
                        q.put(("input", has))
            else:
                last_input = None  # 关掉再开时重新报一次，父进程不该拿着旧状态做决定
        except Exception:
            _err(q)  # 一帧出错不退出
        time.sleep(0.05)
    q.put(("dead", "采集停了（微信关了？）"))
    try:
        cap.wait()  # 采集线程若是报错死的，这里把错抛出来
    except Exception:
        _err(q)
