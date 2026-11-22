#!/usr/bin/env python3
"""
书源 / 音源的健康检查与合并。

三家源，选的都是"自身就在做校验"的项目 —— 这一点很关键：
上游肯把自己的失效清单公开，等于替我们挡掉了大半脏数据。

    tickmao/Novel     书源，严格验证 260 + 兼容保留 65，主动移除成人源
    XIU2/Yuedu        书源，22 条精选，作者用分组标注了"人机验证/暂不可用"
    hebijunge         音源，57 个插件，主流平台覆盖

只做三件事：拉、验、比。不发语义探测（不真的去搜一本书），
因为那需要逐条构造请求，成本高且容易被站方判为爬虫。

三层校验：
    传输层  状态码、内容长度
    结构层  能否解析、必备字段是否齐备
    数量层  与上次基线对比，掉超过阈值就报警

产出（output/nav/）：
    book-source.json   合并去重后的书源，可直接导入阅读 App
    music-source.json  合并后的音源插件清单
    nav-health.txt     只列异常，正常的不出现
    baseline.json      本次快照，供下次比对
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

OUT = Path(__file__).resolve().parent / "output" / "nav"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 数量比基线掉超过这个比例就报警
SHRINK_ALERT = 0.30

# 各类源的自然时效不同，更新周期分开定。
# 书源是人工整理的规则，站点改版才会变，一周一次足够；
# 音源插件跟平台接口走，失效快一些，三天一次。
# 直播源不在这里，由 fetch_nav.py 每天抓。
KIND_EVERY = {
    "book": 7,
    "music": 3,
}

SOURCES = [
    {
        "name": "tickmao 严格验证书源",
        "kind": "book",
        "url": ("https://raw.githubusercontent.com/tickmao/Novel/"
                "master/sources/legado/full.json"),
    },
    {
        "name": "XIU2 精品书源",
        "kind": "book",
        "url": "https://raw.githubusercontent.com/XIU2/Yuedu/master/shuyuan",
    },
    {
        "name": "hebijunge 音源插件",
        "kind": "music",
        "url": ("https://raw.githubusercontent.com/hebijunge/"
                "musicfree-plugins/main/plugins-ghproxy.json"),
    },
]


def fetch(url: str) -> tuple[int, bytes]:
    if httpx is not None:
        with httpx.Client(timeout=90, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
            return r.status_code, r.content

    # 本机可能没装 httpx，而 macOS 自带的 python 常常缺 CA 证书，
    # urllib 会直接 CERTIFICATE_VERIFY_FAILED。curl 用系统证书链，最稳。
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        p = subprocess.run(
            ["curl", "-sS", "-L", "-m", "90", "--noproxy", "*",
             "-A", UA, "-o", path, "-w", "%{http_code}", url],
            capture_output=True, text=True)
        code = int(p.stdout.strip() or 0)
        with open(path, "rb") as fh:
            return code, fh.read()
    finally:
        try:
            import os
            os.unlink(path)
        except OSError:
            pass


def parse_book(raw: bytes) -> tuple[list[dict], str]:
    """返回 (合格条目, 问题描述)。问题为空串表示完全正常。"""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return [], "JSON 解析失败：%s" % e
    if not isinstance(data, list):
        return [], "顶层不是数组，实际是 %s" % type(data).__name__

    good, bad = [], 0
    for it in data:
        if not isinstance(it, dict):
            bad += 1
            continue
        if (it.get("bookSourceName") and it.get("bookSourceUrl")
                and it.get("ruleSearch")):
            good.append(it)
        else:
            bad += 1
    if not good:
        return [], "没有任何一条含完整字段（bookSourceName / bookSourceUrl / ruleSearch）"
    return good, ("%d 条字段不全已剔除" % bad if bad else "")


def parse_music(raw: bytes) -> tuple[list[dict], str]:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return [], "JSON 解析失败：%s" % e

    items = data.get("plugins") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "找不到 plugins 数组"

    good = [x for x in items
            if isinstance(x, dict) and x.get("name") and x.get("url")]
    if not good:
        return [], "没有任何一条含 name / url"
    return good, ("" if len(good) == len(items)
                  else "%d 条缺字段已剔除" % (len(items) - len(good)))


def load_baseline() -> dict:
    p = OUT / "baseline.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_due(kind: str, base: dict, today) -> bool:
    """按类型判断是否到了更新周期。上次记录缺失或不合法都当作到期。"""
    last = (base.get("last_run") or {}).get(kind)
    if not last:
        return True
    try:
        d = datetime.strptime(str(last), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return (today - d).days >= KIND_EVERY.get(kind, 1)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = load_baseline()
    prev_counts = dict(base.get("counts") or {})
    last_run = dict(base.get("last_run") or {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().date()

    due = {k for k in KIND_EVERY if is_due(k, base, today)}
    skipped = [k for k in KIND_EVERY if k not in due]

    alerts: list[str] = []
    lines: list[str] = []
    books: dict[str, dict] = {}
    music: dict[str, dict] = {}
    counts = dict(prev_counts)

    print("%s  检查 %d 个源，到期类型：%s\n"
          % (now, len(SOURCES), "、".join(sorted(due)) or "无"))

    for src in SOURCES:
        name, kind = src["name"], src["kind"]
        if kind not in due:
            left = KIND_EVERY[kind] - (today - datetime.strptime(
                str(last_run[kind]), "%Y-%m-%d").date()).days
            lines.append("· %-22s 未到周期（%d 天一次，还差 %d 天，沿用上次结果）"
                         % (name, KIND_EVERY[kind], max(left, 0)))
            continue

        try:
            code, raw = fetch(src["url"])
        except Exception as e:
            msg = "%s（%s）" % (type(e).__name__, str(e)[:60])
            alerts.append("[抓不到] %s —— %s" % (name, msg))
            lines.append("✘ %-22s 抓取失败：%s" % (name, msg))
            continue

        if code != 200:
            alerts.append("[HTTP %d] %s" % (code, name))
            lines.append("✘ %-22s HTTP %d，%d 字节" % (name, code, len(raw)))
            continue

        items, problem = (parse_book(raw) if kind == "book"
                          else parse_music(raw))

        prev = prev_counts.get(name)
        delta = "" if prev is None else "  %+d" % (len(items) - prev)
        lines.append("✔ %-22s %4d 条  %5.1f KB%s"
                     % (name, len(items), len(raw) / 1024, delta))

        if problem:
            alerts.append("[%s] %s" % (problem, name))
        if prev and prev > 0 and len(items) < prev * (1 - SHRINK_ALERT):
            alerts.append("[数量骤降] %s —— 上次 %d 条，这次 %d 条"
                          % (name, prev, len(items)))
        if not items:
            continue

        counts[name] = len(items)
        bucket = books if kind == "book" else music
        for it in items:
            key = ((it.get("bookSourceUrl") or "").rstrip("/")
                   if kind == "book" else it.get("name"))
            if key and key not in bucket:
                bucket[key] = it

    # 只在拿到数据时才覆盖，抓取全失败就保留上一次的成果
    if "book" in due and books:
        (OUT / "book-source.json").write_text(
            json.dumps(list(books.values()), ensure_ascii=False, indent=1),
            encoding="utf-8")
        last_run["book"] = today.isoformat()
        lines.append("")
        lines.append("书源已更新：%d 条" % len(books))
    elif "book" in due:
        alerts.append("[未覆盖] 书源本轮没拿到任何数据，保留上次文件")
        lines.append("书源本轮无数据，保留上次文件不覆盖")

    if "music" in due and music:
        (OUT / "music-source.json").write_text(
            json.dumps({"plugins": list(music.values())},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        last_run["music"] = today.isoformat()
        lines.append("音源已更新：%d 个" % len(music))
    elif "music" in due:
        alerts.append("[未覆盖] 音源本轮没拿到任何数据，保留上次文件")
        lines.append("音源本轮无数据，保留上次文件不覆盖")

    if skipped:
        lines.append("沿用上次：%s（未到周期）" % "、".join(skipped))

    print("\n".join(lines))

    # 健康报告：只写异常
    health = ["%s  检查 %d 个源" % (now, len(SOURCES)), ""]
    if alerts:
        health.extend(alerts)
    elif not due:
        health.append("本轮无到期源，全部沿用上次结果。")
    elif not base:
        health.append("首次运行，已建立基线。下次起会比对条目数变化。")
    else:
        health.append("无异常。到期的源条目数与上次持平。")
    if skipped:
        health.append("")
        health.append("未到期跳过：%s" % "、".join(
            "%s（%d 天一次）" % (k, KIND_EVERY[k]) for k in skipped))
    (OUT / "nav-health.txt").write_text("\n".join(health) + "\n",
                                        encoding="utf-8")
    print("\n—— 健康报告 ——")
    print("\n".join(health))

    (OUT / "baseline.json").write_text(json.dumps({
        "updated_at": now,
        "counts": counts,
        "last_run": last_run,
        "interval_days": KIND_EVERY,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
