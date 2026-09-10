# 使用说明

个人 GitHub 开源项目「周飙升榜」雷达。榜单正文在仓库根目录 [README.md](../README.md)，由脚本每天自动覆盖更新。

## 上线步骤

1. 把本仓库推到 GitHub（建议公开仓库）
2. **Settings → Actions → General → Workflow permissions** 选 **Read and write permissions**，保存
3. 打开 **Actions** → **Update GitHub Weekly Rank** → **Run workflow**
4. 之后每天 UTC 00:00（北京时间约 08:00）自动更新；直接看 README 即可

## 本地试跑

```bash
# Windows PowerShell 可选：
# $env:GITHUB_TOKEN = "ghp_xxx"

python scripts/update_rank.py
```

无第三方依赖。首次运行周增长为 0（只有快照）；从第二天起会显示相对前一天的观测增量，约一周后形成完整周榜。

## 原理简述

1. 用 GitHub Search API 拉一批候选仓库（高星 / 近期创建 / 近期活跃）
2. 把当天 Star 写入 `data/snapshots/YYYY-MM-DD.json`
3. 与约 7 天前（不足则用更近的历史）快照相减，得到增长量
4. 按周增长排序，生成 README Top 20

灵感：[OpenGithubs/github-weekly-rank](https://github.com/OpenGithubs/github-weekly-rank)
