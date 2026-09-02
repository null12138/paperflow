"""高校 WebVPN 学校数据库。

数据来源：https://github.com/Rimagination/scansci-pdf （Apache-2.0）
``paperflow/data/webvpn.json`` 收录 100+ 所高校的 WebVPN 入口与（部分）加密密钥。

WebVPN 协议要点（桑弧/圣博润类系统）：
  * 目标 URL 的 hostname 用 AES-128-CFB 加密（segment 128bit），密钥默认
    ``wrdvpnisthebest!``（部分学校自定义，见 ``crypto_key``/``crypto_iv``）；
  * 加密结果以 ``hex(iv)+hex(密文)`` 形式拼进 URL：
    ``https://<webvpn-host>/https/<hex(iv)+hex(hostname密文)><原始path>``；
  * 登录为机构 CAS/SSO，登录态保存在 cookie 中，复用 cookie 即复用机构权限。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_KEY = b"wrdvpnisthebest!"
_DATA_FILE = Path(__file__).resolve().parent / "data" / "webvpn.json"
_cache: list["SchoolEntry"] | None = None


@dataclass
class SchoolEntry:
    name: str
    province: str
    host: str        # WebVPN 入口，如 https://webvpn.pku.edu.cn
    key: bytes       # AES 密钥（16/24/32 字节）
    iv: bytes        # AES IV（默认等于 key）
    school_type: str = "webvpn"  # "webvpn"=桑弧 AES 转发；"sangfor"=Array/深信服 SSL VPN 门户
    gateway: str = ""


def _load_db() -> dict:
    if not _DATA_FILE.exists():
        return {}
    try:
        return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_entry(name: str, province: str, info: dict) -> SchoolEntry | None:
    host = (info.get("host") or "").strip()
    if not host:
        return None
    if not host.startswith("http"):
        host = f"https://{host}"
    key_str = info.get("crypto_key", "")
    iv_str = info.get("crypto_iv", "")
    key = key_str.encode("utf-8") if key_str else DEFAULT_KEY
    iv = iv_str.encode("utf-8") if iv_str else key
    return SchoolEntry(name=name, province=province, host=host.rstrip("/"),
                       key=key, iv=iv,
                       school_type=info.get("type", "webvpn"),
                       gateway=info.get("gateway", ""))


def list_schools() -> list[SchoolEntry]:
    global _cache
    if _cache is not None:
        return _cache
    result: list[SchoolEntry] = []
    for province, schools in _load_db().items():
        for name, info in schools.items():
            entry = _parse_entry(name, province, info)
            if entry:
                result.append(entry)
    _cache = result
    return result


def search_schools(query: str) -> list[SchoolEntry]:
    q = query.casefold()
    return [e for e in list_schools()
            if q in e.name.casefold() or q in e.province.casefold()
            or q in e.host.casefold()]


def get_school(name: str) -> SchoolEntry:
    """按学校名精确/模糊查找；唯一命中或最短名前缀命中时返回。"""
    schools = list_schools()
    for s in schools:
        if s.name == name:
            return s
    matches = [s for s in schools if name in s.name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        matches.sort(key=lambda s: len(s.name))
        return matches[0]
    for s in schools:
        if s.name in name:
            return s
    raise ValueError(
        f"未找到学校 '{name}'。可用 search_schools / `paperflow auth webvpn-list` 查看。")