"""Comparación de commits por ascendencia, nunca por el valor o la fecha del hash."""

import json
import os
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def clean_version(value):
    value = str(value or "").lstrip("\ufeff").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else None


def same_version(first, second):
    first, second = clean_version(first), clean_version(second)
    return bool(first and second and (first.startswith(second) or second.startswith(first)))


def _git(repo_dir, *args):
    options = {"capture_output": True, "text": True, "timeout": 5, "shell": False}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(["git", "-C", str(repo_dir), *args], **options)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _local_relation(local, remote, repo_dir):
    if repo_dir is None:
        return None
    # Si un commit no existe localmente no inferimos divergencia. GitHub podrá
    # resolverlo en instalaciones sin repo, si ambas versiones se han publicado.
    for version in (local, remote):
        result = _git(repo_dir, "rev-parse", "--verify", f"{version}^{{commit}}")
        if result is None or result.returncode != 0:
            return None
    forward = _git(repo_dir, "merge-base", "--is-ancestor", local, remote)
    if forward is None or forward.returncode not in (0, 1):
        return None
    if forward.returncode == 0:
        return "remote_ahead"
    backward = _git(repo_dir, "merge-base", "--is-ancestor", remote, local)
    if backward is None or backward.returncode not in (0, 1):
        return None
    if backward.returncode == 0:
        return "local_ahead"
    shallow = _git(repo_dir, "rev-parse", "--is-shallow-repository")
    if shallow is not None and shallow.returncode == 0 and shallow.stdout.strip() == "false":
        return "diverged"
    return None


def compare_versions(local, remote, repo_dir=None, github_repo="gcg9898/app-jira"):
    """equal/local_ahead/remote_ahead/diverged/unknown, respecto al EXE publicado.

    Git local permite reconocer versiones locales aún sin publicar. En el EXE
    distribuido no hace falta Git: se usa la API compare de GitHub. Un 404,
    rate limit o ausencia de conexión NO se interpreta como actualización.
    """
    local, remote = clean_version(local), clean_version(remote)
    if not local or not remote:
        return "unknown"
    if same_version(local, remote):
        return "equal"
    relation = _local_relation(local, remote, repo_dir)
    if relation:
        return relation
    url = f"https://api.github.com/repos/{github_repo}/compare/{local}...{remote}"
    try:
        request = Request(url, headers={"User-Agent": "JiraBoard-Updater",
                                         "Accept": "application/vnd.github+json",
                                         "Cache-Control": "no-cache"})
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        # En compare/base...head, 'ahead' significa que HEAD (la remota) avanza.
        return {"ahead": "remote_ahead", "behind": "local_ahead", "identical": "equal",
                "diverged": "diverged"}.get(result.get("status"), "unknown") if isinstance(result, dict) else "unknown"
    except (URLError, HTTPError, OSError, ValueError):
        return "unknown"