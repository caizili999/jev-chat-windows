# -*- coding: utf-8 -*-
"""知识库界面里那两段文本解析（对齐上游 KnowledgeActivity 的 splitTags / importNotesDialog）。

**不依赖 Qt**，所以能单独离线测（见 tools/check_kb.py）——它们是为界面服务的纯函数，
但都是「输入一段用户手打的文本、输出结构化数据」，恰恰是最该被钉住的那一类。
"""


def split_tags(raw) -> list:
    """标签输入框 → 标签列表。

    中英文逗号、顿号都当分隔符（用户不会在意自己打的是哪个），逐项 trim，空的丢掉。
    去重但**保持原顺序**——界面上显示的顺序该跟用户写的一致。
    """
    if not raw:
        return []
    out = []
    for part in str(raw).replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def parse_import_blocks(raw) -> list:
    """「从文本导入」的多行文本 → [(标题, 正文)]。

    规则跟上游一致：**按空行分段**，每段的第一行当标题，其余行拼成正文。
    段内空行不留（已经当分段符用了），每行 trim。

    只有空白的内容整段丢掉；一段只有一行时正文是空串（合法——标题本身就是一条笔记）。
    """
    if not raw:
        return []
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for chunk in _split_blank_lines(text):
        lines = [ln.strip() for ln in chunk.split("\n")]
        lines = [ln for ln in lines if ln]
        if lines:
            out.append((lines[0], "\n".join(lines[1:])))
    return out


def _split_blank_lines(text: str) -> list:
    """按「一个或多个空行」切段。空行允许夹着空格/制表符。

    手写而不是用正则：`re.split` 在连续多个空行上会产生空段，还要再滤一遍；
    这里一次扫描，顺手把行尾空白也处理掉，逻辑比正则好读。
    """
    chunks, current = [], []
    for line in text.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            chunks.append("\n".join(current))
            current = []
    if current:
        chunks.append("\n".join(current))
    return chunks
