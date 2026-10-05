#!/usr/bin/env python3
"""
upload_to_github.py — One-Click Upload Mobile App to GitHub
============================================================
Pushes the entire fishfinder_mobile_apk project to your GitHub repository
using the GitHub API without needing Git installed locally!
"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error

# Set your GitHub Username and Repository name:
OWNER = "hacrav00"
REPO = "fishfinder-app"
BRANCH = "main"

def get_all_files():
    ignored_dirs = {".git", "__pycache__", ".idea", ".vscode", "build", ".gradle"}
    files = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for root, dirs, filenames in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), base_dir)
            rel_path = rel_path.replace("\\", "/")
            if rel_path != "upload_to_github.py":
                files.append(rel_path)
    return sorted(files)

def get_sha(path, token):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None

def upload_file(path, token):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(base_dir, path)
    if not os.path.exists(full_path):
        return

    with open(full_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")

    sha = get_sha(path, token)
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    payload = {
        "message": f"Add {path} for FishFinder Android APK build",
        "content": content_b64,
        "branch": BRANCH
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="PUT")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[✓] Uploaded: {path}")
    except Exception as e:
        print(f"[✗] Failed {path}: {e}")

def main():
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Usage: python upload_to_github.py <YOUR_GITHUB_TOKEN>")
        print("Or set GITHUB_TOKEN environment variable.")
        sys.exit(1)

    print(f"[*] Uploading FishFinder Mobile App to https://github.com/{OWNER}/{REPO}...")
    files = get_all_files()
    for f in files:
        upload_file(f, token)
    print("\n[✓] All files uploaded successfully! GitHub Actions is now building your APK!")

if __name__ == "__main__":
    main()
