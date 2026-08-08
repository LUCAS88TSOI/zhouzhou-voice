"""
州州語音 - 敏感資訊遮蔽

API Key 會透過三條路徑洩漏進 `%APPDATA%\\zhouzhou-voice\\logs\\app.log` 與設定頁：

1. 金鑰放在 URL query（`?key=...`）時，urllib3 的例外訊息含完整 request_uri
2. 使用者把整條含 query 的 URL 貼進「API URL」欄位（該欄位不是密碼框）
3. 伺服器回應 body 原樣回顯金鑰

使用者回報 bug 時把 app.log 貼上 GitHub issue，等同公開送出可計費的金鑰。
所有會寫進 log、顯示在 UI、或 raise 給上層的訊息都必須先過這裡。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

# 短金鑰（自架 Ollama / LM Studio / one-api 常見 sk-1234、test）洩漏後
# 一樣可用，門檻只需擋住空字串與單字元造成整段訊息被打爛。
_MIN_SECRET_LEN = 3

# URL query 中常見的金鑰參數名
_KEY_PARAM_RE = re.compile(
    r"(?i)\b(api[-_]?key|key|access[-_]?token|token|password|secret)=[^&\s\"']+"
)


def redact(message: str, secret: str | None) -> str:
    """把訊息中的金鑰換成 [REDACTED]。"""
    if not message:
        return message
    if secret and len(secret) >= _MIN_SECRET_LEN:
        message = message.replace(secret, REDACTED)
    # 即使不知道金鑰值（例如使用者把 key 貼進了 API URL 欄位），
    # 也要把 URL query 裡看起來像金鑰的參數一併遮掉
    return _KEY_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", message)


def safe_url(url: str) -> str:
    """
    移除 URL 的 query 與 userinfo，供 log／UI 顯示。

    使用者常把文件上整條含 `?key=...` 的 URL 貼進「API URL」欄位，
    直接記 endpoint 就等於把金鑰寫進 log。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED

    netloc = parts.netloc
    if "@" in netloc:  # 剝掉 user:pass@
        netloc = netloc.rsplit("@", 1)[-1]

    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
