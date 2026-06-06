#!/usr/bin/env python3
"""Regenerate the auto-updating parts of the profile README.

Two outputs, both driven by the GitHub Search/REST API:

1. Open Source Contributions table — merged pull requests to repositories the
   user does not own, sorted by upstream stars. Rewritten between the
   ``<!-- OSS-CONTRIBUTIONS:START -->`` / ``...:END -->`` markers in README.md.
2. Merged-PR activity chart — a cumulative line chart of every merged PR over
   time, written to assets/pr-activity.svg.

Runs locally (``GH_TOKEN=$(gh auth token) python scripts/update_profile.py``)
and in CI via .github/workflows/update-profile.yml.
"""
from __future__ import annotations

import calendar
import gzip
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

USER = "sueun-dev"

# Repos owned by the user or by orgs the user belongs to are *their* work, not
# "contributions to others" — exclude them from the OSS table. Public orgs are
# also fetched at runtime and merged into this set.
EXCLUDE_OWNERS = {
    USER.lower(),
    "bbaguette-world",
    "contract-labs",
    "shorti-a-short-but-never-too-short-trip",
    "apt-alcohol-prevention-training",
    "kitchen-kompanion",
}

# Only surface contributions to repos with at least this many stars, so that
# personal/school/demo repos don't crowd out the real open-source work.
MIN_STARS = 10
# Cap the table so it stays scannable.
MAX_ROWS = 12

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README_PATH = os.path.join(ROOT, "README.md")
SVG_PATH = os.path.join(ROOT, "assets", "pr-activity.svg")

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": f"{USER}-profile-updater",
    "X-GitHub-Api-Version": "2022-11-28",
    # Compress on the wire: smaller payloads avoid truncated reads on large pages.
    "Accept-Encoding": "gzip",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


# --------------------------------------------------------------------------- #
# GitHub API helpers
# --------------------------------------------------------------------------- #
def api_get(path: str, params: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_err: Exception | None = None
    for attempt in range(5):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            last_err = exc
            # Secondary / primary rate limits → back off and retry.
            if exc.code in (403, 429):
                retry_after = exc.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else 8 * (attempt + 1)
                print(f"  rate limited ({exc.code}); sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (http.client.IncompleteRead, urllib.error.URLError, ConnectionError) as exc:
            # Transient truncated read / network blip → retry.
            last_err = exc
            print(f"  transient error ({exc}); retrying", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
            continue
    raise RuntimeError(f"GET {url} failed: {last_err}")


def search_merged_prs() -> list[dict]:
    """Every merged PR authored by the user (paginated)."""
    items: list[dict] = []
    page = 1
    while True:
        data = api_get(
            "/search/issues",
            {
                "q": f"author:{USER} type:pr is:merged",
                "per_page": 100,
                "page": page,
                "sort": "created",
                "order": "asc",
            },
        )
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or len(items) >= data.get("total_count", 0):
            break
        page += 1
        if page > 10:  # Search API hard cap is 1000 results.
            break
    return items


def fetch_public_orgs() -> set[str]:
    try:
        orgs = api_get(f"/users/{USER}/orgs")
        return {o["login"].lower() for o in orgs}
    except Exception as exc:  # noqa: BLE001 - best effort, never fatal
        print(f"  could not fetch orgs: {exc}", file=sys.stderr)
        return set()


# --------------------------------------------------------------------------- #
# Open Source Contributions table
# --------------------------------------------------------------------------- #
def fmt_stars(n: int) -> str:
    if n >= 1000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(n)


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").strip()


def build_oss_table(prs: list[dict], exclude: set[str]) -> str:
    # Group merged PRs by repository.
    by_repo: dict[str, dict] = {}
    for pr in prs:
        full_name = pr["repository_url"].split("/repos/")[-1]  # owner/repo
        owner = full_name.split("/")[0].lower()
        if owner in exclude:
            continue
        repo = by_repo.setdefault(full_name, {"prs": []})
        repo["prs"].append(pr)

    # Look up stars once per repo and keep those clearing the threshold.
    star_cache: dict[str, int] = {}
    rows: list[dict] = []
    for full_name, repo in by_repo.items():
        try:
            info = api_get(f"/repos/{full_name}")
        except Exception as exc:  # noqa: BLE001 - skip repos we can't read
            print(f"  skip {full_name}: {exc}", file=sys.stderr)
            continue
        stars = info.get("stargazers_count", 0)
        star_cache[full_name] = stars
        if stars < MIN_STARS:
            continue
        prs_sorted = sorted(repo["prs"], key=lambda p: p.get("closed_at") or "", reverse=True)
        latest = prs_sorted[0]
        rows.append(
            {
                "full_name": full_name,
                "repo_url": info.get("html_url", f"https://github.com/{full_name}"),
                "stars": stars,
                "pr_number": latest["number"],
                "pr_url": latest["html_url"],
                "pr_title": md_escape(latest["title"]),
                "count": len(prs_sorted),
            }
        )

    rows.sort(key=lambda r: r["stars"], reverse=True)
    rows = rows[:MAX_ROWS]

    lines = ["| Project | Stars | Contribution |", "| --- | --- | --- |"]
    for r in rows:
        extra = f" · +{r['count'] - 1} more" if r["count"] > 1 else ""
        lines.append(
            f"| [{r['full_name']}]({r['repo_url']}) | ⭐ {fmt_stars(r['stars'])} | "
            f"[#{r['pr_number']}]({r['pr_url']}) {r['pr_title']}{extra} |"
        )
    return "\n".join(lines)


def replace_marked(content: str, marker: str, body: str) -> str:
    start = f"<!-- {marker}:START -->"
    end = f"<!-- {marker}:END -->"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end), re.DOTALL
    )
    replacement = f"{start}\n{body}\n{end}"
    if not pattern.search(content):
        raise RuntimeError(f"markers for {marker} not found in README")
    return pattern.sub(replacement, content)


# --------------------------------------------------------------------------- #
# Cumulative merged-PR SVG chart
# --------------------------------------------------------------------------- #
def monthly_cumulative(prs: list[dict]) -> list[tuple[str, int]]:
    """Return [(YYYY-MM, cumulative_count)] from first merged month to now."""
    per_month: dict[str, int] = defaultdict(int)
    for pr in prs:
        when = pr.get("closed_at") or pr.get("created_at")
        if not when:
            continue
        per_month[when[:7]] += 1
    if not per_month:
        return []

    first = min(per_month)
    now = datetime.now(timezone.utc)
    y, m = int(first[:4]), int(first[5:7])
    series: list[tuple[str, int]] = []
    cumulative = 0
    while (y, m) <= (now.year, now.month):
        key = f"{y:04d}-{m:02d}"
        cumulative += per_month.get(key, 0)
        series.append((key, cumulative))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return series


def nice_scale(maxval: int) -> tuple[int, int]:
    """Return (axis_max, step) giving 3-6 gridlines for a clean y-axis."""
    if maxval <= 0:
        return 1, 1
    for step in [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000]:
        ticks = math.ceil(maxval / step)
        if 3 <= ticks <= 6:
            return step * ticks, step
    step = 10 ** math.ceil(math.log10(maxval))
    return step, max(step // 5, 1)


def month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{calendar.month_abbr[int(m)]} '{y[2:]}"


def build_svg(series: list[tuple[str, int]], total: int) -> str:
    W, H = 800, 220
    L, R, T, B = 48, 24, 46, 34
    plot_w, plot_h = W - L - R, H - T - B
    baseline = T + plot_h

    ymax, step = nice_scale(series[-1][1] if series else 1)
    n = len(series)

    def px(i: int) -> float:
        return L + (plot_w / 2 if n == 1 else plot_w * i / (n - 1))

    def py(v: int) -> float:
        return baseline - plot_h * v / ymax

    pts = [(px(i), py(v)) for i, (_, v) in enumerate(series)]

    line = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"M {pts[0][0]:.1f},{baseline:.1f} "
        + "L " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        + f" L {pts[-1][0]:.1f},{baseline:.1f} Z"
    )

    # Horizontal gridlines + y labels.
    grid = []
    v = 0
    while v <= ymax:
        y = py(v)
        grid.append(
            f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" '
            f'stroke="#1e2633" stroke-width="1"/>'
            f'<text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="ax">{v}</text>'
        )
        v += step

    # ~6 x labels, evenly spaced.
    xlabels = []
    label_count = min(6, n)
    if label_count > 0:
        idxs = (
            [0]
            if label_count == 1
            else [round(i * (n - 1) / (label_count - 1)) for i in range(label_count)]
        )
        ordered = sorted(set(idxs))
        for i in ordered:
            # Keep the edge labels inside the canvas instead of centering them.
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            xlabels.append(
                f'<text x="{px(i):.1f}" y="{baseline + 18:.1f}" text-anchor="{anchor}" '
                f'class="ax">{month_label(series[i][0])}</text>'
            )

    end_x, end_y = pts[-1]
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" \
width="{W}" height="{H}" role="img" aria-label="Cumulative merged pull requests over time">
  <defs>
    <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#7aa2f7" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#7aa2f7" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="stroke" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#bb9af7"/>
      <stop offset="100%" stop-color="#7aa2f7"/>
    </linearGradient>
    <style>
      text {{ font-family: 'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif; }}
      .title {{ fill: #c9d1d9; font-size: 15px; font-weight: 600; }}
      .total {{ fill: #bb9af7; font-size: 13px; font-weight: 600; }}
      .ax {{ fill: #6b7785; font-size: 10px; }}
      .upd {{ fill: #3d4754; font-size: 9px; }}
    </style>
  </defs>
  <rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10"
        fill="#0d1117" stroke="#1e2633"/>
  <text x="20" y="28" class="title">Merged Pull Requests Over Time</text>
  <text x="{W - R}" y="28" text-anchor="end" class="total">{total} merged</text>
  {''.join(grid)}
  <path d="{area}" fill="url(#area)"/>
  <path d="{line}" fill="none" stroke="url(#stroke)" stroke-width="2.5"
        stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="4" fill="#bb9af7"
          stroke="#0d1117" stroke-width="2"/>
  {''.join(xlabels)}
  <text x="{W - R}" y="{H - 10}" text-anchor="end" class="upd">updated {updated}</text>
</svg>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    if not TOKEN:
        print("warning: no GH_TOKEN/GITHUB_TOKEN set — requests may be rate limited",
              file=sys.stderr)

    print("Fetching merged pull requests...")
    prs = search_merged_prs()
    print(f"  {len(prs)} merged PRs")

    exclude = EXCLUDE_OWNERS | fetch_public_orgs()

    print("Building open-source contributions table...")
    table = build_oss_table(prs, exclude)
    readme = open(README_PATH, encoding="utf-8").read()
    readme = replace_marked(readme, "OSS-CONTRIBUTIONS", table)
    with open(README_PATH, "w", encoding="utf-8") as fh:
        fh.write(readme)
    print(f"  wrote {README_PATH}")

    print("Rendering merged-PR activity chart...")
    series = monthly_cumulative(prs)
    if series:
        svg = build_svg(series, len(prs))
        os.makedirs(os.path.dirname(SVG_PATH), exist_ok=True)
        with open(SVG_PATH, "w", encoding="utf-8") as fh:
            fh.write(svg)
        print(f"  wrote {SVG_PATH} ({series[-1][1]} cumulative over {len(series)} months)")
    else:
        print("  no PR history to chart")

    return 0


if __name__ == "__main__":
    sys.exit(main())
