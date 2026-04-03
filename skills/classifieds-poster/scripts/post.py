#!/usr/bin/env python3
"""
Post a classified ad to PSP Classifieds via Groups.io API.

Usage:
  post.py --subject "FF: Item" --body "Description" --image photo.jpg
  post.py --subject "FF: Item" --body "Description"   (no image)
  post.py --dry-run --subject "..." --body "..."       (validate only, don't post)
  post.py --status                                     (check recent posts & drafts)

The script reads the API key from ~/.openclaw/workspace/.env.classifieds
(PSP_API_KEY). Exit codes: 0 = posted/ok, 1 = error, 2 = pending moderation.
"""

import argparse
import json
import mimetypes
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
POST_LOG = DATA_DIR / "posts.jsonl"
ENV_FILE = Path.home() / ".openclaw" / "workspace" / ".env.classifieds"
API_BASE = "https://groups.io/api/v1"
CLASSIFIEDS_GROUP_ID = 8407

# Build SSL context — use certifi if available, fall back to system certs
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.load_default_certs()
HTTPS_HANDLER = urllib.request.HTTPSHandler(context=SSL_CTX)
OPENER = urllib.request.build_opener(HTTPS_HANDLER)
urllib.request.install_opener(OPENER)


def log_post(entry):
    """Append a post record to the local JSONL log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(POST_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_post_log():
    """Read all entries from the local post log."""
    if not POST_LOG.exists():
        return []
    entries = []
    for line in POST_LOG.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def load_api_key():
    """Load PSP_API_KEY from the env file."""
    if not ENV_FILE.exists():
        print(json.dumps({"status": "error", "error": f"Missing {ENV_FILE}"}))
        sys.exit(1)
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "PSP_API_KEY":
            return val.strip().strip('"').strip("'")
    print(json.dumps({"status": "error", "error": "PSP_API_KEY not found in env file"}))
    sys.exit(1)


def api_post(endpoint, api_key, fields=None, files=None):
    """
    POST to Groups.io API. Uses form-urlencoded for fields-only requests,
    multipart/form-data when files are included.
    """
    url = f"{API_BASE}/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}

    if files:
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        body = b""
        for k, v in (fields or {}).items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += f"{v}\r\n".encode()
        for field_name, file_path in files:
            mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            fname = os.path.basename(file_path)
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{field_name}"; filename="{fname}"\r\n'.encode()
            body += f"Content-Type: {mime}\r\n\r\n".encode()
            body += Path(file_path).read_bytes()
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in (fields or {}).items()).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {"object": "error", "type": "http_error", "extra": f"{e.code}: {error_body}"}


def api_get(endpoint, api_key, params=None):
    """GET from Groups.io API."""
    url = f"{API_BASE}/{endpoint}"
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {"object": "error", "type": "http_error", "extra": f"{e.code}: {error_body}"}


def check_status():
    """Check local post log, live topics, and pending drafts."""
    api_key = load_api_key()
    result = {"status": "ok", "submitted": [], "live": [], "pending_drafts": []}

    # Local log — everything we've submitted
    log_entries = read_post_log()
    submitted_subjects = set()
    for entry in log_entries:
        result["submitted"].append({
            "timestamp": entry.get("timestamp"),
            "subject": entry.get("subject"),
            "status_at_submit": entry.get("status"),
            "draft_id": entry.get("draft_id"),
        })
        submitted_subjects.add(entry.get("subject"))

    # Check which submitted posts are now live
    # Collect topic_ids from log entries that have them
    known_topic_ids = {e.get("topic_id") for e in log_entries if e.get("topic_id")}

    if submitted_subjects or known_topic_ids:
        topics = api_get("gettopics", api_key, params={
            "group_id": CLASSIFIEDS_GROUP_ID,
            "limit": 100,
            "sort_field": "created",
            "sort_dir": "desc",
        })
        live_topic_ids = set()
        live_subjects = set()
        if topics.get("data"):
            for t in topics["data"]:
                tid = t.get("id")
                subj = t.get("subject", "")
                # Match by topic_id or by subject (normalize whitespace/dashes)
                matched = tid in known_topic_ids or subj in submitted_subjects
                if not matched:
                    # Fuzzy: check if any submitted subject starts with the topic subject or vice versa
                    for ss in submitted_subjects:
                        if subj.startswith(ss.rstrip(" —\u2014")) or ss.startswith(subj.rstrip(" —\u2014")):
                            matched = True
                            break
                if matched:
                    result["live"].append({
                        "id": tid,
                        "subject": subj,
                        "created": t.get("created"),
                    })
                    live_topic_ids.add(tid)
                    live_subjects.add(subj)

    # Pending drafts
    drafts = api_get("getdrafts", api_key, params={"group_id": CLASSIFIEDS_GROUP_ID})
    if drafts.get("data"):
        for d in drafts["data"]:
            result["pending_drafts"].append({
                "id": d.get("id"),
                "subject": d.get("subject") or "(no subject)",
                "created": d.get("created"),
                "type": d.get("draft_type"),
            })

    # Build summary
    parts = []
    if result["submitted"]:
        live_ids = {p["id"] for p in result["live"]}
        pending_mod = [s for s in result["submitted"]
                       if s.get("draft_id") and s["draft_id"] not in live_ids
                       and s.get("subject") not in {p["subject"] for p in result["live"]}]
        if result["live"]:
            parts.append(f"{len(result['live'])} post(s) live")
        if pending_mod:
            parts.append(f"{len(pending_mod)} post(s) awaiting moderator approval")
    if result["pending_drafts"]:
        parts.append(f"{len(result['pending_drafts'])} unsent draft(s)")
    result["summary"] = ", ".join(parts) if parts else "No posts submitted yet."

    print(json.dumps(result, indent=2))


def post_listing(subject, body_html, image_path=None, dry_run=False):
    """Create draft, set content, optionally attach image, and post."""
    api_key = load_api_key()

    if dry_run:
        result = {
            "status": "dry_run",
            "subject": subject,
            "body": body_html,
            "image": str(image_path) if image_path else None,
        }
        print(json.dumps(result, indent=2))
        return

    # Step 1: Create draft
    resp = api_post("newdraft", api_key, fields={
        "group_id": CLASSIFIEDS_GROUP_ID,
        "draft_type": "draft_type_post",
    })
    if resp.get("object") == "error":
        print(json.dumps({"status": "error", "step": "newdraft", "error": resp}))
        sys.exit(1)
    draft_id = resp["id"]

    # Step 2: Update draft with subject and body
    resp = api_post("updatedraft", api_key, fields={
        "draft_id": draft_id,
        "subject": subject,
        "body": body_html,
    })
    if resp.get("object") == "error":
        print(json.dumps({"status": "error", "step": "updatedraft", "error": resp}))
        sys.exit(1)

    # Step 3: Attach image if provided
    if image_path:
        img = Path(image_path)
        if not img.exists():
            print(json.dumps({"status": "error", "error": f"Image not found: {image_path}"}))
            sys.exit(1)
        resp = api_post("uploadattachments", api_key,
                        fields={"draft_id": draft_id},
                        files=[("fileupload", str(img))])
        if resp.get("object") == "error":
            print(json.dumps({"status": "error", "step": "uploadattachments", "error": resp}))
            sys.exit(1)

    # Step 4: Post the draft
    resp = api_post("postdraft", api_key, fields={"draft_id": draft_id})
    if resp.get("object") == "error":
        print(json.dumps({"status": "error", "step": "postdraft", "error": resp}))
        sys.exit(1)

    extra = resp.get("extra", "")
    status = "pending" if extra == "pending post" else "posted"
    result = {
        "status": status,
        "draft_id": draft_id,
        "subject": subject,
    }
    if extra:
        result["note"] = extra

    # Log locally
    log_post({
        "timestamp": datetime.now().isoformat(),
        "draft_id": draft_id,
        "subject": subject,
        "body": body_html,
        "image": str(image_path) if image_path else None,
        "status": status,
    })

    print(json.dumps(result))
    sys.exit(2 if status == "pending" else 0)


def main():
    parser = argparse.ArgumentParser(description="Post a listing to PSP Classifieds")
    parser.add_argument("--subject", default=None, help="Post subject line")
    parser.add_argument("--body", default=None, help="Post body (HTML)")
    parser.add_argument("--image", default=None, help="Path to image to attach")
    parser.add_argument("--dry-run", action="store_true", help="Validate without posting")
    parser.add_argument("--status", action="store_true", help="Check recent posts and pending drafts")
    args = parser.parse_args()

    if args.status:
        check_status()
        return

    if not args.subject or not args.body:
        parser.error("--subject and --body are required when posting")

    post_listing(args.subject, args.body, args.image, args.dry_run)


if __name__ == "__main__":
    main()
