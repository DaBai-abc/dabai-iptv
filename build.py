#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大白 IPTV 清单构建器

从几个公开清单聚合直播源，按运营商分类，可选做真实播放检测，最后输出
可以直接订阅的 m3u / txt。

设计上的两个取舍：

1. **必须按运营商分文件**。国内直播源一大半是各省运营商的内网地址
   （IPv6 段 2409 移动 / 2408 联通 / 240e 电信），跨运营商必然连不上。
   混在一起发出去，等于给用户一份 60% 是废的清单。

2. **检测要测到分片**。很多源 m3u8 能返回 200，但里面的 ts 分片早就没了，
   播放器打开就是黑屏。只测 m3u8 的清单看着漂亮，实际上不能看。

用法：
    python build.py                  # 只聚合，不检测（GitHub Actions 用）
    python build.py --probe          # 聚合 + 检测（家里网络跑才准）
    python build.py --probe --only telecom
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

EPG = "https://epg.112114.xyz/pp.xml"

# ---------------------------------------------------------------- 源清单
# urls 按顺序尝试，第一个失败就换下一个（raw 在国内常被墙，jsDelivr 一般能通）
SOURCES = [
    {
        "name": "vbskycn/iptv · IPv4",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/vbskycn/iptv@master/tv/iptv4.m3u",
            "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
        ],
    },
    {
        "name": "vbskycn/iptv · IPv6",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/vbskycn/iptv@master/tv/iptv6.m3u",
            "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv6.m3u",
        ],
    },
    {
        "name": "ssili126/tv",
        "kind": "txt",
        "urls": [
            "https://cdn.jsdelivr.net/gh/ssili126/tv@main/itvlist.txt",
            "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
        ],
    },
    {
        "name": "iptv-org · 中国区",
        "kind": "iptvorg",
        "urls": ["https://iptv-org.github.io/api/streams.json"],
    },
    {
        "name": "fanmingming/live · IPv6",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/ipv6.m3u",
        ],
    },
    {
        "name": "Guovin/iptv-api · 广东",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u",
        ],
    },
    {
        "name": "Kimentanm/aptv",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/Kimentanm/aptv@master/m3u/iptv.m3u",
        ],
    },
    {
        "name": "YueChan/Live",
        "kind": "m3u",
        "urls": [
            "https://cdn.jsdelivr.net/gh/YueChan/Live@main/IPTV.m3u",
        ],
    },
    {
        # 本地 iptv-api（NAS 上的容器）产物，没有就跳过
        "name": "本地 iptv-api",
        "kind": "m3u",
        "urls": ["file:///iptv-output/result.m3u", "file://" + str(HERE / "cache/result.m3u")],
    },
]

GROUP_ORDER = ["央视频道", "卫视频道", "省内频道", "港台频道", "体育频道",
               "影视频道", "少儿频道", "纪录频道", "数字频道", "其他频道"]

# 频道名归一化：这些后缀只是同一频道的不同画质版本，去重时视为同一个
_QUALITY = re.compile(
    r"[\s\-_]*(高清|超清|标清|蓝光|4K|8K|HD|FHD|UHD|SD|H265|HEVC|HDR|\d{2,4}P)$",
    re.I)


def clean_name(name: str) -> str:
    n = (name or "").strip().strip('"').strip()
    n = n.replace("CCTV-", "CCTV").replace("cctv", "CCTV")
    prev = None
    while prev != n:                       # “CCTV1高清HD” 要连着剥两层
        prev = n
        n = _QUALITY.sub("", n).strip()
    return n or "未命名"


def isp_of(url: str) -> str:
    """判断这个源属于哪个运营商。判不出来的归 any（公网源，谁都能连）。"""
    m = re.match(r"https?://\[([0-9a-fA-F:]+)\]", url)
    if m:
        head = m.group(1).lower()
        if head.startswith("2409"):
            return "mobile"
        if head.startswith("240e"):
            return "telecom"
        if head.startswith("2408"):
            return "unicom"
        return "any"
    low = url.lower()
    if "chinamobile" in low or "cmcc" in low or "10086" in low:
        return "mobile"
    if "chinaunicom" in low or "10010" in low:
        return "unicom"
    if "chinatelecom" in low or "189.cn" in low:
        return "telecom"
    return "any"


ISP_LABEL = {"telecom": "电信", "unicom": "联通", "mobile": "移动", "any": "通用"}


def detect_isp(timeout: float = 6.0) -> str:
    """看这台机器走的是哪家宽带。

    用途只有一个：别浪费时间去测别的运营商的内网源。一条电信宽带去探
    348 条 2409:（移动）地址，实测 0 条能通，纯属白跑还占满并发。
    """
    for url in ("https://myip.ipip.net", "https://myip.ipip.net/s"):
        try:
            r = httpx.get(url, timeout=timeout, headers={"User-Agent": UA})
            t = r.text
        except Exception:                                   # noqa: BLE001
            continue
        for kw, isp in (("电信", "telecom"), ("联通", "unicom"), ("移动", "mobile")):
            if kw in t:
                return isp
    return ""


# ---------------------------------------------------------------- 解析
_EXTINF = re.compile(r"^#EXTINF:-?\d+(.*)$", re.I)


def parse_m3u(text: str, source: str) -> list[dict]:
    rows, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = _EXTINF.match(line)
            attrs_raw = m.group(1) if m else ""
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs_raw))
            shown = attrs_raw.rsplit(",", 1)[-1].strip() if "," in attrs_raw else ""
            cur = {
                "name": clean_name(attrs.get("tvg-name") or shown),
                "group": (attrs.get("group-title") or "").strip(),
                "logo": attrs.get("tvg-logo", "").strip(),
                "source": source,
            }
        elif line.startswith("#"):
            continue
        elif cur and "://" in line:
            cur["url"] = line
            rows.append(cur)
            cur = None
    return rows


def parse_txt(text: str, source: str) -> list[dict]:
    """常见格式：频道名,地址   或   group,#genre# 分节"""
    rows, group = [], "其他频道"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("#genre#"):
            group = line.split(",")[0].strip()
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        url = url.strip()
        if "://" not in url:
            continue
        rows.append({"name": clean_name(name), "group": group, "logo": "",
                     "url": url, "source": source})
    return rows


def parse_iptvorg(data: str, source: str) -> list[dict]:
    """iptv-org 的 streams.json：只保留大中华区频道。"""
    try:
        streams = json.loads(data)
    except json.JSONDecodeError:
        return []
    region = {"CN": "省内频道", "HK": "港台频道", "TW": "港台频道", "MO": "港台频道"}
    rows = []
    for s in streams:
        country = (s.get("channel") or "")
        url = s.get("url") or ""
        if not url.startswith("http"):
            continue
        rows.append({"name": "", "group": "", "logo": "", "url": url,
                     "source": source, "_raw": s})
    return rows


def fetch_iptvorg() -> list[dict]:
    """iptv-org 要把 streams 和 channels 两个表对起来才知道频道属于哪个国家。

    channels.json 有 8MB 左右，走 http_get_text 的重试，别一次抖动就整份丢掉。
    """
    base = "https://iptv-org.github.io/api/"
    # channels.json 有 8MB，家里网络每次都在 600KB~1MB 处被掐断，重试三次也是白等
    # 三分钟。给一次机会，拿不到就拉倒——这份源本来就只贡献少量港台频道。
    streams = json.loads(http_get_text(base + "streams.json", timeout=30, tries=2))
    channels = {x["id"]: x for x in json.loads(
        http_get_text(base + "channels.json", timeout=30, tries=1))}
    region = {"CN": "省内频道", "HK": "港台频道", "TW": "港台频道", "MO": "港台频道"}
    rows = []
    for s in streams:
        ch = channels.get(s.get("channel") or "")
        if not ch:
            continue
        cc = ch.get("country") or ""
        if cc not in region:
            continue
        url = (s.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        rows.append({
            "name": clean_name(ch.get("name") or ""),
            "group": region[cc],
            "logo": (ch.get("logo") or "").strip(),
            "url": url,
            "source": "iptv-org",
        })
    return rows


# ---------------------------------------------------------------- 抓取
def http_get_text(url: str, timeout: float = 15, tries: int = 3) -> str:
    """下载文本，失败重试。

    实测 jsDelivr 传几百 KB 的清单时会半路掐连接：
    `RemoteProtocolError: peer closed connection without sending complete message body
    (received 376154 bytes, expected 396412)`。一次就把整份源丢掉太亏，
    这种情况重试一两次基本都能拿到。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True,
                              headers={"User-Agent": UA}) as c:
                r = c.get(url)
            r.raise_for_status()
            return r.text
        except Exception as e:                              # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(1.2 * (i + 1))
    raise last if last else RuntimeError("下载失败")


def fetch(src: dict) -> list[dict]:
    last = None
    for url in src["urls"]:
        if url.startswith("file://"):
            p = Path(url[7:])
            if not p.exists():
                last = "本地文件不存在"
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        else:
            try:
                text = http_get_text(url)
            except Exception as e:                          # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, str(e)[:80])
                continue
        if src["kind"] == "iptvorg":
            return fetch_iptvorg()
        if src["kind"] == "txt":
            return parse_txt(text, src["name"])
        return parse_m3u(text, src["name"])
    print("  ! %s 取不到（%s）" % (src["name"], last), file=sys.stderr)
    return []


def fetch_guarded(src: dict, seconds: float = 90) -> list[dict]:
    """在独立线程里抓，超时就不等了。

    httpx 的 timeout 只管 socket 读写，管不到 DNS 解析——getaddrinfo 是 C 调用，
    没有超时机制。实测抓 iptv-org 时整个进程卡死过一次：日志停在第三个源之后
    不再动，进程还活着，一个多小时零进展。所以外面再套一层看门狗。
    线程是 daemon，放弃它不会拦住进程退出。
    """
    box: dict = {}

    def run() -> None:
        try:
            box["rows"] = fetch(src)
        except BaseException as e:                          # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        print("  ! %s 抓取超时（%ds），跳过" % (src["name"], int(seconds)), file=sys.stderr)
        return []
    if "err" in box:
        print("  ! %s 抓取出错：%s" % (src["name"], box["err"]), file=sys.stderr)
        return []
    return box.get("rows", [])


# ---------------------------------------------------------------- 检测
_tl = threading.local()


def _client(timeout: float) -> httpx.Client:
    """每个线程一个连接池。同步 client 比异步在这件事上稳得多：
    之前用 asyncio + httpx 时，两条 URL 能跑出 35 秒，查不到原因。"""
    c = getattr(_tl, "c", None)
    if c is None:
        c = httpx.Client(verify=False, follow_redirects=True, timeout=timeout,
                         headers={"User-Agent": UA})
        _tl.c = c
    return c


def _read_head(rs, limit: int, deadline: float) -> bytes:
    """流式读前 limit 字节，同时掐一个墙钟截止。

    必须这么写的原因：有相当一部分「m3u8」其实是挂着清单名头的直播流，
    数据源源不断地来，每次 read 都在超时时间内返回，httpx 的 read timeout
    永远不触发，整段读完更是等不到 EOF —— 线程就这么无限期挂住。
    实测全量 1875 条时，最后 75 条卡了一个多小时，就是这类地址。
    所以这里只要读够判断需要的字节、或者超过墙钟预算，就主动断开。
    """
    buf = bytearray()
    for chunk in rs.iter_bytes(8192):
        buf += chunk
        if len(buf) >= limit or time.time() > deadline:
            break
    return bytes(buf[:limit])


def probe_one(row: dict, timeout: float, budget: float | None = None) -> dict:
    """判定一个源能不能真的播出画面：

    1. m3u8 要能拿到，且内容是 #EXTM3U；
    2. 清单里第一个分片要能下到 >1KB。
    只做第 1 步的话，会把「清单还在、分片早没了」的死源当成好的——
    播放器打开就是黑屏，这正是原来那份清单看着有几百条却不能看的原因。
    """
    url = row["url"]
    budget = budget or max(8.0, timeout * 4)
    deadline = time.time() + budget
    t0 = time.time()
    c = _client(timeout)
    try:
        with c.stream("GET", url, timeout=timeout) as rs:
            code = rs.status_code
            head = _read_head(rs, 16384, deadline)
        if code != 200 or b"#EXTM3U" not in head[:400]:
            return {**row, "ok": False, "why": "HTTP %d" % code}
        text = head.decode("utf-8", "replace")
        seg = next((x.strip() for x in text.splitlines()
                    if x.strip() and not x.startswith("#")), "")
        if not seg:
            return {**row, "ok": False, "why": "清单为空"}
        if not seg.startswith("http"):
            seg = url.rsplit("/", 1)[0] + "/" + seg
        # 只读前 8KB 就断开。分片动辄几 MB，整段下完再判断会让全量检测从
        # 两分钟变成十几分钟，而判断「有没有画面」用前几 KB 已经够了。
        with c.stream("GET", seg, timeout=timeout) as rs2:
            code2 = rs2.status_code
            seg_head = _read_head(rs2, 8192, deadline)
        ok = code2 == 200 and len(seg_head) >= 1000
        return {**row, "ok": ok, "why": "" if ok else "分片 %d" % code2,
                "ms": int((time.time() - t0) * 1000)}
    except httpx.TimeoutException:
        return {**row, "ok": False, "why": "超时"}
    except Exception as e:                                  # noqa: BLE001
        return {**row, "ok": False, "why": type(e).__name__}


def probe_all(rows: list[dict], workers: int, timeout: float,
              label: str = "检测") -> list[dict]:
    total = len(rows)
    if not total:
        return []
    t0 = time.time()
    done = 0
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe_one, r, timeout) for r in rows]
        for f in as_completed(futures):
            out.append(f.result())
            done += 1
            if done % 100 == 0 or done == total:
                print("    %s进度 %d/%d  已用 %.0fs" % (label, done, total, time.time() - t0),
                      file=sys.stderr)
    return out


# 这些失败原因有「被限流」的可能，值得低并发复测一次；404 是真的没了，不用试
_RETRYABLE = {"超时", "HTTP 503", "HTTP 502", "HTTP 429", "ProxyError",
              "ConnectTimeout", "ReadTimeout", "ConnectError", "RemoteProtocolError"}


def retry_failed(rows: list[dict], orig: dict[str, dict], workers: int,
                 timeout: float) -> list[dict]:
    """第一轮用高并发扫全量，第二轮把「可能是被限流打死」的挑出来低并发复测。

    为什么需要这一轮：一条宽带对同一个 CDN 同时甩 100 个连接，对方大概率返
    503/502 或者干脆不回包。第一轮跑出来 17% 的可用率，其中相当一部分并不是
    源坏了，是我们自己把人家打毛了。复测用 1/5 的并发重问一遍，能捞回一批。
    """
    bad = [r for r in rows if not r["ok"] and r.get("why") in _RETRYABLE]
    if not bad:
        return rows
    print("  复测 %d 条（低并发 %d）" % (len(bad), workers), file=sys.stderr)
    fixed = {r["url"]: r for r in probe_all(bad, workers, timeout, label="复测")}
    saved = 0
    for r in rows:
        n = fixed.get(r["url"])
        if n and n["ok"]:
            orig[r["url"]] = {**orig[r["url"]], "ok": True, "why": "",
                              "ms": n.get("ms")}
            saved += 1
    print("  复测救回 %d 条" % saved, file=sys.stderr)
    return [orig[r["url"]] for r in rows]


# ---------------------------------------------------------------- 输出
def group_key(g: str) -> int:
    for i, name in enumerate(GROUP_ORDER):
        if g and (g == name or name in g or g in name):
            return i
    return 99


def to_m3u(rows: list[dict]) -> str:
    lines = ['#EXTM3U x-tvg-url="%s"' % EPG]
    for r in rows:
        attrs = 'tvg-name="%s"' % r["name"]
        if r.get("logo"):
            attrs += ' tvg-logo="%s"' % r["logo"]
        attrs += ' group-title="%s"' % (r.get("group") or "其他频道")
        lines.append("#EXTINF:-1 %s,%s" % (attrs, r["name"]))
        lines.append(r["url"])
    return "\n".join(lines) + "\n"


def to_txt(rows: list[dict]) -> str:
    out, cur = [], None
    for r in rows:
        g = r.get("group") or "其他频道"
        if g != cur:
            out.append("%s,#genre#" % g)
            cur = g
        out.append("%s,%s" % (r["name"], r["url"]))
    return "\n".join(out) + "\n"


def dedupe(rows: list[dict], per_channel: int = 4) -> list[dict]:
    """同一频道在同一运营商下保留最快的几路，避免几百条同名占屏。"""
    buckets: dict[tuple, list] = collections.defaultdict(list)
    for r in rows:
        buckets[(r["name"], r["isp"])].append(r)
    out = []
    for (_name, _isp), items in buckets.items():
        items.sort(key=lambda x: (not x.get("ok", True), x.get("ms") or 99999))
        out.extend(items[:per_channel])
    return out


def sort_rows(rows: list[dict]) -> list[dict]:
    isp_rank = {"any": 0, "telecom": 1, "unicom": 2, "mobile": 3}
    rows = sorted(rows, key=lambda r: (
        group_key(r.get("group") or ""),
        not r.get("ok", True),
        isp_rank.get(r["isp"], 9),
        r.get("ms") or 99999,
        r["name"],
    ))
    return rows


def write(name: str, text: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(text, encoding="utf-8")
    print("  写出 %-16s %6.1f KB" % (name, len(text.encode()) / 1024))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="做真实播放检测（家里网络跑才准）")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--workers", type=int, default=60)
    ap.add_argument("--retry-workers", type=int, default=20,
                    help="失败项复测的并发（低并发才不会被 CDN 当攻击）")
    ap.add_argument("--no-retry", action="store_true", help="不做失败项复测")
    ap.add_argument("--isp", default="auto",
                    help="本机宽带运营商：auto/telecom/unicom/mobile/all。"
                         "非本机的内网源不实测，另存为未实测清单")
    ap.add_argument("--outdir", default="output", help="输出目录（Actions 写 output/raw 以免覆盖主清单）")
    ap.add_argument("--only", default="", help="只输出某个运营商：telecom/unicom/mobile/any")
    args = ap.parse_args()

    global OUT
    OUT = (HERE / args.outdir).resolve()

    print("① 抓取源清单")
    raw: list[dict] = []
    for src in SOURCES:
        rows = fetch_guarded(src, 150)
        print("  %-22s %4d 条" % (src["name"], len(rows)))
        if not rows:
            print("  ! %s 解析出 0 条，这份源本次没用上" % src["name"], file=sys.stderr)
        raw.extend(rows)

    # 去重（同一 URL 只留一条）、判定运营商、补临时字段
    seen, rows = set(), []
    for r in raw:
        u = r["url"].strip()
        if not u or u in seen:
            continue
        if not u.lower().startswith(("http://", "https://")):
            continue
        seen.add(u)
        r["url"] = u
        r["isp"] = isp_of(u)
        if not r.get("name"):
            continue
        rows.append(r)

    print("② 去重后 %d 条 | 电信 %d / 联通 %d / 移动 %d / 通用 %d" % (
        len(rows),
        sum(1 for r in rows if r["isp"] == "telecom"),
        sum(1 for r in rows if r["isp"] == "unicom"),
        sum(1 for r in rows if r["isp"] == "mobile"),
        sum(1 for r in rows if r["isp"] == "any")))

    skipped: list[dict] = []
    local = ""
    if args.probe:
        if args.isp != "all":
            local = detect_isp() if args.isp == "auto" else args.isp
            print("  本机宽带：%s" % (ISP_LABEL.get(local, "未识别（全部实测）")))
        # 跨运营商的内网源在本机必然连不通，实测只会把它们判死。
        # 这类不测，单独存成"未实测"清单，让对应运营商的用户自己取。
        skipped = []
        if local:
            skipped = [r for r in rows if r["isp"] not in ("any", local)]
            rows = [r for r in rows if r["isp"] in ("any", local)]
            if skipped:
                print("  跳过 %d 条跨运营商内网源（本机 %s 播不了，另存未实测清单）"
                      % (len(skipped), ISP_LABEL.get(local, local)))

        print("③ 真实播放检测（并发 %d，超时 %.0fs）" % (args.workers, args.timeout))
        rows = probe_all(rows, args.workers, args.timeout)
        ok = [r for r in rows if r["ok"]]
        print("  首轮可用 %d / %d (%.0f%%)" % (len(ok), len(rows),
                                              100 * len(ok) / max(1, len(rows))))
        orig = {r["url"]: r for r in rows}
        if not args.no_retry:
            rows = retry_failed(rows, orig, args.retry_workers, args.timeout)
        ok = [r for r in rows if r["ok"]]
        print("  复测后可用 %d / %d (%.0f%%)" % (len(ok), len(rows),
                                                100 * len(ok) / max(1, len(rows))))
        reasons = collections.Counter(r.get("why") or "-" for r in rows if not r["ok"])
        print("  失败原因：%s" % ", ".join("%s×%d" % kv for kv in reasons.most_common(5)))
        # 全量检测明细留一份在 cache/（已被 gitignore），出问题能追溯
        try:
            d = HERE / "cache"
            d.mkdir(exist_ok=True)
            (d / "probe.json").write_text(json.dumps(
                [{"name": r["name"], "isp": r["isp"], "url": r["url"],
                  "ok": r["ok"], "why": r.get("why", ""), "ms": r.get("ms")}
                 for r in rows], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        rows = ok
    else:
        print("③ 跳过检测（GitHub Actions 的机器在国外，测国内源会大面积误杀）")

    if args.only:
        rows = [r for r in rows if r["isp"] == args.only]

    rows = sort_rows(dedupe(rows))

    print("④ 输出")
    # 先清掉上一轮的文件。不清的话，这一轮某个运营商源全灭时，旧文件会留在
    # 那里冒充「本次结果」，订阅的人看到的是一份几小时前的过期清单。
    OUT.mkdir(parents=True, exist_ok=True)
    for old in list(OUT.glob("*.m3u")) + list(OUT.glob("*.txt")):
        try:
            old.unlink()
        except OSError:
            pass

    write("iptv.m3u", to_m3u(rows))
    write("iptv.txt", to_txt(rows))
    for isp in ("any", "telecom", "unicom", "mobile"):
        sub = [r for r in rows if r["isp"] == isp]
        if sub:
            write("%s.m3u" % isp, to_m3u(sub))

    # 跨运营商内网源：本机验不了，但对应运营商的用户能用，单独放一份
    untested = {}
    if skipped:
        for isp in ("telecom", "unicom", "mobile"):
            sub = sort_rows([r for r in skipped if r["isp"] == isp])
            if not sub:
                continue
            head = ("#EXTM3U x-tvg-url=\"%s\"\n"
                    "# 以下是%s内网源，未在本机实测（本机是%s宽带，连不上）。\n"
                    "# 只有%s宽带的用户能播，能不能用请自行试。\n" % (
                        EPG, ISP_LABEL[isp],
                        ISP_LABEL.get(local, "其他"), ISP_LABEL[isp]))
            body = to_m3u(sub).split("\n", 1)[1]
            write("%s.unprobed.m3u" % isp, head + body)
            untested[isp] = len(sub)

    stats = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probed": args.probe,
        "probe_isp": local if args.probe else "",
        "total": len(rows),
        "by_isp": {k: sum(1 for r in rows if r["isp"] == k)
                   for k in ("any", "telecom", "unicom", "mobile")},
        "channels": len({r["name"] for r in rows}),
        "untested_by_isp": untested,
    }
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print("  共 %d 条 / %d 个频道" % (stats["total"], stats["channels"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
