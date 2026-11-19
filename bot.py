import csv
import json
import os
import time
import logging
from collections import Counter
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("PERSONAL_GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER", "")
SEARCH_QUERY = os.getenv("SEARCH_QUERY", "stars:>100 pushed:>2026-10-20")
MAX_REPOSITORIES = max(1, min(int(os.getenv("MAX_REPOSITORIES", "10")), 30))
MAX_STARGAZERS_PER_REPO = max(1, min(int(os.getenv("MAX_STARGAZERS_PER_REPO", "100")), 100))
OUTPUT_DIR = "discovery"
API = "https://api.github.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("GitHubTrendResearcher")

if not TOKEN:
    raise EnvironmentError("PERSONAL_GITHUB_TOKEN is required.")

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "GitHub-Trend-Researcher/1.0",
})


def api_get(path, params=None):
    response = SESSION.get(f"{API}{path}", params=params, timeout=30)
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        reset = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
        wait = max(1, reset - int(time.time()) + 1)
        log.warning("GitHub API rate limit reached; waiting %s seconds", wait)
        time.sleep(wait)
        response = SESSION.get(f"{API}{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def discover_repositories():
    data = api_get("/search/repositories", {
        "q": SEARCH_QUERY,
        "sort": "stars",
        "order": "desc",
        "per_page": MAX_REPOSITORIES,
    })
    repos = data.get("items", [])
    log.info("Found %s trending repositories", len(repos))
    return repos


def collect_stargazers(repositories):
    scores = Counter()
    starred_repos = {}
    for repo in repositories:
        full_name = repo["full_name"]
        log.info("Reading stargazers: %s", full_name)
        try:
            users = api_get(f"/repos/{full_name}/stargazers", {
                "per_page": MAX_STARGAZERS_PER_REPO,
            })
        except requests.RequestException as exc:
            log.warning("Could not read stargazers for %s: %s", full_name, exc)
            continue
        for user in users:
            login = user.get("login")
            if login and login.lower() != GITHUB_USER.lower():
                scores[login] += 1
                starred_repos.setdefault(login, []).append(full_name)
    return scores, starred_repos


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    repositories = discover_repositories()
    scores, starred_repos = collect_stargazers(repositories)

    candidates = []
    for login, matched_count in scores.most_common():
        try:
            profile = api_get(f"/users/{login}")
        except requests.RequestException as exc:
            log.warning("Skipping profile %s: %s", login, exc)
            continue

        if profile.get("type") != "User" or profile.get("site_admin"):
            continue

        candidates.append({
            "login": login,
            "profile_url": profile.get("html_url", f"https://github.com/{login}"),
            "name": profile.get("name") or "",
            "bio": profile.get("bio") or "",
            "company": profile.get("company") or "",
            "location": profile.get("location") or "",
            "public_repos": profile.get("public_repos", 0),
            "followers": profile.get("followers", 0),
            "account_created": profile.get("created_at", ""),
            "matched_trending_repositories": matched_count,
            "starred_repositories": "; ".join(starred_repos.get(login, [])),
        })

    candidates.sort(
        key=lambda row: (
            row["matched_trending_repositories"],
            row["public_repos"],
            row["followers"],
        ),
        reverse=True,
    )

    json_path = os.path.join(OUTPUT_DIR, "candidates.json")
    csv_path = os.path.join(OUTPUT_DIR, "candidates.csv")
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "search_query": SEARCH_QUERY,
            "repositories": [
                {
                    "full_name": repo["full_name"],
                    "url": repo["html_url"],
                    "stars": repo["stargazers_count"],
                    "language": repo.get("language"),
                    "description": repo.get("description"),
                }
                for repo in repositories
            ],
            "candidates": candidates,
        }, file, ensure_ascii=False, indent=2)

    columns = list(candidates[0].keys()) if candidates else [
        "login", "profile_url", "matched_trending_repositories", "starred_repositories"
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(candidates)

    log.info("Saved %s candidate profiles to %s and %s", len(candidates), csv_path, json_path)
    log.info("Discovery only: no follow/unfollow actions are performed.")


if __name__ == "__main__":
    main()
