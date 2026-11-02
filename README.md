# 大白 IPTV

把我自己看着顺手的几个公开直播源清单合到一起，**在家里宽带上真播一遍**，只留下
出得了画面的那些，按运营商分好类，每天更新一次。

出来的 m3u 直接喂给电视、VLC、PotPlayer 或者手机上的直播 App 就行。

## 订阅地址

国内直连 `raw.githubusercontent.com` 基本是超时，所以走镜像。**主地址用 gh-proxy**
（实测 1 秒内返回，拿到的一定是当天的清单）：

| 用途 | 地址 |
|---|---|
| **主清单（先订这个）** | `https://gh-proxy.com/https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/any.m3u` |
| 备用镜像（同上，换一个域名） | `https://ghfast.top/https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/any.m3u` |
| txt 格式（部分 App 只吃这种） | `https://gh-proxy.com/https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/iptv.txt` |
| 移动宽带专用（**未实测**） | `https://gh-proxy.com/https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/mobile.unprobed.m3u` |
| 联通宽带专用（**未实测**） | `https://gh-proxy.com/https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/unicom.unprobed.m3u` |

jsDelivr 那条更「正规」，但**别拿它当主地址**：

```
https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/any.m3u
```

它的分支地址设的是 12 小时边缘缓存，而且每个 POP 各存一份、`purge` 刷不干净。
实测在推送后 10 分钟，从不同节点能同时取到 357 条和 406 条两个版本。日志稳、
但内容可能滞后半天——适合当备份，不适合当天看。

> 想用最稳的官方渠道，可以在仓库 Settings → Pages 里把 Pages 打开（Source 选
> `main` / `/`），之后 `https://dabai-abc.github.io/dabai-iptv/output/any.m3u`
> 就是 GitHub 自家 CDN，缓存只有 10 分钟。

**怎么选**：主机房是河北电信，所以 `any.m3u` 就是「在电信宽带上实测能播」的那份。
你是电信用户，订这一个就够。移动、联通用户订 `*.unprobed.m3u`——那里面是对应
运营商的内网源，我这里连不上，验不了，能不能播得你自己试。

## 清单是怎么来的

```
9 个公开源  →  2849 条去重  →  按运营商切开  →  实测  →  422 条 / 297 个频道
```

最近一次实测的漏斗（河北电信，2026-11-02 20:58，以 `output/stats.json` 为准）：

| 环节 | 条数 |
|---|---|
| 聚合去重后 | 2849 |
| 跨运营商内网源（跳过不测） | 435（移动 402 / 联通 33） |
| 真正实测 | 2414 |
| 首轮判定可用 | 399 |
| 低并发复测捞回 | 23 |
| **最终可用** | **422 / 297 个频道** |

数字每天都会变。这些是公开源，今天 349 条明天可能只剩一半，所以每天重跑一次；
`stats.json` 记着这一轮是什么时候、在哪个运营商网络下测的。

### 检测标准

一个源要算「能用」，必须同时满足两条：

1. m3u8 清单能拿到，内容是 `#EXTM3U`
2. 清单里的第一个分片能下到，且大于 1KB

只做第 1 步是很多清单的通病：列表里一堆台，点开全黑屏——清单文件还在，分片
早就 404 了。这个仓库的可用清单是过了第 2 步的。

### 为什么要复测

一条宽带同时朝同一个 CDN 甩 100 个连接，对方大概率返 503/502 或者干脆不回包。
第一轮跑完可用率只有一成多，其中相当一部分不是源坏了，是我们自己把人家打毛了。
所以失败的那批会降并发到 20 重问一遍——有过一次一轮就捞回一倍可用源的情况。

同理，单条检测带墙钟预算（默认 4 倍超时）。有些地址挂着 `.m3u8` 的名头其实
是直播流，数据源源不断，每次 read 都在超时时间内返回，读超时永远不触发，线程
会无限期挂死——第一次全量检测就卡在最后 75 条上，一个多小时没动静。

### 为什么要按运营商分文件

国内直播源最大的一个坑。清单里一大半地址长这样：

```
http://[2409:8087:1a01:df::4077]/ottrrs.hl.chinamobile.com/PLTV/.../index.m3u8
```

`2409` 是**移动**的 IPv6 段，`240e` 是电信，`2408` 是联通。这些是各省运营商自己的
内网源，只有本网用户连得进去，跨网就是死链接。

实测最说明问题：NAS 上那个 iptv-api 容器产出的 IPv6 清单 1109 条，**100% 是 2409
移动**，在电信宽带上一条都播不了。混在一起发出去，八成是废的，还会让人以为
是播放器坏了。

顺带说个现实：电信的 IPv6 内网源（240e）在公开聚合站里基本拿不到，vbskycn、
fanmingming 这些站给的 IPv6 清单清一色是移动。所以电信用户能用的主要是
IPv4 / 域名源，这份清单就是这么筛出来的。

## 为什么 GitHub Actions 不做检测

`.github/workflows/update.yml` 每天只做聚合，不测速，输出到 `output/raw/`。

检测必须从**用户所在网络**发起才有意义。GitHub 的机器在海外机房：连不上各省
运营商的内网 IPv6，也判断不了国内 CDN 的真实可用性，硬测会把一大批国内能用的
源误杀。

所以真正筛选过的清单是在家里 NAS 上跑 `--probe` 生成的，跑完直接推回本仓库的
`output/`。Actions 那份 `output/raw/` 只是兜底：万一家里机器好几天没开机，
订阅还有东西可看。

## 自己跑

```bash
pip install httpx

python build.py                      # 只聚合，秒级完成
python build.py --probe              # 聚合 + 真实检测
python build.py --probe --workers 100 --timeout 5
python build.py --probe --isp telecom    # 手动指定本机运营商
python build.py --probe --isp all        # 不跳过任何源，全都测（慢，误杀多）
```

`--isp auto`（默认）会从出口 IP 判断本机是电信/联通/移动，然后跳过别家的内网源。
一条电信宽带去测四百条移动地址，实测 0 条能通，纯属白跑还占满并发。

## 在 NAS 上常驻

镜像就在仓库根目录：

```bash
docker build -t local/iptv-pub:1.0 .
docker run -d --name dabai-iptv-pub --network host \
  -e GITHUB_TOKEN=xxx -e GITHUB_REPO=DaBai-abc/dabai-iptv \
  -v /path/to/iptv-api/output:/iptv-output:ro \
  local/iptv-pub:1.0
```

几个要点：

- **必须 `--network host`**。容器要走 IPv6 才测得了 2409/240e 那些内网源，
  默认 bridge 网络没有 IPv6。
- `-v ...:/iptv-output:ro` 把家里 iptv-api 容器的产物挂进来，它是最肥的候选池。
- 容器起来先跑一轮，之后每天 `PUBLISH_HOUR:PUBLISH_MINUTE`（默认 05:30）跑一次。
- 只想立刻跑一次：`docker run --rm ... python publish.py --once`。
- 推送走 Git Data API，一次 commit 推完全部文件，不会把仓库历史刷成噪声。

## 目录

```
build.py               聚合 + 分类 + 检测 + 输出
publish.py             NAS 上常驻：定时实测 → 推回 GitHub
Dockerfile             上面这个脚本的运行环境
output/                筛选过的清单（订阅用这个）
  any.m3u              通用源，实测能播（电信用户订这个）
  iptv.m3u / iptv.txt  全部
  mobile.unprobed.m3u  移动内网源，未实测
  unicom.unprobed.m3u  联通内网源，未实测
  stats.json           本次生成时间、条数、检测所用的外网运营商
output/raw/            Actions 生成的未筛选聚合，兜底用
cache/                 可放一份本地 iptv-api 的 result.m3u 作为候选池
```

## 已知限制

- **源会死**。这类公开源的生命周期以天计，每天重建一次，别指望一个地址长期不变。
- **不保证每个台都能看**。通过检测只说明「刚才这一刻能出画面」，晚高峰该卡还是卡。
  抽检 8 条有 7 条能出画面。
- **不含付费频道**。只聚合公开清单里本来就有的东西，不做任何破解。
- **jsDelivr 有缓存**。刚更新完可能还是旧的，等一会儿或者换 raw 地址。
- 频道名做了归一化（`CCTV1高清` 和 `CCTV1HD` 视为同一个台），同一频道下最多保留
  4 路源，按延迟排。某路不行，播放器里换下一个就行。
