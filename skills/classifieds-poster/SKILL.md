---
name: classifieds-poster
description: Generate and post classified ad listings on Park Slope Parents (PSP) Classifieds. Triggers on listing/giveaway requests (photo + description via Telegram) AND status checks ("classifieds status", "did my post go through", "check my listings").
---

# Classifieds Poster

Generate ready-to-post classified ad listings for the Park Slope Parents (PSP) Classifieds group.

## Workflow — New Listing

1. User sends a photo of the item via Telegram + brief description or instructions
2. Analyze the photo to identify the item (brand, model, condition, color, notable features)
3. Read `references/psp-format.md` for the posting format
4. Generate a subject line and body following PSP conventions
5. Present the complete listing to the user for review
6. **Wait for explicit user approval before posting** — never post automatically
7. On approval, save the Telegram photo to a temp file and post via `scripts/post.py` (see Posting section below)

## Workflow — Status Check

When the user asks about the status of their posts ("did my post go through?", "classifieds status", "check my listings"):

1. Run `python3 scripts/post.py --status`
2. Summarize the results in a friendly message:
   - List any recent posts that are live, with subject and date
   - List any pending drafts that haven't been posted yet
   - If a post was recently submitted, note that it may be pending moderator approval

## Input Requirements

- **Photo**: At least one photo of the item (required)
- **Description**: Any details the user provides — brand, age, condition, defects (optional but helpful)
- If the user provides minimal info, infer what you can from the photo and ask only if something critical is ambiguous (e.g., "Is this a specific brand/model?")

## Output Format

Present the listing as two clearly separated sections:

**Subject:**
`FF: Item Name — Clinton Hill`

**Body:**
The complete post body.

## Guidelines

- Default to "FF:" prefix unless user specifies selling (use "FS:" for sale)
- Default neighborhood: Clinton Hill (unless user specifies otherwise)
- Keep descriptions honest — note visible wear or damage from the photo
- Be concise: 3-5 sentences max for the body
- Always include "Photos attached" (user will attach the same photo when posting)
- Always include pickup coordination line: "If interested, please email me and we can coordinate pickup from my home."
- If multiple items in one photo, create one combined listing or ask if separate posts are preferred

## Posting

After the user approves the listing, post it using `scripts/post.py`:

```bash
python3 scripts/post.py \
  --subject "FF: Item Name — Clinton Hill" \
  --body "<p>Hi all,</p><p>Body in HTML...</p>" \
  --image /path/to/photo.jpg
```

- **Script:** `scripts/post.py` (Python 3, no external dependencies)
- **Credentials:** Reads `PSP_API_KEY` from `~/.openclaw/workspace/.env.classifieds`
- **API:** Groups.io REST API (newdraft → updatedraft → uploadattachments → postdraft)
- **Image:** Optional `--image` flag attaches a photo to the post
- **Dry run:** Use `--dry-run` to validate without posting
- **IMPORTANT:** Always show the listing and get explicit user approval before running the script. Never post without confirmation.
- **Exit codes:** 0 = posted, 1 = error, 2 = pending moderator approval
- **Output:** JSON to stdout with status, draft_id, and any notes

## Status Check

```bash
python3 scripts/post.py --status
```

Returns JSON with:
- `submitted` — all posts made through this script (from local log at `data/posts.jsonl`)
- `live` — submitted posts that now appear in the group (approved by mods)
- `pending_drafts` — unsent drafts still in Groups.io
- `summary` — human-readable one-liner

Posts that appear in `submitted` but not `live` are awaiting moderator approval.

## Data

- **Post log:** `data/posts.jsonl` — append-only, one JSON line per submission (timestamp, draft_id, subject, body, image path, status at time of submission)
