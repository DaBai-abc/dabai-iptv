# 大白 IPTV

把我自己看着顺手的几个公开直播源清单合到一起，按运营商分好类，每天更新一次。
出来的 m3u 直接喂给电视、VLC、PotPlayer 或者手机上的直播 App 就行。

## 订阅地址

国内网络访问 `raw.githubusercontent.com` 基本是超时的，所以主地址走 jsDelivr：

| 用途 | 地址 |
|---|---|
| **通用源（推荐先试这个）** | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/any.m3u` |
| 电信宽带 | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/telecom.m3u` |
| 联通宽带 | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/unicom.m3u` |
| 移动宽带 | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/mobile.m3u` |
| 全部混在一起 | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/iptv.m3u` |
| txt 格式（部分 App 只吃这种） | `https://cdn.jsdelivr.net/gh/DaBai-abc/dabai-iptv@main/output/iptv.txt` |

海外或者有代理的话，用 raw 地址更实时（jsDelivr 有缓存，最长可能滞后 12 小时）：

```
https://raw.githubusercontent.com/DaBai-abc/dabai-iptv/main/output/any.m3u
```

> 家宽用户先订阅 `any.m3u`。如果里面缺了你常看的台，再去订对应运营商那份。
> 两份都订上也不会冲突，播放器一般会合并列表。

## 为什么要按运营商分文件

这是国内直播源最大的一个坑。清单里一大半地址长这样：

```
http://[2409:8087:1a01:df::4077]/ottrrs.hl.chinamobile.com/PLTV/.../index.m3u8
```

`2409` 开头的 IPv6 段属于**中国移动**，`240e` 是电信，`2408` 是联通。这类是各省
运营商自己的内网源，只有本网用户连得进去，跨网就是死链接。

我自己在河北电信的宽带下实测过一份 400 条的清单：

- 其中 232 条是移动内网地址，**一条都播不了**
- 剩下的 168 条公网源，只有 66 条能真正出画面

也就是说混着发出去，八成是废的，还会让人误以为是播放器坏了。所以这里按运营商
切开，各订各的。

## 检测标准

一个源要算「能用」，需要同时满足两条：

1. m3u8 清单能拿到，内容是 `#EXTM3U`
2. 清单里的第一个分片能下到，且大于 1KB

很多清单只做第 1 步，于是会出现「列表里有一堆台，点开全黑屏」——清单文件还在，
分片早就 404 了。这个仓库的可用清单（`output/` 根目录那几份）是过了第 2 步的。

## 为什么 GitHub Actions 不做检测

`.github/workflows/update.yml` 每天只做聚合，不测速，输出到 `output/raw/`。

原因是检测必须从**用户所在网络**发起才有意义。GitHub 的机器在海外机房：

- 连不上各省运营商的内网 IPv6，测了必然判死
- 判断不了国内 CDN 的真实可用性，容易把国内能用的源误杀

所以真正筛选过的清单是在家里（或 NAS）跑 `--probe` 生成的。Actions 那份
`output/raw/` 只是兜底：万一家里机器好几天没开机，订阅还有东西可看。

## 自己跑

```bash
pip install httpx

python build.py                    # 只聚合，秒级完成
python build.py --probe            # 聚合 + 真实检测，约 5~15 分钟
python build.py --probe --workers 80 --timeout 5
python build.py --probe --only telecom    # 只出电信那份
```

想让订阅内容最准，把带 `--probe` 的那条放进本机定时任务（macOS 用 launchd，
NAS 用 crontab / 计划任务），跑完 `git commit && git push` 即可。

## 目录

```
build.py               聚合 + 分类 + 检测 + 输出
output/                筛选过的清单（订阅用这个）
  any.m3u              通用公网源
  telecom.m3u          电信
  unicom.m3u           联通
  mobile.m3u           移动
  iptv.m3u / iptv.txt  全部
  stats.json           本次生成的时间与数量
output/raw/            Actions 生成的未筛选聚合，兜底用
cache/                 可放一份本地 iptv-api 的 result.m3u 作为候选池
```

## 已知限制

- **源会死**。这类公开源的生命周期以天计，所以每天重建一次，别指望一个地址长期不变。
- **不保证每个台都能看**。通过检测只说明「刚才这一刻能出画面」，晚高峰该卡还是卡。
- **不含付费频道**。只聚合公开清单里本来就有的东西，不做任何破解。
- **jsDelivr 有缓存**。刚更新完可能还是旧的，等一会儿或者换 raw 地址。
- 频道名做了归一化（`CCTV1高清` 和 `CCTV1HD` 视为同一个台），同一频道下最多保留
  4 路源，按延迟排。真遇到某路不行，播放器里换下一个就行。
