#!/usr/bin/env python3
"""通过 Kimi WebBridge 和 Edge 校园会话，将 WOS Full Record 导出到本地。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests


BRIDGE_URL = "http://127.0.0.1:10086/command"
SESSION = "wos-species-download"
WOS_URL = "https://webofscience.clarivate.cn/wos/woscc/basic-search"
HTTP = requests.Session()
HTTP.trust_env = False  # localhost 桥接不能经过系统 HTTP(S) 代理


class BridgeError(RuntimeError):
    pass


def command(action: str, args: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
    body = {"action": action, "args": args or {}, "session": SESSION}
    response = None
    for attempt in range(3):
        try:
            response = HTTP.post(BRIDGE_URL, json=body, timeout=timeout)
            if response.status_code < 500:
                break
        except requests.ConnectionError:
            if attempt == 0:
                subprocess.run([str(Path.home() / ".kimi-webbridge/bin/kimi-webbridge"), "start"], check=True)
        time.sleep(1 + attempt)
    if response is None:
        raise BridgeError("无法连接 Kimi WebBridge")
    if response.status_code >= 400:
        raise BridgeError(f"WebBridge HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not payload.get("ok"):
        message = (payload.get("error") or {}).get("message", str(payload))
        raise BridgeError(message)
    return payload.get("data") or {}


def evaluate(code: str) -> Any:
    return command("evaluate", {"code": code}).get("value")


def wait_until(js_condition: str, description: str, timeout: int = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if evaluate(f"(() => Boolean({js_condition}))()"):
                return
        except BridgeError:
            pass
        time.sleep(0.75)
    raise BridgeError(f"等待超时：{description}")


def js_click_by_text(selector: str, exact_text: str) -> None:
    selector_json = json.dumps(selector)
    text_json = json.dumps(exact_text)
    clicked = evaluate(
        f"(() => {{ const e=Array.from(document.querySelectorAll({selector_json}))"
        f".find(x=>x.innerText.trim()==={text_json}); if(!e)return false; e.click(); return true; }})()"
    )
    if not clicked:
        raise BridgeError(f"找不到控件：{exact_text}")


def fill_input(selector: str, value: str) -> None:
    selector_json = json.dumps(selector)
    value_json = json.dumps(value)
    result = evaluate(
        f"(() => {{ const e=document.querySelector({selector_json}); if(!e)return null;"
        "const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
        f"s.call(e,{value_json});e.dispatchEvent(new Event('input',{{bubbles:true}}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));return e.value; })()"
    )
    if result != value:
        raise BridgeError(f"无法填写输入框：{selector}")


def click_css(selector: str) -> None:
    selector_json = json.dumps(selector)
    if not evaluate(f"(() => {{ const e=document.querySelector({selector_json}); if(!e)return false;e.click();return true; }})()"):
        raise BridgeError(f"找不到控件：{selector}")


def open_search_page() -> None:
    tabs = command("list_tabs").get("tabs", [])
    existing = [
        tab for tab in tabs
        if "webofscience.clarivate.cn/wos/" in tab.get("url", "")
        and "/error" not in tab.get("url", "")
    ]
    if existing:
        command("find_tab", {"url": existing[-1]["url"]})
        command("navigate", {"url": WOS_URL})
    else:
        command("navigate", {"url": WOS_URL, "newTab": True, "group_title": "WOS 物种论文导出"})
    wait_until("document.querySelector('input[aria-label^=\"Search box 1 Topic\"]')", "WOS Topic 输入框")
    institution = evaluate("(() => document.body.innerText.includes('Capital Normal University'))()")
    if not institution:
        raise BridgeError("未检测到 Capital Normal University 机构访问，请先在 Edge 进入校园 WOS")


def search_species(species: str) -> int:
    fill_input("input[aria-label^='Search box 1 Topic']", f'"{species}"')
    clicked = evaluate(
        "(() => { const form=document.querySelector('input[aria-label^=\"Search box 1 Topic\"]')?.closest('form');"
        "const b=Array.from(form?.querySelectorAll('button')||[]).find(x=>x.innerText.includes('Search'));"
        "if(!b)return false;b.click();return true; })()"
    )
    if not clicked:
        raise BridgeError("无法点击 WOS Search")
    wait_until("location.pathname.includes('/summary/')", "WOS 结果页", 60)
    wait_until("/results? from Web of Science/.test(document.body.innerText)", "WOS 结果数量", 60)
    title = evaluate("document.title") or ""
    match = re.search(r"–\s*([\d,]+)\s*–", title)
    if not match:
        match = re.search(r"([\d,]+) results from Web of Science", evaluate("document.body.innerText") or "")
    return int(match.group(1).replace(",", "")) if match else 0


def open_export_dialog() -> None:
    wait_until("document.querySelector('#export-trigger-btn')", "Export 按钮")
    evaluate("document.querySelector('#export-trigger-btn').click(); true")
    wait_until("Array.from(document.querySelectorAll('[role=menuitem]')).some(x=>x.innerText.trim()==='Plain text file')", "导出菜单")
    js_click_by_text("[role=menuitem]", "Plain text file")
    wait_until("location.href.includes('overlay:export/exp')", "Plain text 导出窗口")


def export_batch(start: int, end: int) -> str:
    open_export_dialog()
    evaluate("document.querySelectorAll('input[type=radio]')[1]?.click(); true")
    fill_input("input[aria-label^='Input starting record range']", str(start))
    fill_input("input[aria-label^='Input ending record range']", str(end))
    click_css("[role=combobox][aria-label^='Filter by']")
    wait_until("Array.from(document.querySelectorAll('[role=option]')).some(x=>x.innerText.trim()==='Full Record')", "Full Record 选项")
    js_click_by_text("[role=option]", "Full Record")
    command("network", {"cmd": "start"})
    js_click_by_text("button", "Export")
    request_id = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        data = command("network", {"cmd": "list", "filter": "saveToFile"})
        completed = [r for r in data.get("requests", []) if r.get("completed") and r.get("status") == 200]
        if completed:
            request_id = completed[-1]["requestId"]
            break
        time.sleep(1)
    if not request_id:
        command("network", {"cmd": "stop"})
        raise BridgeError(f"WOS 导出 {start}-{end} 未返回文件")
    detail = command("network", {"cmd": "detail", "requestId": request_id})
    command("network", {"cmd": "stop"})
    body = detail.get("body", "")
    if not body.startswith("FN Clarivate"):
        raise BridgeError(f"WOS 导出 {start}-{end} 内容格式异常")
    return body


def safe_stem(species: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", species).strip("_") or "species"


def load_species(path: Path) -> list[str]:
    return list(dict.fromkeys(
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("input.txt"))
    parser.add_argument("--output-dir", type=Path, default=Path("wos_exports"))
    parser.add_argument("--max-records", type=int, default=0,
                        help="每个物种最多导出多少条；0 表示全部")
    args = parser.parse_args()
    species_names = load_species(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    exports = []
    open_search_page()
    for index, species in enumerate(species_names, 1):
        if index > 1:
            command("navigate", {"url": WOS_URL})
            wait_until("document.querySelector('input[aria-label^=\"Search box 1 Topic\"]')", "WOS Topic 输入框")
        print(f"WOS 检索 [{index}/{len(species_names)}]：{species}")
        total = search_species(species)
        wanted = min(total, args.max_records) if args.max_records else total
        chunks = []
        for start in range(1, wanted + 1, 1000):
            end = min(start + 999, wanted)
            print(f"  导出 {start}-{end} / {total}")
            chunks.append(export_batch(start, end).rstrip("\n"))
            time.sleep(2)
        filename = safe_stem(species) + ".txt"
        (args.output_dir / filename).write_text("\n".join(chunks) + "\n", encoding="utf-8")
        exports.append({"species": species, "file": filename, "records": wanted, "wos_total": total})
    (args.output_dir / "manifest.json").write_text(
        json.dumps({"exports": exports}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"完成：WOS 导出目录 {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
