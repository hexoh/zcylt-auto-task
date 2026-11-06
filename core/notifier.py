"""通知推送：任务完成 / 异常时提醒（企业微信机器人、Server 酱、自定义 webhook）。

只用标准库 urllib，避免为一个 POST 引入 requests。
推送失败只记日志，绝不影响主流程。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from typing import Optional

TYPES = ("none", "wecom", "serverchan", "webhook")

TYPE_LABELS = {
    "none": "关闭",
    "wecom": "企业微信机器人",
    "serverchan": "Server 酱",
    "webhook": "自定义 Webhook",
}


class Notifier:
    def __init__(self, config: Optional[dict] = None, logger=None):
        config = dict(config or {})
        self.kind = str(config.get("type") or "none").lower()
        self.url = str(config.get("url") or "").strip()
        self.sendkey = str(config.get("sendkey") or "").strip()
        self.timeout = float(config.get("timeout") or 8)
        self.logger = logger

    @property
    def enabled(self) -> bool:
        if self.kind == "none":
            return False
        if self.kind == "serverchan":
            return bool(self.sendkey)
        return bool(self.url)

    def _log(self, msg: str, level: str = "info") -> None:
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg)

    def send(self, title: str, content: str) -> bool:
        if not self.enabled:
            return False
        try:
            self._post(title, content)
            self._log(f"通知已推送：{title}", "debug")
            return True
        except Exception as exc:
            self._log(f"通知推送失败：{exc}", "warn")
            return False

    def send_async(self, title: str, content: str) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self.send, args=(title, content), daemon=True).start()

    def _post(self, title: str, content: str) -> None:
        if self.kind == "wecom":
            body = {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}
            self._request_json(self.url, body)
        elif self.kind == "serverchan":
            params = urllib.parse.urlencode({"title": title, "desp": content})
            url = f"https://sctapi.ftqq.com/{self.sendkey}.send?{params}"
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                resp.read()
        elif self.kind == "webhook":
            self._request_json(self.url, {"title": title, "content": content})
        else:
            raise ValueError(f"未知通知类型：{self.kind}")

    def _request_json(self, url: str, payload: dict) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"通知地址必须是 http(s)：{url!r}")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
