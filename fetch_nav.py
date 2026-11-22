#!/usr/bin/env python3
"""
从「TvBox 资源导航」(tvbox.shenzjd.com) 采集地址清单。

设计前提：只取 URL 与页面已标注的探活结果，不解析目标 JSON 的内容。
也就是说，本脚本产出的是一笔"网址连接"，不是一个合并后的配置源。

页面是 Next.js App Router，数据内嵌在 RSC 流里：
    <script>self.__next_f.push([1,"..."])</script>
每段的字符串是 JS 转义过的 JSON 片段，拼起来就是服务端渲染的原始数组。
从 RSC 取数比解析 DOM 稳定得多 —— 站点改版只要数据结构不变就不会失效。

产出（output/ 根目录，publish.py 会自动带上）：
    nav-live.txt   直播源地址，m3u / txt，按延迟升序
    nav-api.txt    TVbox 接口地址，按延迟升序
    nav.json       全量元数据，含探活时间与反馈票数
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import httpx
except ImportError:  # 本机可能没装，退回标准库，容器里仍走 httpx
    httpx = None

NAV = "https://tvbox.shenzjd.com/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OUT = Path(__file__).resolve().parent / "output"

# 条目数低于这个值就认为抓取异常，宁可报错也不写出残缺清单
MIN_EXPECTED = 50


def fetch_html() -> str:
    if httpx is None:
        import urllib.request
        req = urllib.request.Request(NAV, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(NAV)
        r.raise_for_status()
        return r.text


def unescape_rsc(html: str) -> str:
    """把 self.__next_f.push([1,"..."]) 的所有片段拼回完整文本。"""
    parts = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    buf = []
    for p in parts:
        try:
            buf.append(json.loads('"' + p + '"'))
        except json.JSONDecodeError:
            continue  # 单段坏了不影响其余片段
    return "".join(buf)


def extract_rows(text: str) -> list[dict]:
    """从 RSC 文本里找出所有以 [{"id": 开头的条目数组。"""
    dec = json.JSONDecoder()
    rows: list[dict] = []
    i = 0
    while True:
        i = text.find('[{"id":', i)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, list) and obj and "latency_ms" in obj[0]:
                rows.extend(obj)
            i = end
        except json.JSONDecodeError:
            i += 1
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """同一地址可能以多个名字重复收录，按 url 归一化保留延迟最低的那条。"""
    best: dict[str, dict] = {}
    for r in rows:
        u = (r.get("url") or "").strip()
        if not u:
            continue
        cur = best.get(u)
        if cur is None or (r.get("latency_ms") or 9e9) < (cur.get("latency_ms") or 9e9):
            best[u] = r
    return list(best.values())


def sort_key(r: dict):
    """可用的排前面，其次按延迟升序，最后按名字。"""
    return (0 if r.get("status") == "ok" else 1,
            r.get("latency_ms") if r.get("latency_ms") is not None else 9e9,
            r.get("name") or "")


def write(name: str, text: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text(text, encoding="utf-8")
    print("  写出 %-16s %6.1f KB" % (name, p.stat().st_size / 1024))


def main() -> int:
    global OUT
    ap = argparse.ArgumentParser(description="采集 TVbox 资源导航站的地址清单")
    ap.add_argument("--outdir", default=str(OUT),
                    help="输出目录，默认仓库下的 output/")
    args = ap.parse_args()
    OUT = Path(args.outdir)

    print("① 抓取 %s" % NAV)
    try:
        html = fetch_html()
    except Exception as e:
        print("  失败：%s" % e)
        return 1
    print("  页面 %.1f KB" % (len(html) / 1024))

    print("② 解包 RSC 数据")
    text = unescape_rsc(html)
    rows = dedupe(extract_rows(text))
    if len(rows) < MIN_EXPECTED:
        print("  只拿到 %d 条，低于阈值 %d，判定为抓取异常，中止" %
              (len(rows), MIN_EXPECTED))
        return 2
    print("  去重后 %d 条" % len(rows))

    by_cat = Counter((r.get("category") or "?") for r in rows)
    ok_by_cat = Counter((r.get("category") or "?")
                        for r in rows if r.get("status") == "ok")
    print("  分类：" + "，".join(
        "%s %d 条（可用 %d）" % (k, v, ok_by_cat.get(k, 0))
        for k, v in by_cat.most_common()))

    print("③ 输出")
    live = sorted((r for r in rows if r.get("category") == "live"), key=sort_key)
    api = sorted((r for r in rows if r.get("category") == "api"), key=sort_key)

    write("nav-live.txt", "".join((r["url"].strip() + "\n") for r in live))
    write("nav-api.txt", "".join((r["url"].strip() + "\n") for r in api))

    meta = {
        "generated_at": __import__("datetime").datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"),
        "source": NAV,
        "total": len(rows),
        "live": len(live),
        "api": len(api),
        "ok": sum(1 for r in rows if r.get("status") == "ok"),
        "items": rows,
    }
    write("nav.json", json.dumps(meta, ensure_ascii=False, indent=1))

    ok_live = [r for r in live if r.get("status") == "ok"]
    fast = [r for r in ok_live if (r.get("latency_ms") or 9e9) <= 300]
    print("  直播源 %d 条，可用 %d，其中 ≤300ms 的 %d 条" %
          (len(live), len(ok_live), len(fast)))
    print("  接口   %d 条，可用 %d" %
          (len(api), sum(1 for r in api if r.get("status") == "ok")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
