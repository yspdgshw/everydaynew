#!/usr/bin/env python3
"""Fetch GitHub repo star snapshots and regenerate the weekly rank README."""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
META_PATH = ROOT / "data" / "meta.json"
README_PATH = ROOT / "README.md"

TOP_N = 20
TOP_HIGHLIGHT = 3
MAX_TRACKED = 400
SEARCH_PER_PAGE = 30
MAX_STALE_REFRESH = 120

USER_AGENT = "everydaynew-rank-bot/1.0"


def token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def api_request(url: str, retries: int = 5) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    t = token()
    if t:
        headers["Authorization"] = f"Bearer {t}"

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                data = json.loads(raw.decode("utf-8"))
            if remaining is not None and int(remaining) == 0 and reset:
                wait = max(1, int(reset) - int(time.time()) + 1)
                print(f"rate limit exhausted, sleeping {min(wait, 90)}s", file=sys.stderr)
                time.sleep(min(wait, 90))
            elif remaining is not None and int(remaining) < 3:
                time.sleep(2)
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (403, 429, 502, 503) and attempt < retries:
                sleep_s = min(60, 5 * attempt)
                print(f"HTTP {e.code}, retry in {sleep_s}s", file=sys.stderr)
                time.sleep(sleep_s)
                last_err = RuntimeError(f"HTTP {e.code} for {url}: {body[:300]}")
                continue
            raise RuntimeError(f"HTTP {e.code} for {url}: {body[:300]}") from e
        except (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            OSError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as e:
            last_err = e
            if attempt < retries:
                sleep_s = min(30, 3 * attempt)
                print(f"network error ({e}), retry in {sleep_s}s", file=sys.stderr)
                time.sleep(sleep_s)
                continue
            raise
    raise RuntimeError(f"failed after retries: {url}: {last_err}")

def search_repos(query: str, pages: int = 1) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": SEARCH_PER_PAGE,
                "page": page,
            }
        )
        url = f"https://api.github.com/search/repositories?{params}"
        data = api_request(url)
        batch = data.get("items") or []
        items.extend(batch)
        if len(batch) < SEARCH_PER_PAGE:
            break
        time.sleep(2)
    return items


def normalize_repo(item: dict[str, Any]) -> dict[str, Any]:
    full_name = item["full_name"]
    return {
        "full_name": full_name,
        "html_url": item.get("html_url") or f"https://github.com/{full_name}",
        "description": (item.get("description") or "").strip(),
        "stars": int(item.get("stargazers_count") or 0),
        "created_at": (item.get("created_at") or "")[:10],
        "language": item.get("language") or "",
    }


def search_candidates(today: date) -> dict[str, dict[str, Any]]:
    queries = [
        "stars:>2000",
        f"stars:>200 created:>{(today - timedelta(days=90)).isoformat()}",
        f"stars:>500 pushed:>{(today - timedelta(days=14)).isoformat()}",
        f"stars:>100 created:>{(today - timedelta(days=30)).isoformat()}",
    ]
    repos: dict[str, dict[str, Any]] = {}
    for q in queries:
        print(f"search: {q}")
        for item in search_repos(q, pages=1):
            repo = normalize_repo(item)
            repos[repo["full_name"]] = repo
        time.sleep(2)
    return repos


def load_recent_history(days: int = 14) -> dict[str, dict[str, Any]]:
    repos: dict[str, dict[str, Any]] = {}
    for snap_path in sorted(SNAPSHOT_DIR.glob("*.json"))[-days:]:
        try:
            old = json.loads(snap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for full_name, row in (old.get("repos") or {}).items():
            repos[full_name] = {
                "full_name": full_name,
                "html_url": row.get("html_url") or f"https://github.com/{full_name}",
                "description": row.get("description") or "",
                "stars": int(row.get("stars") or 0),
                "created_at": row.get("created_at") or "",
                "language": row.get("language") or "",
            }
    return repos


def refresh_stale(repos: dict[str, dict[str, Any]], fresh_names: set[str]) -> None:
    stale = [name for name in repos if name not in fresh_names][:MAX_STALE_REFRESH]
    for i, full_name in enumerate(stale, 1):
        url = f"https://api.github.com/repos/{full_name}"
        try:
            repos[full_name] = normalize_repo(api_request(url))
        except RuntimeError as e:
            print(f"skip {full_name}: {e}", file=sys.stderr)
        if i % 20 == 0:
            time.sleep(1)


def nearest_snapshot_on_or_before(target: date) -> tuple[date, dict[str, Any]] | None:
    best: tuple[date, dict[str, Any]] | None = None
    for path in SNAPSHOT_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if d > target:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if best is None or d > best[0]:
            best = (d, data)
    return best


def pick_baseline(today: date, days: int) -> tuple[date, dict[str, Any]] | None:
    """Prefer ~N days ago; otherwise fall back to the newest snapshot strictly before today."""
    preferred = nearest_snapshot_on_or_before(today - timedelta(days=days))
    if preferred and preferred[0] < today:
        return preferred
    return nearest_snapshot_on_or_before(today - timedelta(days=1))


def format_stars(n: int) -> str:
    if n >= 1000:
        val = n / 1000
        text = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(n)


def growth_vs(repos_today: dict[str, dict[str, Any]], snap: dict[str, Any] | None) -> dict[str, int]:
    if not snap:
        return {name: 0 for name in repos_today}
    old_repos = snap.get("repos") or {}
    out: dict[str, int] = {}
    for name, row in repos_today.items():
        old = old_repos.get(name)
        if old is None:
            # New to our tracker: don't treat full star count as "weekly growth"
            out[name] = 0
        else:
            out[name] = max(0, row["stars"] - int(old.get("stars") or 0))
    return out


def build_ranking(
    repos: dict[str, dict[str, Any]],
    week_growth: dict[str, int],
    month_growth: dict[str, int],
) -> list[dict[str, Any]]:
    rows = []
    for name, repo in repos.items():
        rows.append(
            {
                **repo,
                "week_growth": week_growth.get(name, 0),
                "month_growth": month_growth.get(name, 0),
            }
        )
    rows.sort(key=lambda r: (r["week_growth"], r["stars"]), reverse=True)
    return rows


def render_readme(
    today: date,
    week_start: date,
    rows: list[dict[str, Any]],
    note: str,
) -> str:
    top = rows[:TOP_N]
    best = top[0]["full_name"].split("/")[-1] if top else "—"
    lines: list[str] = [
        f"## {today.isoformat()} 本周最佳开源项目🔝:{best}",
        "",
        "> 个人视野雷达：按近 7 日 Star 增长自动更新（GitHub Actions）",
        "",
        f"> 🏆{today.strftime('%Y.%m.%d')}周榜最佳项目前{min(TOP_HIGHLIGHT, len(top))}名",
        "",
    ]

    for i, row in enumerate(top[:TOP_HIGHLIGHT], 1):
        desc = row["description"] or "暂无描述"
        lines.extend(
            [
                f"* **榜单增长：第{i}名: {row['full_name']} **",
                f"    * 开源地址：<{row['html_url']}>",
                f"    * 📅 开源时间：{row['created_at'] or '—'}",
                f"    * ⭐ 总星标数量：{row['stars']}⭐",
                f"    * 🔺周Star增长量：{row['week_growth']}⭐",
                f"    * 📝 项目描述： {desc}",
                "",
            ]
        )

    lines.extend(
        [
            f"## {week_start.strftime('%Y.%m.%d')}-{today.strftime('%Y.%m.%d')} 周榜排行",
            "",
            "| 排名 | 项目名 | Star⭐ | 上周增长量 |",
            "| -- | --- | --- | --- |",
        ]
    )
    for i, row in enumerate(top, 1):
        link = f"[{row['full_name']}]({row['html_url']})"
        lines.append(
            f"| {i} | {link} | {format_stars(row['stars'])} | 🔺{row['week_growth']} |"
        )

    lines.extend(
        [
            "",
            f"**注**: {note}",
            "",
            f"## {week_start.strftime('%Y.%m.%d')}-{today.strftime('%Y.%m.%d')} 周榜项目详情",
            "",
        ]
    )

    for i, row in enumerate(top, 1):
        lines.extend(
            [
                f"### {i}. <{row['html_url']}>",
                "",
                f"* ⭐ 总星标数量：{format_stars(row['stars'])}",
                f"* 🔺 上周增长数量：{row['week_growth']}⭐",
                f"* 🔺 上月增长数量：{row['month_growth']}⭐",
                f"* 📅 开源时间：{row['created_at'] or '—'}",
                f"* 📝 项目描述：{row['description'] or '暂无描述'}",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "由 [everydaynew](.) 自动生成 · 数据来自 GitHub API  ·  [使用说明](docs/SETUP.md)",
            "",
        ]
    )
    return "\n".join(lines)


def save_snapshot(today: date, repos: dict[str, dict[str, Any]]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(repos),
        "repos": repos,
    }
    (SNAPSHOT_DIR / f"{today.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cutoff = today - timedelta(days=60)
    for old in SNAPSHOT_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(old.stem)
        except ValueError:
            continue
        if d < cutoff:
            old.unlink(missing_ok=True)


def trim_repos(repos: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ranked = sorted(repos.values(), key=lambda r: r.get("stars", 0), reverse=True)
    return {r["full_name"]: r for r in ranked[:MAX_TRACKED]}


def main() -> int:
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=6)
    print(f"updating rank for {today.isoformat()}")

    fresh = search_candidates(today)
    historical = load_recent_history(14)
    repos = trim_repos({**historical, **fresh})
    refresh_stale(repos, set(fresh.keys()))
    repos = trim_repos(repos)

    # Compare against historical snapshots before overwriting today
    week_snap = pick_baseline(today, 7)
    month_snap = pick_baseline(today, 30)

    save_snapshot(today, repos)

    week_growth = growth_vs(repos, week_snap[1] if week_snap else None)
    month_growth = growth_vs(repos, month_snap[1] if month_snap else None)
    rows = build_ranking(repos, week_growth, month_growth)

    if week_snap:
        age = (today - week_snap[0]).days
        if age >= 6:
            note = (
                f"周增长对比快照日期：{week_snap[0].isoformat()}（约 {age} 天前）；"
                f"候选库约 {len(repos)} 个；每天 UTC 00:00（北京时间 08:00）自动更新。"
            )
        else:
            note = (
                f"历史不足 7 天，当前增长对比快照：{week_snap[0].isoformat()}（{age} 天前观测增量）；"
                f"连续运行约一周后即为完整周榜。候选库约 {len(repos)} 个。"
            )
    else:
        note = (
            "尚无可用历史快照，周增长暂为 0。"
            "连续运行数天后即可看到真实飙升榜；也可在 Actions 里手动 Run workflow。"
        )

    README_PATH.write_text(
        render_readme(today, week_start, rows, note),
        encoding="utf-8",
    )

    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(
        json.dumps(
            {
                "last_update": today.isoformat(),
                "repo_count": len(repos),
                "week_snapshot": week_snap[0].isoformat() if week_snap else None,
                "top": [
                    {
                        "rank": i,
                        "full_name": r["full_name"],
                        "stars": r["stars"],
                        "week_growth": r["week_growth"],
                    }
                    for i, r in enumerate(rows[:TOP_N], 1)
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"wrote {README_PATH} with top {min(TOP_N, len(rows))} repos")
    if rows:
        print(f"#1 {rows[0]['full_name']} stars={rows[0]['stars']} week=+{rows[0]['week_growth']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
