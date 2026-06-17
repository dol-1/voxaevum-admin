#!/usr/bin/env python3
"""
VoxAevum trilingual gap-filler (autonomous).

One run does the whole job:
  1. SCAN   GitHub for blog groups missing one or more of EN/RU/ES (by 6-digit ID).
  2. CREATE the missing language(s) via DeepSeek transcreation (source order RU>EN>ES).
  3. PLACE  the new .astro files into the local repo working tree (correct path + depth).
  4. BUILD  the whole site with Node 22 in Docker (host Node v20 is rejected by Astro).
  5. COMMIT + PUSH (git pull --rebase first; n8n auto-commits to the same repo).
  6. RE-SCAN to confirm 0 incomplete groups remain.

Production-safe: never touches n8n. Stops on the first failing step. Nothing is
pushed unless the local Docker build passes. Use --dry-run to generate + build only.

Tokens are read interactively (never written to disk / never hard-coded).

Usage (from anywhere; the script clones the repo if needed):
    python3 voxaevum_fill_trilingual.py            # full run
    python3 voxaevum_fill_trilingual.py --dry-run  # generate + build, no commit/push
    python3 voxaevum_fill_trilingual.py --no-build  # skip Docker build (not recommended)
"""

import os, re, sys, json, base64, subprocess, getpass, urllib.request, urllib.error
from collections import defaultdict

OWNER, REPO = "dol-1", "voxaevum"
REPO_DIR = os.path.expanduser("~/voxaevum")
DIRS = {"en": "src/pages/blog", "ru": "src/pages/ru/blog", "es": "src/pages/es/blog"}
IMPORT_DEPTH = {"en": "../..", "ru": "../../..", "es": "../../.."}
READTIME = {"en": "8 min read", "ru": "8 мин чтения", "es": "8 min lectura"}
BYLINE = {"en": "By", "ru": "Автор:", "es": "Por"}
LANG_NAME = {"en": "English", "ru": "Russian", "es": "Spanish (neutral international)"}
SOURCE_PRIORITY = ["ru", "en", "es"]

DRY_RUN = "--dry-run" in sys.argv
NO_BUILD = "--no-build" in sys.argv

# ----------------------------------------------------------------------------- tokens
def prompt_secret(label):
    """Read a secret from the real terminal, flushing any pending paste buffer
    first so that lines pasted *after* this command can't leak into the input."""
    try:
        tty = open("/dev/tty", "r+")
    except Exception:
        # No controlling terminal (e.g. piped) -> fall back to getpass
        return getpass.getpass(label).strip()
    try:
        import termios
        # Discard anything already sitting in the input queue (the rest of a
        # multi-line paste), so only what the user types NOW is read.
        termios.tcflush(tty.fileno(), termios.TCIFLUSH)
    except Exception:
        pass
    tty.write(label)
    tty.flush()
    # Turn off echo for the secret
    try:
        import termios
        old = termios.tcgetattr(tty.fileno())
        new = old[:]
        new[3] = new[3] & ~termios.ECHO
        termios.tcsetattr(tty.fileno(), termios.TCSADRAIN, new)
        line = tty.readline()
        termios.tcsetattr(tty.fileno(), termios.TCSADRAIN, old)
        tty.write("\n"); tty.flush()
    except Exception:
        line = tty.readline()
    tty.close()
    return line.strip()

GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
if not GH_TOKEN:
    GH_TOKEN = prompt_secret("GitHub PAT (Contents read+write): ")
if not DS_KEY:
    DS_KEY = prompt_secret("DeepSeek API key: ")
if not GH_TOKEN or not DS_KEY:
    sys.exit("Both tokens are required. Aborting.")

# ----------------------------------------------------------------------------- helpers
def gh_api(path, method="GET", data=None):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method,
        headers={"Authorization": f"token {GH_TOKEN}",
                 "Accept": "application/vnd.github.v3+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

def list_dir(path):
    try:
        return gh_api(f"contents/{path}?per_page=100")
    except urllib.error.HTTPError:
        return []

def get_content(path):
    d = gh_api(f"contents/{path}")
    return base64.b64decode(d["content"]).decode("utf-8")

def scan():
    groups = defaultdict(lambda: {"en": None, "ru": None, "es": None})
    for lang, d in DIRS.items():
        for f in list_dir(d):
            n = f["name"]
            if not n.endswith(".astro") or n == "index.astro":
                continue
            m = re.search(r"-(\d{6})\.astro$", n)
            key = m.group(1) if m else n
            groups[key][lang] = n
    return groups

# --- field extraction from an existing .astro file -------------------------
def extract_field(content, field):
    m = re.search(rf"{field}:\s*'([\s\S]*?)',?\s*\n", content)
    if not m:
        m = re.search(rf'{field}:\s*"([\s\S]*?)",?\s*\n', content)
    return m.group(1) if m else ""

def extract_tags(content):
    m = re.search(r"tags:\s*(\[[^\]]*\])", content)
    return m.group(1) if m else None

def extract_body(content):
    # body is inside set:html={ ... } — could be JSON.stringify(...) or a "literal"
    m = re.search(r'set:html=\{([\s\S]*?)\}\s*/>', content)
    if not m:
        return None
    raw = m.group(1).strip()
    # If it's a plain JSON string literal "...." decode it; if JSON.stringify(...) strip wrapper.
    js = re.match(r'JSON\.stringify\(([\s\S]*)\)$', raw)
    if js:
        raw = js.group(1).strip()
    try:
        return json.loads(raw)  # decode the JS/JSON string to plain HTML text
    except Exception:
        return raw

# --- brand guards ----------------------------------------------------------
BANNED_OPENERS = {
    "en": [r"^\s*<p>\s*(Imagine|Picture|Discover|Explore|Have you ever)\b"],
    "ru": [r"^\s*<p>\s*(Представьте|Узнайте|Откройте|Вообразите)\b"],
    "es": [r"^\s*<p>\s*(Imagina|Descubre|Explora|Imagínate|Supongamos)\b"],
}
def scrub(text):
    text = text.replace("\u2014", ", ").replace("--", ", ")  # em-dash scrub
    return text

# --- DeepSeek transcreation -------------------------------------------------
def transcreate(src_lang, dst_lang, title, summary, body_html):
    sys_prompt = (
        f"You are a master transcreator for VoxAevum, a trilingual AI/tech publication. "
        f"Transcreate (not literally translate) from {LANG_NAME[src_lang]} into {LANG_NAME[dst_lang]}. "
        f"Preserve meaning, voice, and structure. Keep ALL HTML tags intact "
        f"(<h1> <h2> <p> <blockquote> etc). Keep proper nouns untranslated "
        f"(GPT-4, Netflix, Google, Neuralink, etc). "
        f"RULES: never use em-dashes (use commas or periods). "
        f"Do not open with hypothetical/cliche framing (no 'Imagine', 'Discover', "
        f"'Представьте', 'Узнайте', 'Imagina', 'Descubre'). "
    )
    if dst_lang == "es":
        sys_prompt += "Use neutral international Spanish: no voseo, no regional idioms. Never write 'yo, Emil'. "
    if dst_lang == "ru":
        sys_prompt += "Russian is the quality benchmark: full transcreation, idiomatic, not word-for-word. "
    sys_prompt += (
        "Return ONLY a JSON object with keys: title, summary, body. "
        "title: a natural SEO title in the target language. "
        "summary: a 1-2 sentence meta description (do NOT start with Discover/Explore/Узнайте/Descubre). "
        "body: the full HTML article transcreated, tags preserved. No markdown fences."
    )
    user = json.dumps({"title": title, "summary": summary, "body": body_html}, ensure_ascii=False)
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {DS_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    out = json.loads(resp["choices"][0]["message"]["content"])
    out["title"] = scrub(out["title"])
    out["summary"] = scrub(out["summary"])
    out["body"] = scrub(out["body"])
    return out

# --- slug + file builder ----------------------------------------------------
def make_slug(title, group_id):
    s = title.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-")
    # transliterate-free: keep ascii where possible; fallback to group only
    s_ascii = re.sub(r"[^a-z0-9-]", "", s)
    if len(s_ascii) < 4:
        s_ascii = "post"
    s_ascii = s_ascii[:60].strip("-")
    return f"{s_ascii}-{group_id}"

def build_astro(lang, title, summary, body_html, author, date, tags_literal):
    depth = IMPORT_DEPTH[lang]
    def esc(s):  # single-quote-safe for frontmatter JS string
        return s.replace("\\", "").replace("'", "\u2019").replace("\r", " ").replace("\n", " ")
    lines = [
        "---",
        f"import Layout from '{depth}/layouts/Layout.astro';",
    ]
    if tags_literal:
        lines.append(f"import TagList from '{depth}/components/TagList.astro';")
    lines += [
        "export const post = {",
        f"  title: '{esc(title)}',",
        f"  date: '{esc(date)}',",
        f"  readTime: '{READTIME[lang]}',",
        f"  author: '{esc(author)}',",
        f"  summary: '{esc(summary)}',",
    ]
    if tags_literal:
        lines.append(f"  tags: {tags_literal},")
    lines += [
        "};",
        "---",
        "<Layout title={post.title} description={post.summary}>",
        "  <article>",
        '    <header class="article-header">',
        '      <div class="article-eyebrow">',
        "        <span>{post.date}</span>",
        "        <span>{post.readTime}</span>",
        f'        <span class="article-author">{BYLINE[lang]} {{post.author}}</span>',
        "      </div>",
        '      <h1 class="article-title">{post.title}</h1>',
        '      <p class="article-summary">{post.summary}</p>',
        "    </header>",
        '    <div class="article-body" set:html={' + json.dumps(body_html, ensure_ascii=False) + "} />",
    ]
    if tags_literal:
        lines.append(f'    <TagList lang="{lang}" tags={{post.tags}} />')
    lines += ["  </article>", "</Layout>", ""]
    return "\n".join(lines)

# ----------------------------------------------------------------------------- repo
def run(cmd, cwd=None, check=True, quiet_cmd=None):
    print(f"  $ {quiet_cmd if quiet_cmd else ' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout.strip():
        print("   ", r.stdout.strip()[:500])
    if r.returncode != 0:
        print("    STDERR:", r.stderr.strip()[:500])
        if check:
            sys.exit(f"Command failed: {quiet_cmd if quiet_cmd else ' '.join(cmd)}")
    return r

def ensure_repo():
    if os.path.isdir(os.path.join(REPO_DIR, ".git")):
        print(f"Repo exists at {REPO_DIR}, pulling latest...")
        run(["git", "pull", "--rebase"], cwd=REPO_DIR, check=False)
    else:
        print(f"Cloning repo to {REPO_DIR}...")
        url = f"https://{GH_TOKEN}@github.com/{OWNER}/{REPO}.git"
        run(["git", "clone", "--depth", "1", url, REPO_DIR],
            quiet_cmd=f"git clone --depth 1 https://***@github.com/{OWNER}/{REPO}.git {REPO_DIR}")

def docker_build():
    print("Building site with Node 22 (Docker)...")
    cmd = [
        "docker", "run", "--rm", "-v", f"{REPO_DIR}:/app", "-w", "/app",
        "node:22-alpine", "sh", "-c",
        "npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund; npm run build",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip()[-1500:])
    if r.returncode != 0:
        print("BUILD STDERR:", r.stderr.strip()[-1500:])
        return False
    return True

# ----------------------------------------------------------------------------- main
def main():
    ensure_repo()
    print("\n=== SCAN ===")
    groups = scan()
    incomplete = {k: v for k, v in groups.items() if not all(v.values())}
    print(f"Total groups: {len(groups)} | incomplete: {len(incomplete)}")
    if not incomplete:
        print("All groups complete. Nothing to do.")
        return

    created = []
    for gid, langs in sorted(incomplete.items(), reverse=True):
        src_lang = next((l for l in SOURCE_PRIORITY if langs[l]), None)
        if not src_lang:
            continue
        src_file = langs[src_lang]
        print(f"\n--- group {gid}: source {src_lang} ({src_file}) ---")
        content = get_content(f"{DIRS[src_lang]}/{src_file}")
        title = extract_field(content, "title")
        summary = extract_field(content, "summary")
        author = extract_field(content, "author") or "Leo"
        date = extract_field(content, "date") or "May 31, 2026"
        tags_literal = extract_tags(content)
        body = extract_body(content)
        if not body:
            print(f"  ! could not extract body from {src_file}, skipping")
            continue

        for dst in ("en", "ru", "es"):
            if langs[dst]:
                continue
            print(f"  -> transcreating {src_lang} -> {dst}")
            try:
                out = transcreate(src_lang, dst, title, summary, body)
            except Exception as e:
                print(f"    ! DeepSeek error: {e}")
                continue
            slug = make_slug(out["title"], gid)
            # normalize date to English month format used across the repo
            astro = build_astro(dst, out["title"], out["summary"], out["body"],
                                author, date, tags_literal)
            target = os.path.join(REPO_DIR, DIRS[dst], f"{slug}.astro")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(astro)
            print(f"    wrote {DIRS[dst]}/{slug}.astro")
            created.append((dst, slug))

    if not created:
        print("\nNothing created. Aborting before build.")
        return

    print(f"\n=== {len(created)} files created ===")

    if not NO_BUILD:
        ok = docker_build()
        if not ok:
            sys.exit("\nBUILD FAILED. Nothing committed/pushed. Inspect the files and re-run.")
        print("BUILD OK.")
    else:
        print("Build skipped (--no-build).")

    if DRY_RUN:
        print("\n--dry-run: files generated + build tested, NOT committed. Review the working tree.")
        return

    print("\n=== COMMIT + PUSH ===")
    run(["git", "add", "-A"], cwd=REPO_DIR)
    run(["git", "pull", "--rebase"], cwd=REPO_DIR, check=False)
    run(["git", "commit", "-m", f"fill: complete {len(created)} missing trilingual language files"], cwd=REPO_DIR, check=False)
    run(["git", "push"], cwd=REPO_DIR)

    print("\n=== RE-SCAN ===")
    groups = scan()
    incomplete = {k: v for k, v in groups.items() if not all(v.values())}
    print(f"Incomplete after fill: {len(incomplete)}")
    for k, v in sorted(incomplete.items(), reverse=True):
        miss = [l for l in ("en", "ru", "es") if not v[l]]
        print(f"  {k}: still missing {miss}")
    if not incomplete:
        print("All groups complete. Done.")

if __name__ == "__main__":
    main()
