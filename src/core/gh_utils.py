import json
import os
from pathlib import Path

from src.core.config import CONFIG_PATH, load_toml, parse_app_entries, parse_config
from src.core.logger import abort, epr, wpr
from src.core.network import NetworkManager, ResourceNotFoundError

def _fetch_latest_release(source: str, net: NetworkManager) -> tuple[str, str]:
    gitlab = source.startswith("gitlab:")
    if source.startswith("github:") or source.startswith("gitlab:"):
        clean_src = source.split(":", 1)[1]
    else:
        clean_src = source

    if gitlab:
        project = clean_src.replace("/", "%2F")
        url = f"https://gitlab.com/api/v4/projects/{project}/releases/permalink/latest"
        rel = json.loads(net.get(url))
        return rel.get("description", "") or "", rel.get("released_at", "") or ""
    else:
        url = f"https://api.github.com/repos/{clean_src}/releases/latest"
        rel = json.loads(net.get(url, headers=net._gh_headers))
        return rel.get("body", "") or "", rel.get("published_at", "") or ""


def get_matrix(source: str) -> None:
    data = load_toml(CONFIG_PATH)
    main_cfg = parse_config(data)
    source_lower = source.lower()

    is_auto = os.getenv("IS_AUTO", "false").lower() == "true"
    build_changed_only = data.get("build-changed-only", [])
    filter_by_changelog = is_auto and (source_lower in [str(b).lower() for b in build_changed_only])

    changelog_text = ""
    if filter_by_changelog:
        patches_source = ""
        for entry in parse_app_entries(data, main_cfg):
            if entry.enabled and entry.brand.lower() == source_lower:
                patches_source = entry.patches_source
                break
        
        if patches_source:
            with NetworkManager() as net:
                repo = os.getenv("GITHUB_REPOSITORY")
                if repo:
                    try:
                        our_releases_raw = net.get(f"https://api.github.com/repos/{repo}/releases?per_page=100", headers=net._gh_headers)
                        our_date = ""
                        for rel in json.loads(our_releases_raw):
                            tag = rel.get("tag_name", "")
                            brand = tag.split("-", 1)[1] if "-" in tag else ""
                            if brand.lower() == source_lower:
                                our_date = rel.get("published_at", "") or ""
                                break
                        if not our_date:
                            filter_by_changelog = False
                    except Exception:
                        pass

                if filter_by_changelog:
                    try:
                        changelog_text, _ = _fetch_latest_release(patches_source, net)
                    except Exception as exc:
                        epr(f"Failed to fetch changelog for '{patches_source}': {exc}")
                        filter_by_changelog = False

    include: list[dict[str, str]] = []
    for entry in parse_app_entries(data, main_cfg):
        if not entry.enabled or entry.brand.lower() != source_lower:
            continue

        if filter_by_changelog and changelog_text:
            if not any(kw in changelog_text.lower() for kw in entry.changelog_keywords):
                continue

        if entry.arch == "both":
            include.extend([{"id": entry.table, "arch": "arm64-v8a"}, {"id": entry.table, "arch": "armeabi-v7a"}])
        else:
            include.append({"id": entry.table})

    if not include:
        abort(f"No apps found for patch source '{source}'")

    print(json.dumps({"include": include}, ensure_ascii=False))

def check_builds_needed(force_all: bool = False) -> None:
    data = load_toml(CONFIG_PATH)
    main_cfg = parse_config(data)
    seen: dict[str, str] = {}
    for entry in parse_app_entries(data, main_cfg):
        if not entry.enabled:
            continue
        brand = entry.brand.lower()
        if brand not in seen:
            seen[brand] = entry.patches_source

    if not seen:
        print(json.dumps([]))
        return

    if force_all:
        print(json.dumps(list(seen.keys())))
        return

    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        abort("GITHUB_REPOSITORY environment variable is not set")

    with NetworkManager() as net:
        our_releases_by_brand: dict[str, str] = {}
        try:
            our_releases_raw = net.get(f"https://api.github.com/repos/{repo}/releases?per_page=100", headers=net._gh_headers)
            for rel in json.loads(our_releases_raw):
                tag = rel.get("tag_name", "")
                brand = tag.split("-", 1)[1] if "-" in tag else ""
                if brand in seen and brand not in our_releases_by_brand:
                    our_releases_by_brand[brand] = rel.get("published_at", "") or ""
        except Exception as exc:
            epr(f"Failed to fetch our releases: {exc}")
            our_releases_by_brand = {}

        brands_to_build: list[str] = []
        for brand, patches_source in seen.items():
            our_date = our_releases_by_brand.get(brand, "")
            upstream_date = ""
            changelog_text = ""
            try:
                changelog_text, upstream_date = _fetch_latest_release(patches_source, net)
            except ResourceNotFoundError:
                epr(f"No upstream release found for '{patches_source}', skipping brand '{brand}'")
                continue
            except Exception as exc:
                epr(f"Failed to fetch upstream release for '{patches_source}': {exc}")
                brands_to_build.append(brand)
                continue

            if not our_date or upstream_date > our_date:
                build_changed_only = data.get("build-changed-only", [])
                if our_date and brand in [str(b).lower() for b in build_changed_only]:
                    has_apps = False
                    for app in parse_app_entries(data, main_cfg):
                        if app.enabled and app.brand.lower() == brand:
                            if any(kw in changelog_text.lower() for kw in app.changelog_keywords):
                                has_apps = True
                                break
                    if not has_apps:
                        continue
                brands_to_build.append(brand)

    print(json.dumps(brands_to_build))

def _parse_log_file(log: Path, green_lines: list[str], collected: list[str]) -> str:
    microg_line = ""
    capturing = False
    current: list[str] = []
    with log.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("- 🟢"):
                green_lines.append(f"{line}  ")
            elif not microg_line and line.startswith("▶️") and "MicroG" in line:
                microg_line = line

            if line.startswith(">") and "CLI:" in line:
                capturing = True
                current = []

            if capturing:
                current.append(f"{line}  ")
                if line.startswith("[") and "Changelog]" in line:
                    collected.append("\n".join(current))
                    capturing = False

    if capturing:
        wpr(f"Unclosed CLI section in '{log}', changelog end marker not found")

    return microg_line

def combine_logs(logs_dir: Path | str) -> None:
    logs = sorted(Path(logs_dir).rglob("build*.md"))
    if not logs:
        return

    green_lines: list[str] = []
    collected: list[str] = []
    microg_line = ""
    for log in logs:
        m_line = _parse_log_file(log, green_lines, collected)
        if not microg_line:
            microg_line = m_line

    if green_lines:
        print("\n".join(green_lines), end="\n\n")

    if microg_line:
        print(microg_line, end="\n\n")

    if unique := list(dict.fromkeys(collected)):
        print("\n\n".join(unique))