#!/usr/bin/env python3
"""Regenerate the auto-updating parts of the profile README.

Two tables, each rewritten between HTML markers in README.md, both driven by the
GitHub Search/REST API and sorted by upstream stars:
  * Open Source Contributions (``OSS-CONTRIBUTIONS``) — merged PRs to external repos.
  * In Review (``OSS-IN-REVIEW``) — currently-open PRs to external repos.

Runs locally (``GH_TOKEN=$(gh auth token) python scripts/update_profile.py``)
and in CI via .github/workflows/update-profile.yml.
"""
from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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
# Cap each table so it stays scannable.
MAX_ROWS = 15

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README_PATH = os.path.join(ROOT, "README.md")

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


def search_open_prs() -> list[dict]:
    """Every currently-open PR authored by the user (paginated)."""
    items: list[dict] = []
    page = 1
    while True:
        data = api_get(
            "/search/issues",
            {
                "q": f"author:{USER} type:pr is:open is:public",
                "per_page": 100,
                "page": page,
                "sort": "created",
                "order": "desc",
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


def _collect_rows(prs: list[dict], exclude: set[str], star_cache: dict[str, int],
                  sort_key: str) -> list[dict]:
    """Group PRs by external repo, attach upstream stars, keep those clearing the
    star threshold, and return rows sorted by stars (desc). ``star_cache`` is
    shared across tables so each repo is looked up at most once."""
    by_repo: dict[str, dict] = {}
    for pr in prs:
        full_name = pr["repository_url"].split("/repos/")[-1]  # owner/repo
        owner = full_name.split("/")[0].lower()
        if owner in exclude:
            continue
        by_repo.setdefault(full_name, {"prs": []})["prs"].append(pr)

    rows: list[dict] = []
    for full_name, repo in by_repo.items():
        if full_name not in star_cache:
            try:
                info = api_get(f"/repos/{full_name}")
            except Exception as exc:  # noqa: BLE001 - skip repos we can't read
                print(f"  skip {full_name}: {exc}", file=sys.stderr)
                continue
            star_cache[full_name] = info.get("stargazers_count", 0)
        stars = star_cache[full_name]
        if stars < MIN_STARS:
            continue
        prs_sorted = sorted(repo["prs"], key=lambda p: p.get(sort_key) or "", reverse=True)
        latest = prs_sorted[0]
        rows.append(
            {
                "full_name": full_name,
                "repo_url": f"https://github.com/{full_name}",
                "stars": stars,
                "pr_number": latest["number"],
                "pr_url": latest["html_url"],
                "pr_title": md_escape(latest["title"]),
                "count": len(prs_sorted),
            }
        )
    rows.sort(key=lambda r: r["stars"], reverse=True)
    return rows


def _render_table(rows: list[dict]) -> list[str]:
    lines = ["| Project | Stars | Contribution |", "| --- | --- | --- |"]
    for r in rows[:MAX_ROWS]:
        extra = f" · +{r['count'] - 1} more" if r["count"] > 1 else ""
        lines.append(
            f"| [{r['full_name']}]({r['repo_url']}) | ⭐ {fmt_stars(r['stars'])} | "
            f"[#{r['pr_number']}]({r['pr_url']}) {r['pr_title']}{extra} |"
        )
    return lines


def build_oss_table(prs: list[dict], exclude: set[str], star_cache: dict[str, int]) -> str:
    rows = _collect_rows(prs, exclude, star_cache, sort_key="closed_at")
    total_stars = sum(r["stars"] for r in rows)
    lines = [
        f"<p><strong>🌟 {fmt_stars(total_stars)}+ stars reached"
        f" &nbsp;·&nbsp; {len(rows)} open-source projects"
        f" &nbsp;·&nbsp; {sum(r['count'] for r in rows)} merged PRs</strong></p>",
        "",
        *_render_table(rows),
    ]
    return "\n".join(lines)


def build_inreview_table(prs: list[dict], exclude: set[str], star_cache: dict[str, int]) -> str:
    rows = _collect_rows(prs, exclude, star_cache, sort_key="created_at")
    if not rows:
        return "_No open pull requests in review right now._"
    total_stars = sum(r["stars"] for r in rows)
    lines = [
        f"<p><strong>🔍 {sum(r['count'] for r in rows)} open PRs in review"
        f" &nbsp;·&nbsp; {len(rows)} projects"
        f" &nbsp;·&nbsp; ⭐ {fmt_stars(total_stars)}+ combined</strong></p>",
        "",
        *_render_table(rows),
    ]
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
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    if not TOKEN:
        print("warning: no GH_TOKEN/GITHUB_TOKEN set — requests may be rate limited",
              file=sys.stderr)

    print("Fetching merged pull requests...")
    prs = search_merged_prs()
    print(f"  {len(prs)} merged PRs")

    print("Fetching open pull requests...")
    open_prs = search_open_prs()
    print(f"  {len(open_prs)} open PRs")

    exclude = EXCLUDE_OWNERS | fetch_public_orgs()
    star_cache: dict[str, int] = {}

    print("Building open-source contributions table...")
    table = build_oss_table(prs, exclude, star_cache)
    print("Building in-review table...")
    inreview = build_inreview_table(open_prs, exclude, star_cache)

    readme = open(README_PATH, encoding="utf-8").read()
    readme = replace_marked(readme, "OSS-CONTRIBUTIONS", table)
    readme = replace_marked(readme, "OSS-IN-REVIEW", inreview)
    with open(README_PATH, "w", encoding="utf-8") as fh:
        fh.write(readme)
    print(f"  wrote {README_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
