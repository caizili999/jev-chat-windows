# -*- coding: utf-8 -*-
"""文本相似度判断。**只依赖标准库**，这一点是刻意的。

它同时被 app/ocr.py（子进程里的 OCR 去重）和 app/recorder.py（主进程里的聊天记录去重）用。
如果留在 app/ocr.py，主进程为了比较两个字符串就得 import rapidocr_onnxruntime——那是约 40MB
模型 + 几百毫秒启动，白付。
"""
import difflib


def similar(a, b):
    """同一段像素挪个位置 OCR 会抖（「傻逼了」↔「傻逼」、「不好意思」↔「不好竟思」），按相似度判同一条。"""
    if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) <= 1  # 短句错一个字
