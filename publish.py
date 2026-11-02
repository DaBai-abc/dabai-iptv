#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 NAS 上每天跑一次实测，把结果推回 GitHub。

为什么最终清单不在 GitHub Actions 里生成：检测必须从用户所在的网络发起才有
意义。Actions 的机器在海外机房，既连不上各省运营商的内网 IPv6，也判断不了
国内 CDN 的真实可用性，硬测会把一大批本来能用的源判死。

所以分工是：

  · GitHub Actions —— 每天聚合一份未筛选的全量清单，放 output/raw/ 兜底；
  · 这个脚本（跑在家里 NAS 上）—— 用家里宽带实测，产出真正能播的清单，
    覆盖 output/ 根目录那几份，也就是大家订阅的地址。

环境变量：
  GITHUB_TOKEN   有 repo 写权限的 PAT
  GITHUB_REPO    形如 DaBai-abc/dabai-iptv
  PUBLISH_HOUR   每天几点跑（默认 5）
  IPTV_M3U       本地 iptv-api 产物路径（可选，作为候选池）

用法：
  python publish.py --once     # 立刻跑一次（调试用）
  python publish.py            # 常驻，每天定时跑
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
API = "https://api.github.com"

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = os.environ.get("GITHUB_REPO", "").strip()
HOUR = int(os.environ.get("PUBLISH_HOUR", "5"))
MINUTE = int(os.environ.get("PUBLISH_MINUTE", "30"))


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 构建
def build() -> bool:
    cmd = [sys.executable, str(HERE / "build.py"), "--probe",
           "--workers", os.environ.get("PROBE_WORKERS", "100"),
           "--timeout", os.environ.get("PROBE_TIMEOUT", "5")]
    log("开始构建：%s" % " ".join(cmd[1:]))
    t0 = time.time()
    # 边跑边打。之前是跑完才一次性输出，构建要十几分钟，这期间 docker logs
    # 一片空白，完全看不出卡在哪一步、还是已经死了。
    # stderr 必须并到 stdout：抓取报错、检测进度全走 stderr。
    p = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log("  " + line)
    code = p.wait()
    if code != 0:
        log("构建失败（退出码 %d）" % code)
        return False
    log("构建完成，用时 %.0fs" % (time.time() - t0))
    return True


# ---------------------------------------------------------------- 推送
def collect() -> list[Path]:
    files = sorted(OUT.glob("*.m3u")) + sorted(OUT.glob("*.txt"))
    stats = OUT / "stats.json"
    if stats.exists():
        files.append(stats)
    return [f for f in files if f.exists()]


def push_batch(cli: httpx.Client, files: list[Path]) -> bool:
    """一次 commit 推完全部文件。

    用 Contents API 一个个推，一天就是七八条提交，仓库历史很快就被刷成噪声。
    这里走 Git Data API：建 blob → 拼 tree → 一次 commit → 移动分支指针。
    """
    h = {"Accept": "application/vnd.github+json"}

    r = cli.get("%s/repos/%s/git/ref/heads/main" % (API, REPO), headers=h)
    if r.status_code != 200:
        log("读不到 main 分支：%d %s" % (r.status_code, r.text[:200]))
        return False
    head_sha = r.json()["object"]["sha"]

    r = cli.get("%s/repos/%s/git/commits/%s" % (API, REPO, head_sha), headers=h)
    base_tree = r.json()["tree"]["sha"]

    tree = []
    for f in files:
        r = cli.post("%s/repos/%s/git/blobs" % (API, REPO), headers=h, json={
            "content": base64.b64encode(f.read_bytes()).decode(),
            "encoding": "base64",
        })
        if r.status_code not in (200, 201):
            log("  ✗ blob 失败 %s %d" % (f.name, r.status_code))
            return False
        tree.append({"path": "output/" + f.name, "mode": "100644",
                     "type": "blob", "sha": r.json()["sha"]})

    r = cli.post("%s/repos/%s/git/trees" % (API, REPO), headers=h,
                 json={"base_tree": base_tree, "tree": tree})
    if r.status_code not in (200, 201):
        log("  ✗ tree 失败 %d %s" % (r.status_code, r.text[:200]))
        return False
    new_tree = r.json()["sha"]

    r = cli.post("%s/repos/%s/git/commits" % (API, REPO), headers=h, json={
        "message": "chore: 实测可用清单 %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tree": new_tree,
        "parents": [head_sha],
    })
    if r.status_code not in (200, 201):
        log("  ✗ commit 失败 %d %s" % (r.status_code, r.text[:200]))
        return False
    new_commit = r.json()["sha"]

    r = cli.patch("%s/repos/%s/git/refs/heads/main" % (API, REPO), headers=h,
                  json={"sha": new_commit, "force": False})
    if r.status_code not in (200, 201):
        log("  ✗ 移动分支失败 %d %s" % (r.status_code, r.text[:200]))
        return False

    log("已推送 %d 个文件，commit %s" % (len(files), new_commit[:7]))
    return True


def publish() -> None:
    if not TOKEN or not REPO:
        log("缺 GITHUB_TOKEN / GITHUB_REPO，只构建不推送")
        return
    files = collect()
    if not files:
        log("没有可推送的文件")
        return
    with httpx.Client(timeout=60, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "dabai-iptv-publisher",
    }) as cli:
        if not push_batch(cli, files):
            log("批量推送失败，本次未发布")


# ---------------------------------------------------------------- 调度
def next_run() -> datetime:
    now = datetime.now()
    target = now.replace(hour=HOUR, minute=MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="立刻跑一次就退出")
    args = ap.parse_args()

    while True:
        try:
            if build():
                publish()
        except Exception:                                   # noqa: BLE001
            log("出错了：\n" + traceback.format_exc())
        if args.once:
            return 0
        t = next_run()
        log("下次运行：%s" % t.strftime("%Y-%m-%d %H:%M"))
        time.sleep(max(60, (t - datetime.now()).total_seconds()))


if __name__ == "__main__":
    sys.exit(main())
