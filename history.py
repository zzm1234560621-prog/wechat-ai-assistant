"""加载并检索导出的聊天记录（统一 JSONL 格式）。

每行一个 JSON 对象，字段（兼容多种命名）：
  content / text / msg  -> 消息文本
  sender                -> 发送者（wxid 或昵称）
  time                  -> 时间
  talker / room         -> 会话 id（群则带 @chatroom）
"""
import json
import os
import re


class HistoryStore:
    def __init__(self, path):
        self.messages = []
        self._load(path)

    def _load(self, path):
        if not path or not os.path.exists(path):
            print(f"[history] 未找到 {path}，检索功能暂时为空。可先运行 export_history.py 导出。")
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        print(f"[history] 已加载 {len(self.messages)} 条消息")

    @staticmethod
    def text(m):
        return str(m.get("content") or m.get("text") or m.get("msg") or "")

    def search(self, query, k=8):
        """朴素关键词打分检索：按命中次数排序返回 top-k。"""
        terms = [t for t in re.split(r"[\s,，。！？!?]+", query) if t]
        if not terms:
            terms = [query]
        scored = []
        for m in self.messages:
            t = self.text(m)
            s = sum(t.count(term) for term in terms if term and term in t)
            if s > 0:
                scored.append((s, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:k]]

    def recent(self, n=30):
        return self.messages[-n:]
