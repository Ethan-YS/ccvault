#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ccvault — Claude Code Vault

Back up, browse, and export your Claude Code conversations — fully local.
- Zero dependencies (Python standard library only)
- No network, ever. Your data never leaves your machine.
- Reads Claude Code's .jsonl transcripts, turns them into readable Markdown +
  structured JSON, and serves a local web UI to browse / search / filter / export.

Usage:
    python3 ccvault.py                  # auto-detect ~/.claude/projects, open browser
    python3 ccvault.py --src PATH       # use a custom transcripts folder
    python3 ccvault.py --out PATH       # custom archive output folder
    python3 ccvault.py --port 8765
    python3 ccvault.py --copy-raw       # also copy original .jsonl into the archive
    python3 ccvault.py --update-only    # just (re)build the archive, no server

MIT License.
"""
import os, sys, json, re, io, zipfile, argparse, datetime, urllib.parse, shutil, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".ccvault")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_SRC = os.path.join(HOME, ".claude", "projects")
DEFAULT_OUT = os.path.join(CONFIG_DIR, "archive")

TOOL_RESULT_MAXLEN = 8000
TOOL_INPUT_MAXLEN = 4000

MODEL_NICE = {
    "opus-4-8": "Opus 4.8", "opus-4-7": "Opus 4.7", "opus-4-6": "Opus 4.6",
    "opus-4-1": "Opus 4.1", "sonnet-4-6": "Sonnet 4.6", "sonnet-4-5": "Sonnet 4.5",
    "haiku-4-5": "Haiku 4.5", "fable-5": "Fable 5",
}

# ---------------------------------------------------------------- config

def load_config():
    try:
        return json.load(open(CONFIG_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- helpers

def nice_model(m):
    if not m:
        return ""
    for k, v in MODEL_NICE.items():
        if k in m:
            return v
    return m


def sanitize(s, maxlen=80):
    if not s:
        return ""
    s = str(s).replace("\n", " ").replace("\r", " ").strip()
    s = re.sub(r'[/\\:\*\?"<>\|]+', '_', s)
    s = re.sub(r'[\x00-\x1f]+', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:maxlen].strip()


def fmt_ts(ts):
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def date_only(ts):
    if not ts:
        return "0000-00-00"
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def friendly_project(cwd, encoded):
    """Human-readable project name from the working directory. No user-specific
    paths are assumed — we just take the last path segment."""
    if cwd:
        parts = [p for p in cwd.replace("\\", "/").split("/") if p]
        if parts:
            return sanitize(parts[-1]) or encoded
    return encoded


def tool_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    parts.append(c.get("text", ""))
                elif t == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(c, ensure_ascii=False))
            else:
                parts.append(str(c))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


# ---------------------------------------------------------------- parsing

def parse_session(path):
    meta = {"sessionId": None, "aiTitle": None, "cwd": None, "model": None,
            "first_ts": None, "last_ts": None}
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "ai-title":
                if o.get("aiTitle"):
                    meta["aiTitle"] = o["aiTitle"]
                continue
            if t in ("user", "assistant"):
                ts = o.get("timestamp")
                if ts:
                    if not meta["first_ts"]:
                        meta["first_ts"] = ts
                    meta["last_ts"] = ts
                if not meta["sessionId"]:
                    meta["sessionId"] = o.get("sessionId")
                if not meta["cwd"]:
                    meta["cwd"] = o.get("cwd")
                if t == "assistant" and not meta["model"]:
                    meta["model"] = (o.get("message") or {}).get("model")
                rows.append((t, o))
    if not meta["sessionId"]:
        meta["sessionId"] = os.path.splitext(os.path.basename(path))[0]
    return meta, rows


def first_user_text(rows):
    for t, o in rows:
        if t != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str):
            if c.strip():
                return c.strip()
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    return b["text"].strip()
    return ""


def build_payload(meta, rows, encoded):
    """Structured, name-free representation. Roles only (user/assistant);
    the UI decides how to label them."""
    msgs = []
    for t, o in rows:
        m = o.get("message") or {}
        ts = fmt_ts(o.get("timestamp"))
        if t == "user":
            c = m.get("content")
            texts, results = [], []
            if isinstance(c, str):
                if c.strip():
                    texts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        if b.get("type") == "text" and (b.get("text") or "").strip():
                            texts.append(b["text"])
                        elif b.get("type") == "tool_result":
                            results.append(b)
            if texts:
                msgs.append({"role": "user", "time": ts, "text": "\n\n".join(texts)})
            for b in results:
                txt = tool_result_text(b)
                if len(txt) > TOOL_RESULT_MAXLEN:
                    txt = txt[:TOOL_RESULT_MAXLEN] + f"\n… (truncated, {len(txt)} chars total)"
                msgs.append({"role": "tool_result", "time": ts, "text": txt,
                             "is_error": bool(b.get("is_error"))})
        elif t == "assistant":
            c = m.get("content")
            if isinstance(c, str):
                c = [{"type": "text", "text": c}] if c.strip() else []
            if not isinstance(c, list):
                continue
            blocks = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    th = (b.get("thinking") or "").strip()
                    if th:
                        blocks.append({"type": "thinking", "text": th})
                elif bt == "text":
                    tx = (b.get("text") or "").strip()
                    if tx:
                        blocks.append({"type": "text", "text": tx})
                elif bt == "tool_use":
                    inp = b.get("input")
                    inp_s = json.dumps(inp, ensure_ascii=False, indent=2) if inp is not None else ""
                    if len(inp_s) > TOOL_INPUT_MAXLEN:
                        inp_s = inp_s[:TOOL_INPUT_MAXLEN] + "\n… (truncated)"
                    blocks.append({"type": "tool_use", "name": b.get("name", "?"), "input": inp_s})
            if blocks:
                msgs.append({"role": "assistant", "time": ts, "blocks": blocks})
    ftext = first_user_text(rows).replace("\n", " ").strip()
    return {
        "title": (ftext[:60] or meta.get("aiTitle") or "(untitled)"),
        "aiTitle": meta.get("aiTitle") or "",
        "project": friendly_project(meta.get("cwd"), encoded),
        "model": nice_model(meta.get("model")),
        "time": fmt_ts(meta.get("first_ts")),
        "cwd": meta.get("cwd") or "",
        "msgcount": sum(1 for x in msgs if x["role"] in ("user", "assistant")),
        "messages": msgs,
    }


def payload_to_md(p, clean, names):
    u, a = names.get("user", "User"), names.get("ai", "Claude")
    L = ['# ' + (p.get('title') or ''), '']
    if p.get('aiTitle'):
        L.append('> ' + p['aiTitle'])
    L.append('> ' + ' · '.join(x for x in [p.get('project', ''), p.get('model', ''), p.get('time', '')] if x))
    L += ['', '---', '']
    for m in p.get('messages', []):
        role = m.get('role')
        if role == 'user':
            L += ['### ' + u + (' · ' + m['time'] if m.get('time') else ''), '', m.get('text', ''), '']
        elif role == 'assistant':
            parts = []
            for b in m.get('blocks', []):
                t = b.get('type')
                if t == 'text':
                    parts.append(b.get('text', ''))
                elif not clean and t == 'thinking':
                    parts.append('<details><summary>thinking</summary>\n\n' + b.get('text', '') + '\n\n</details>')
                elif not clean and t == 'tool_use':
                    parts.append('<details><summary>tool: ' + b.get('name', '') + '</summary>\n\n```json\n' + b.get('input', '') + '\n```\n\n</details>')
            if parts:
                L += ['### ' + a + (' · ' + m['time'] if m.get('time') else ''), '', '\n\n'.join(parts), '']
        elif role == 'tool_result' and not clean:
            L += ['<details><summary>tool result</summary>\n\n```\n' + m.get('text', '') + '\n```\n\n</details>', '']
    return '\n'.join(L)


# ---------------------------------------------------------------- build archive

def scan_info(path):
    """Lightweight scan: message count + identity fields (does not keep rows)."""
    sid = None; n = 0; cwd = None; first_ts = None; first_user = None; aiTitle = None; model = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ty = o.get("type")
                if ty == "ai-title":
                    if o.get("aiTitle"):
                        aiTitle = o["aiTitle"]
                    continue
                if ty in ("user", "assistant"):
                    n += 1
                    if not sid:
                        sid = o.get("sessionId")
                    if not cwd:
                        cwd = o.get("cwd")
                    ts = o.get("timestamp")
                    if ts and not first_ts:
                        first_ts = ts
                    if ty == "assistant" and not model:
                        model = (o.get("message") or {}).get("model")
                    if ty == "user" and first_user is None:
                        c = (o.get("message") or {}).get("content")
                        if isinstance(c, str) and c.strip():
                            first_user = c.strip()
                        elif isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                                    first_user = b["text"].strip()
                                    break
    except Exception:
        return None
    return {"sid": sid, "n": n, "cwd": cwd, "first_ts": first_ts,
            "first_user": first_user, "aiTitle": aiTitle, "model": model}


def fingerprint(info):
    """A conversation's identity: same project + same first user message + same first
    timestamp ⇒ the same conversation (its resume snapshots all share these)."""
    base = (info.get("cwd") or "") + "\x00" + ((info.get("first_user") or "")[:200]) + "\x00" + (info.get("first_ts") or "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def build_archive(src, out, copy_raw=False, force=False, dedupe=True):
    os.makedirs(out, exist_ok=True)
    manifest_path = os.path.join(out, ".manifest.json")
    manifest = {}
    if os.path.exists(manifest_path) and not force:
        try:
            manifest = json.load(open(manifest_path, encoding="utf-8"))
        except Exception:
            manifest = {}
    stats = {"sessions": 0, "converted": 0, "skipped": 0, "failed": 0, "deduped": 0}
    if not os.path.isdir(src):
        return {"error": f"source folder not found: {src}", **stats}

    # pass 1 — lightweight scan of every .jsonl
    items = []
    for encoded in sorted(os.listdir(src)):
        pdir = os.path.join(src, encoded)
        if not os.path.isdir(pdir):
            continue
        try:
            jsonls = sorted(f for f in os.listdir(pdir) if f.endswith(".jsonl"))
        except Exception:
            continue
        for jf in jsonls:
            path = os.path.join(pdir, jf)
            try:
                st = os.stat(path)
            except Exception:
                continue
            info = scan_info(path)
            if not info or info["n"] == 0:
                continue
            items.append({"path": path, "stem": os.path.splitext(jf)[0], "st": st,
                          "info": info, "proj": friendly_project(info.get("cwd"), encoded)})
    stats["sessions"] = len(items)

    # pass 2 — dedupe: keep the most complete snapshot of each conversation
    if dedupe:
        groups = {}
        for it in items:
            groups.setdefault(fingerprint(it["info"]), []).append(it)
        chosen = []
        for fpk, its in groups.items():
            its.sort(key=lambda x: (x["info"]["n"], x["st"].st_mtime), reverse=True)
            best = its[0]
            best["key"] = fpk
            chosen.append(best)
            stats["deduped"] += len(its) - 1
    else:
        chosen = []
        for it in items:
            it["key"] = it["stem"]
            chosen.append(it)

    # pass 3 — generate the chosen ones
    seen = set()
    for it in chosen:
        key = it["key"]; seen.add(key); st = it["st"]
        prev = manifest.get(key)
        if (prev and not force and prev.get("stem") == it["stem"]
                and prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size
                and prev.get("jsonfile") and os.path.exists(os.path.join(out, prev["jsonfile"]))):
            stats["skipped"] += 1
            continue
        try:
            meta, rows = parse_session(it["path"])
            payload = build_payload(meta, rows, "")
            md = payload_to_md(payload, False, {"user": "User", "ai": "Claude"})
        except Exception:
            stats["failed"] += 1
            continue
        ftext = first_user_text(rows)
        fbase = sanitize(ftext[:30]) or sanitize(meta.get("aiTitle") or "") or "untitled"
        d = date_only(meta.get("first_ts"))
        base = f"{d}_{fbase}_{key[:8]}"
        proj_dir = os.path.join(out, sanitize(it["proj"]))
        os.makedirs(proj_dir, exist_ok=True)
        md_path = os.path.join(proj_dir, base + ".md")
        json_path = os.path.join(proj_dir, base + ".json")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        if copy_raw:
            try:
                shutil.copy2(it["path"], os.path.join(proj_dir, base + ".jsonl"))
            except Exception:
                pass
        manifest[key] = {"stem": it["stem"], "mtime": st.st_mtime, "size": st.st_size,
                         "mdfile": os.path.relpath(md_path, out), "jsonfile": os.path.relpath(json_path, out),
                         "title": payload["title"], "proj": it["proj"], "date": d,
                         "msgcount": payload["msgcount"], "model": payload["model"]}
        stats["converted"] += 1

    # Append-only by design: we deliberately KEEP conversations that have disappeared
    # from the source. Once a chat is captured here, it stays — even if you delete it
    # in Claude Code. That is the whole point of a vault. The only way an existing entry
    # changes is when a newer / more-complete snapshot of the *same* conversation
    # overwrites it in place (same fingerprint → same filename).
    kept_gone = sum(1 for k in manifest if k not in seen)
    stats["kept"] = kept_gone

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return stats


# ---------------------------------------------------------------- web app

STATE = {"src": DEFAULT_SRC, "out": DEFAULT_OUT, "copy_raw": False, "dedupe": True}


def load_manifest():
    try:
        return json.load(open(os.path.join(STATE["out"], ".manifest.json"), encoding="utf-8"))
    except Exception:
        return {}


def list_payload():
    man = load_manifest()
    groups = {}
    for stem, v in man.items():
        groups.setdefault(v.get("proj", "?"), []).append({
            "stem": stem, "title": v.get("title", "(untitled)"),
            "date": v.get("date", "0000-00-00"), "msgcount": v.get("msgcount", 0)})
    out = []
    for proj, sess in groups.items():
        sess.sort(key=lambda x: x["date"], reverse=True)
        out.append({"project": proj, "count": len(sess), "sessions": sess})
    out.sort(key=lambda g: g["sessions"][0]["date"] if g["sessions"] else "", reverse=True)
    return {"projects": out, "total": sum(len(g["sessions"]) for g in out),
            "src": STATE["src"], "out": STATE["out"]}


def get_session(stem):
    man = load_manifest()
    rec = man.get(stem)
    if not rec or not rec.get("jsonfile"):
        return None
    jp = os.path.join(STATE["out"], rec["jsonfile"])
    if not os.path.exists(jp):
        return None
    try:
        return json.load(open(jp, encoding="utf-8"))
    except Exception:
        return None


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ccvault — Claude Code Vault</title>
<style>
:root{--bg:#f6f5f1;--panel:#fffdf9;--ink:#2b2b29;--muted:#8a8780;--line:#e7e3da;--accent:#3f7d6e;--accent-soft:#e8f1ee;--user:#eef3fb;--user-line:#d6e3f5;--tool:#f3f1ec}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font-family:-apple-system,"Segoe UI","PingFang SC","Helvetica Neue",sans-serif;color:var(--ink);background:var(--bg)}
#app{display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);flex:0 0 auto}
header .logo{font-weight:700;font-size:16px}
header .sub{color:var(--muted);font-size:12px}
header .spacer{flex:1}
button.act{border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer}
button.act:hover{filter:brightness(1.05)}
button.act:disabled{opacity:.6;cursor:default}
button.ghost{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer}
button.ghost:hover{background:var(--tool)}
#status{color:var(--muted);font-size:12px;min-width:50px}
.body{display:flex;flex:1;min-height:0}
.side{width:320px;flex:0 0 auto;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-width:180px;max-width:640px}
.resizer{width:6px;flex:0 0 auto;cursor:col-resize;background:transparent;position:relative}
.resizer:hover,.resizer.dragging{background:var(--accent-soft)}
.resizer::after{content:'';position:absolute;left:2px;top:0;bottom:0;width:1px;background:var(--line)}
.search{padding:10px}
.search input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;outline:none;background:#fff}
.search input:focus{border-color:var(--accent)}
.tools{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.tbtn{flex:1;border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:7px;padding:6px 8px;font-size:12px;cursor:pointer;white-space:nowrap}
.tbtn:hover{background:var(--tool)}
.tbtn.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.fpanel{margin:0 10px 8px;border:1px solid var(--line);border-radius:8px;background:var(--panel);max-height:240px;overflow:auto;padding:4px}
.fp-head{display:flex;gap:12px;padding:5px 7px;font-size:12px;color:var(--muted)}
.fp-head a{color:var(--accent);cursor:pointer}
.fp-item{display:flex;align-items:center;gap:7px;padding:5px 7px;font-size:12.5px;cursor:pointer;border-radius:6px}
.fp-item:hover{background:var(--tool)}
.list{overflow:auto;padding:0 6px 16px}
.proj{margin:6px 4px 2px;font-size:11px;color:var(--muted);font-weight:600;padding:6px 8px 2px}
.folder{display:flex;align-items:center;gap:7px;padding:8px 9px;margin:2px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;color:var(--ink);user-select:none}
.folder:hover{background:var(--tool)}
.folder .arrow{font-size:9px;color:var(--muted);transition:transform .15s;display:inline-block;width:9px;flex:0 0 auto}
.folder.open .arrow{transform:rotate(90deg)}
.folder .ficon{flex:0 0 auto}
.folder .fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.folder .cnt{color:var(--muted);font-weight:400;font-size:11px;margin-left:auto;flex:0 0 auto;padding-left:6px}
.folder .fexp{opacity:0;flex:0 0 auto;border:none;background:transparent;cursor:pointer;font-size:12px;color:var(--muted);padding:0 4px;border-radius:5px;margin-left:4px}
.folder:hover .fexp{opacity:.6}
.folder .fexp:hover{opacity:1;background:var(--user-line)}
.fitems{margin:0 2px 6px 15px;border-left:1px solid var(--line);padding-left:5px}
.item{display:flex;align-items:flex-start;gap:4px;padding:8px 10px;margin:2px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.item:hover{background:var(--accent-soft)}
.item.active{background:var(--accent-soft);border-color:var(--accent)}
.item .icontent{flex:1;min-width:0}
.item .t{font-size:13px;line-height:1.35;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.item .d{font-size:11px;color:var(--muted);margin-top:3px}
.item .arc{opacity:0;flex:0 0 auto;border:none;background:transparent;cursor:pointer;font-size:13px;color:var(--muted);padding:2px 5px;border-radius:5px}
.item:hover .arc{opacity:.7}
.item .arc:hover{opacity:1;background:var(--user-line)}
.main{flex:1;min-width:0;overflow:auto;padding:22px 28px}
.empty{color:var(--muted);text-align:center;margin-top:16vh;font-size:14px;line-height:1.9}
.shead{margin:0 0 4px;font-size:20px;font-weight:700}
.smeta{color:var(--muted);font-size:12px;margin-bottom:2px}
.snote{color:var(--muted);font-size:12px;background:var(--tool);border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin:12px 0 14px}
.vtools{display:flex;gap:8px;margin:0 0 16px;flex-wrap:wrap}
.vbtn{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:7px;padding:6px 12px;font-size:12.5px;cursor:pointer}
.vbtn:hover{background:var(--tool)}
.vbtn.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.msg{margin:14px 0;display:flex;flex-direction:column}
.msg .who{font-size:12px;color:var(--muted);margin-bottom:4px}
.bubble{border-radius:12px;padding:11px 14px;max-width:820px;line-height:1.62;font-size:14.5px}
.msg.user{align-items:flex-end}
.msg.user .bubble{background:var(--user);border:1px solid var(--user-line)}
.msg.assistant .bubble{background:var(--accent-soft);border:1px solid #d4e6e0}
.txt{white-space:pre-wrap;word-break:break-word}
.txt+.txt{margin-top:8px}
strong{font-weight:700}
code{background:#00000010;padding:1px 5px;border-radius:5px;font-family:"SF Mono",Menlo,monospace;font-size:.9em}
pre.code{background:#1f2723;color:#e6efe9;padding:10px 12px;border-radius:8px;overflow:auto;font-family:"SF Mono",Menlo,monospace;font-size:12.5px;line-height:1.5}
details{margin:8px 0;border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}
details>summary{cursor:pointer;padding:7px 11px;font-size:12.5px;color:var(--muted);list-style:none;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:var(--tool)}
details[open]>summary{border-bottom:1px solid var(--line)}
details .inner{padding:10px 12px}
details .inner pre{margin:0}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#2b2b29;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;max-width:80vw}
.toast.show{opacity:.95}
.modal{position:fixed;inset:0;background:#0006;display:none;align-items:center;justify-content:center;z-index:10}
.modal.show{display:flex}
.modal .box{background:var(--panel);border-radius:12px;padding:20px;width:min(560px,90vw);box-shadow:0 10px 40px #0003}
.modal h3{margin:0 0 4px;font-size:15px}
.modal p{color:var(--muted);font-size:12.5px;margin:6px 0}
.modal input{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;font-size:13px;margin-top:8px}
.modal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
</style>
</head>
<body>
<div id="app">
  <header>
    <span class="logo">📦 ccvault</span>
    <span class="sub" id="total"></span>
    <span class="spacer"></span>
    <span id="status"></span>
    <button class="ghost" id="btnLang">中</button>
    <button class="ghost" id="btnSettings" data-i18n="source">⚙︎ Source</button>
    <button class="act" id="btnUpdate" data-i18n="update">🔄 Update</button>
    <button class="ghost" id="btnQuit" data-i18n="quit">⏻ Quit</button>
  </header>
  <div class="body">
    <div class="side" id="side">
      <div class="search">
        <input id="q" data-i18n-ph="search" placeholder="Search title / project…" autocomplete="off">
        <div class="tools">
          <button id="btnFilter" class="tbtn" data-i18n="filter">🗂 Filter</button>
          <button id="btnArchive" class="tbtn"><span data-i18n="archived">📥 Archived</span> <span id="arcCount">0</span></button>
          <button id="btnExport" class="tbtn" data-i18n="exportShown">⬇ Export shown</button>
        </div>
      </div>
      <div class="fpanel" id="filterPanel" hidden></div>
      <div class="list" id="list"></div>
    </div>
    <div class="resizer" id="resizer"></div>
    <div class="main" id="main">
      <div class="empty" data-i18n-html="pickHint">← pick a conversation on the left<br>Everything is read from your local archive. No network.</div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="settings"><div class="box">
  <h3 data-i18n="setTitle">Conversations source folder</h3>
  <p data-i18n="setDesc">Where your Claude Code transcripts (.jsonl) live. Auto-detected by default.</p>
  <input id="srcInput" placeholder="~/.claude/projects">
  <p id="srcCur" class="sub"></p>
  <div class="row"><button class="ghost" data-i18n="cancel" onclick="closeSettings()">Cancel</button><button class="act" data-i18n="saveRebuild" onclick="saveSrc()">Save & rebuild</button></div>
</div></div>
<script>
const I18N={
 en:{source:'⚙︎ Source',update:'🔄 Update',quit:'⏻ Quit',search:'Search title / project…',filter:'🗂 Filter',archived:'📥 Archived',exportShown:'⬇ Export shown',all:'all',none:'none',pickHint:'← pick a conversation on the left<br>Everything is read from your local archive. No network.',noArchive:'No archive yet.<br>Click <b>🔄 Update</b> to build it from your Claude Code folder.',nothingShown:'Nothing to show (filtered or archived).',noArchived:'No archived chats yet.',loading:'loading…',chats:'chats',projects:'projects',msgs:'msgs',thinkingNote:'thinking is not stored by Claude Code, so it is not shown',chatOnly:'💬 Chat only',showAll:'💬 Show all',exportMd:'⬇ Export Markdown',you:'You',thinking:'💭 thinking',toolResult:'📄 tool result',setTitle:'Conversations source folder',setDesc:'Where your Claude Code transcripts (.jsonl) live. Auto-detected by default.',cancel:'Cancel',saveRebuild:'Save & rebuild',restore:'restore',archiveHide:'archive (hide)',exportProj:'export this project',nothingExport:'nothing to export',packing:'packing',exported:'exported',exportFail:'export failed',quitConfirm:'Quit ccvault? (you can close this tab afterwards)',stopped:'📦 ccvault stopped. You can close this tab.',updating:'updating…',scanning:'scanning Claude Code…'},
 zh:{source:'⚙︎ 数据源',update:'🔄 更新',quit:'⏻ 退出',search:'搜索标题 / 项目…',filter:'🗂 筛选',archived:'📥 归档',exportShown:'⬇ 导出可见',all:'全选',none:'全不选',pickHint:'← 从左边选一场对话<br>这里读的是你本地的存档，不联网',noArchive:'还没有存档。<br>点 <b>🔄 更新</b> 从你的 Claude Code 文件夹生成。',nothingShown:'没有可显示的（被筛选或归档了）。',noArchived:'还没有归档的对话。',loading:'读取中…',chats:'对话',projects:'项目',msgs:'条',thinkingNote:'思考过程 Claude Code 未保存，故不显示',chatOnly:'💬 只看对话',showAll:'💬 显示全部',exportMd:'⬇ 导出 Markdown',you:'你',thinking:'💭 思考',toolResult:'📄 工具结果',setTitle:'对话源文件夹',setDesc:'你的 Claude Code 对话记录（.jsonl）所在位置，默认自动检测。',cancel:'取消',saveRebuild:'保存并重建',restore:'恢复',archiveHide:'归档（隐藏）',exportProj:'导出整个项目',nothingExport:'没有可导出的',packing:'正在打包',exported:'已导出',exportFail:'导出失败',quitConfirm:'退出 ccvault？（之后可以关闭此标签页）',stopped:'📦 ccvault 已退出，可以关闭此标签页。',updating:'更新中…',scanning:'扫描 Claude Code…'}
};
let lang=localStorage.getItem('lang')||((navigator.language||'').toLowerCase().startsWith('zh')?'zh':'en');
function t(k){let d=(I18N[lang]||I18N.en); return d[k]!==undefined?d[k]:(I18N.en[k]!==undefined?I18N.en[k]:k);}
function applyLang(){
  document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=t(e.getAttribute('data-i18n')));
  document.querySelectorAll('[data-i18n-ph]').forEach(e=>e.placeholder=t(e.getAttribute('data-i18n-ph')));
  document.querySelectorAll('[data-i18n-html]').forEach(e=>e.innerHTML=t(e.getAttribute('data-i18n-html')));
  let lb=document.getElementById('btnLang'); if(lb) lb.textContent=(lang==='zh'?'EN':'中');
  document.documentElement.lang=lang;
}
function toggleLang(){ lang=(lang==='zh'?'en':'zh'); localStorage.setItem('lang',lang); applyLang(); refreshTexts(); }
function refreshTexts(){
  let tot=document.getElementById('total'); if(tot) tot.textContent=(LIST.total||0)+' '+t('chats')+' · '+(LIST.projects?LIST.projects.length:0)+' '+t('projects');
  renderList(document.getElementById('q').value.trim());
  if(curSession) renderSession(curSession);
}
let LIST={projects:[],total:0}, curStem=null, curSession=null, expanded=new Set();
let hiddenProjects=new Set(JSON.parse(localStorage.getItem('hiddenProjects')||'[]'));
let archived=new Set(JSON.parse(localStorage.getItem('archived')||'[]'));
let archiveView=false, cleanView=localStorage.getItem('cleanView')==='1';
function saveHidden(){localStorage.setItem('hiddenProjects',JSON.stringify([...hiddenProjects]));}
function saveArchived(){localStorage.setItem('archived',JSON.stringify([...archived]));}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function mdLite(t){
  if(!t) return '';
  let parts=String(t).split('```'), out='';
  for(let i=0;i<parts.length;i++){
    if(i%2===1){ out+='<pre class="code">'+esc(parts[i].replace(/^\w*\n/,''))+'</pre>'; }
    else{ let s=esc(parts[i]); s=s.replace(/`([^`]+)`/g,'<code>$1</code>'); s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>'); s=s.replace(/^#{1,6}\s?(.+)$/gm,'<strong>$1</strong>'); out+='<div class="txt">'+s+'</div>'; }
  }
  return out;
}
function toast(m){let e=document.getElementById('toast');e.textContent=m;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2800);}

async function loadList(){
  let r=await fetch('/api/list'); LIST=await r.json();
  document.getElementById('total').textContent=LIST.total+' '+t('chats')+' · '+LIST.projects.length+' '+t('projects');
  if(!expanded.size && LIST.projects.length) expanded.add(LIST.projects[0].project);
  renderList(document.getElementById('q').value.trim());
  if(!LIST.total) document.getElementById('main').innerHTML='<div class="empty">'+t('noArchive')+'</div>';
}
function itemEl(s,projLabel,isArc){
  let it=document.createElement('div'); it.className='item'+(s.stem===curStem?' active':'');
  let sub=isArc?(esc(projLabel)+' · '+s.date):(s.date+(s.msgcount?(' · '+s.msgcount):''));
  it.innerHTML='<div class="icontent"><div class="t">'+esc(s.title)+'</div><div class="d">'+sub+'</div></div><button class="arc" title="'+(isArc?t('restore'):t('archiveHide'))+'">'+(isArc?'↩':'📥')+'</button>';
  it.querySelector('.icontent').onclick=()=>loadSession(s.stem);
  it.querySelector('.arc').onclick=(e)=>{e.stopPropagation(); isArc?archived.delete(s.stem):archived.add(s.stem); saveArchived(); renderList(document.getElementById('q').value.trim());};
  return it;
}
function renderList(q){
  q=(q||'').toLowerCase(); let host=document.getElementById('list'); host.innerHTML='';
  document.getElementById('arcCount').textContent=archived.size;
  if(archiveView){
    let arr=[];
    for(const g of LIST.projects) for(const s of g.sessions) if(archived.has(s.stem)&&(!q||s.title.toLowerCase().includes(q)||g.project.toLowerCase().includes(q))) arr.push([s,g.project]);
    if(!arr.length){host.innerHTML='<div class="proj">'+t('noArchived')+'</div>'; return;}
    arr.sort((a,b)=>b[0].date.localeCompare(a[0].date));
    for(const [s,pj] of arr) host.appendChild(itemEl(s,pj,true)); return;
  }
  let any=false;
  for(const g of LIST.projects){
    if(hiddenProjects.has(g.project)) continue;
    let sess=g.sessions.filter(s=>!archived.has(s.stem)&&(!q||s.title.toLowerCase().includes(q)||g.project.toLowerCase().includes(q)));
    if(!sess.length) continue; any=true;
    let isOpen=!!q||expanded.has(g.project);
    let f=document.createElement('div'); f.className='folder'+(isOpen?' open':'');
    f.innerHTML='<span class="arrow">▸</span><span class="ficon">📁</span><span class="fname">'+esc(g.project)+'</span><span class="cnt">'+sess.length+'</span><span class="fexp" title="'+t('exportProj')+'">⬇</span>';
    f.onclick=()=>{ expanded.has(g.project)?expanded.delete(g.project):expanded.add(g.project); renderList(document.getElementById('q').value.trim()); };
    f.querySelector('.fexp').onclick=(e)=>{ e.stopPropagation(); exportProject(g.project); };
    host.appendChild(f);
    if(isOpen){ let box=document.createElement('div'); box.className='fitems'; for(const s of sess) box.appendChild(itemEl(s,g.project,false)); host.appendChild(box); }
  }
  if(!any) host.innerHTML='<div class="proj">'+t('nothingShown')+'</div>';
}
async function loadSession(stem){
  curStem=stem; renderList(document.getElementById('q').value.trim());
  let main=document.getElementById('main'); main.innerHTML='<div class="empty">'+t('loading')+'</div>';
  try{ let r=await fetch('/api/session?stem='+encodeURIComponent(stem)); let s=await r.json();
    if(s.error){main.innerHTML='<div class="empty">'+esc(s.error)+'</div>'; return;}
    curSession=s; renderSession(s); main.scrollTop=0;
  }catch(e){ main.innerHTML='<div class="empty">failed: '+esc(''+e)+'</div>'; }
}
function renderSession(s){
  let p=[]; p.push('<h1 class="shead">'+esc(s.title)+'</h1>');
  let meta=[s.project]; if(s.model)meta.push(s.model); if(s.time)meta.push(s.time); meta.push(s.msgcount+' '+t('msgs'));
  p.push('<div class="smeta">'+esc(meta.join(' · '))+'</div>');
  if(s.aiTitle) p.push('<div class="smeta">🏷️ '+esc(s.aiTitle)+'</div>');
  p.push('<div class="snote">📂 '+esc(s.cwd||'')+'　·　'+t('thinkingNote')+'</div>');
  p.push('<div class="vtools"><button class="vbtn'+(cleanView?' on':'')+'" onclick="toggleClean()">'+(cleanView?t('showAll'):t('chatOnly'))+'</button><button class="vbtn" onclick="exportCur()">'+t('exportMd')+'</button></div>');
  for(const m of s.messages){
    if(m.role==='user'){ p.push('<div class="msg user"><div class="who">'+esc(m.time)+'  ·  '+t('you')+'</div><div class="bubble">'+mdLite(m.text)+'</div></div>'); }
    else if(m.role==='assistant'){
      let inner='';
      for(const b of m.blocks){
        if(b.type==='text') inner+=mdLite(b.text);
        else if(!cleanView&&b.type==='thinking') inner+='<details><summary>'+t('thinking')+'</summary><div class="inner txt">'+esc(b.text)+'</div></details>';
        else if(!cleanView&&b.type==='tool_use') inner+='<details><summary>🔧 '+esc(b.name)+'</summary><div class="inner"><pre class="code">'+esc(b.input)+'</pre></div></details>';
      }
      if(inner) p.push('<div class="msg assistant"><div class="who">Claude · '+esc(m.time)+'</div><div class="bubble">'+inner+'</div></div>');
    }
    else if(m.role==='tool_result'){ if(!cleanView) p.push('<details><summary>'+t('toolResult')+(m.is_error?' ⚠️':'')+'</summary><div class="inner"><pre class="code">'+esc(m.text)+'</pre></div></details>'); }
  }
  document.getElementById('main').innerHTML=p.join('');
}
function toggleClean(){ cleanView=!cleanView; localStorage.setItem('cleanView',cleanView?'1':'0'); if(curSession) renderSession(curSession); }
function dl(blob,name){ let a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),2000); }
function exportCur(){
  if(!curSession) return; let s=curSession, L=['# '+s.title,''];
  if(s.aiTitle) L.push('> '+s.aiTitle); L.push('> '+[s.project,s.model,s.time].filter(Boolean).join(' · ')); L.push(''); L.push('---'); L.push('');
  for(const m of s.messages){
    if(m.role==='user'){ L.push('### You'+(m.time?(' · '+m.time):'')); L.push(''); L.push(m.text); L.push(''); }
    else if(m.role==='assistant'){ let parts=[];
      for(const b of m.blocks){ if(b.type==='text')parts.push(b.text); else if(!cleanView&&b.type==='thinking')parts.push('<details><summary>thinking</summary>\n\n'+b.text+'\n\n</details>'); else if(!cleanView&&b.type==='tool_use')parts.push('<details><summary>tool: '+b.name+'</summary>\n\n```json\n'+b.input+'\n```\n\n</details>'); }
      if(parts.length){ L.push('### Claude'+(m.time?(' · '+m.time):'')); L.push(''); L.push(parts.join('\n\n')); L.push(''); } }
    else if(m.role==='tool_result'&&!cleanView){ L.push('<details><summary>tool result</summary>\n\n```\n'+m.text+'\n```\n\n</details>'); L.push(''); }
  }
  let name=(s.title||'chat').replace(/[\/\\:\*\?"<>\|\n]/g,'_').slice(0,50)+(cleanView?'_chatonly':'')+'.md';
  dl(new Blob([L.join('\n')],{type:'text/markdown;charset=utf-8'}),name);
}
async function bulkExport(stems){
  if(!stems.length){toast(t('nothingExport'));return;}
  toast(t('packing')+' '+stems.length+'…');
  try{ let r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stems,clean:cleanView})}); let blob=await r.blob(); dl(blob,'ccvault-export'+(cleanView?'_chatonly':'')+'.zip'); toast('✅ '+t('exported')+' '+stems.length); }
  catch(e){ toast('⚠️ '+t('exportFail')+': '+e); }
}
function visibleStems(){ let q=document.getElementById('q').value.trim().toLowerCase(),st=[]; for(const g of LIST.projects){ if(hiddenProjects.has(g.project))continue; for(const s of g.sessions){ if(archived.has(s.stem))continue; if(q&&!(s.title.toLowerCase().includes(q)||g.project.toLowerCase().includes(q)))continue; st.push(s.stem); } } return st; }
function exportVisible(){ bulkExport(visibleStems()); }
function exportProject(proj){ let g=LIST.projects.find(x=>x.project===proj); if(!g)return; bulkExport(g.sessions.filter(s=>!archived.has(s.stem)).map(s=>s.stem)); }
function toggleArchiveView(){ archiveView=!archiveView; document.getElementById('btnArchive').classList.toggle('on',archiveView); document.getElementById('filterPanel').hidden=true; document.getElementById('btnFilter').classList.remove('on'); renderList(document.getElementById('q').value.trim()); }
function toggleFilter(){
  let pn=document.getElementById('filterPanel');
  if(!pn.hidden){pn.hidden=true; document.getElementById('btnFilter').classList.remove('on'); return;}
  let h='<div class="fp-head"><a onclick="filterAll(true)">all</a><a onclick="filterAll(false)">none</a></div>';
  for(const g of LIST.projects){ let c=!hiddenProjects.has(g.project); h+='<label class="fp-item"><input type="checkbox" '+(c?'checked':'')+' data-proj="'+esc(g.project)+'"><span>'+esc(g.project)+' ('+g.sessions.length+')</span></label>'; }
  pn.innerHTML=h;
  pn.querySelectorAll('input[data-proj]').forEach(cb=>{ cb.onchange=()=>{ let pj=cb.getAttribute('data-proj'); cb.checked?hiddenProjects.delete(pj):hiddenProjects.add(pj); saveHidden(); renderList(document.getElementById('q').value.trim()); }; });
  pn.hidden=false; document.getElementById('btnFilter').classList.add('on');
}
function filterAll(show){ if(show)hiddenProjects.clear(); else for(const g of LIST.projects)hiddenProjects.add(g.project); saveHidden(); document.querySelectorAll('#filterPanel input[data-proj]').forEach(cb=>{cb.checked=!hiddenProjects.has(cb.getAttribute('data-proj'));}); renderList(document.getElementById('q').value.trim()); }
async function doUpdate(){
  let b=document.getElementById('btnUpdate'), st=document.getElementById('status');
  b.disabled=true; b.textContent='⏳ '+t('updating'); st.textContent=t('scanning');
  try{ let r=await fetch('/api/update',{method:'POST'}); let j=await r.json(); toast(j.ok?('✅ '+(j.summary||'updated')):('⚠️ '+(j.error||'failed'))); await loadList(); }
  catch(e){ toast('⚠️ '+e); }
  b.disabled=false; b.textContent=t('update'); st.textContent='';
}
async function doQuit(){ if(!confirm(t('quitConfirm')))return; try{await fetch('/api/quit',{method:'POST'});}catch(e){} document.body.innerHTML='<div style="text-align:center;margin-top:32vh;color:#8a8780;font-family:sans-serif">'+t('stopped')+'</div>'; }
function openSettings(){ document.getElementById('srcInput').value=LIST.src||''; document.getElementById('srcCur').textContent='current: '+(LIST.src||'(auto)'); document.getElementById('settings').classList.add('show'); }
function closeSettings(){ document.getElementById('settings').classList.remove('show'); }
async function saveSrc(){
  let v=document.getElementById('srcInput').value.trim(); if(!v){closeSettings();return;}
  toast('rebuilding from new source…');
  try{ let r=await fetch('/api/setsrc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({src:v})}); let j=await r.json(); if(j.error){toast('⚠️ '+j.error);return;} closeSettings(); toast('✅ '+(j.summary||'done')); await loadList(); }
  catch(e){ toast('⚠️ '+e); }
}
document.getElementById('q').addEventListener('input',e=>renderList(e.target.value.trim()));
document.getElementById('btnUpdate').addEventListener('click',doUpdate);
document.getElementById('btnQuit').addEventListener('click',doQuit);
document.getElementById('btnFilter').addEventListener('click',toggleFilter);
document.getElementById('btnArchive').addEventListener('click',toggleArchiveView);
document.getElementById('btnExport').addEventListener('click',exportVisible);
document.getElementById('btnSettings').addEventListener('click',openSettings);
document.getElementById('btnLang').addEventListener('click',toggleLang);
applyLang();
(function(){ let r=document.getElementById('resizer'),side=document.getElementById('side'),drag=false; let sw=localStorage.getItem('sideW'); if(sw)side.style.width=sw+'px';
  r.addEventListener('mousedown',e=>{drag=true;r.classList.add('dragging');document.body.style.cursor='col-resize';document.body.style.userSelect='none';e.preventDefault();});
  document.addEventListener('mousemove',e=>{ if(!drag)return; side.style.width=Math.max(180,Math.min(640,e.clientX))+'px'; });
  document.addEventListener('mouseup',()=>{ if(!drag)return; drag=false; r.classList.remove('dragging'); document.body.style.cursor=''; document.body.style.userSelect=''; localStorage.setItem('sideW',parseInt(side.style.width)); });
  window.addEventListener('dblclick',e=>{ if(e.target===r){ side.style.width='320px'; localStorage.setItem('sideW',320); } });
})();
loadList();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif u.path == "/api/ping":
            self._json({"ok": True})
        elif u.path == "/api/list":
            self._json(list_payload())
        elif u.path == "/api/session":
            stem = (urllib.parse.parse_qs(u.query).get("stem") or [""])[0]
            s = get_session(stem)
            self._json(s if s is not None else {"error": "not found (try Update)"}, 200 if s else 404)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/update":
            stats = build_archive(STATE["src"], STATE["out"], STATE["copy_raw"], dedupe=STATE["dedupe"])
            if stats.get("error"):
                self._json({"ok": False, "error": stats["error"]})
            else:
                self._json({"ok": True, "summary": f"{stats['converted']} new, {stats['skipped']} unchanged, {stats['sessions']} total"})
        elif u.path == "/api/setsrc":
            try:
                src = os.path.expanduser((self._read_json().get("src") or "").strip())
                if not os.path.isdir(src):
                    self._json({"error": f"folder not found: {src}"}); return
                STATE["src"] = src
                cfg = load_config(); cfg["src"] = src; save_config(cfg)
                stats = build_archive(STATE["src"], STATE["out"], STATE["copy_raw"], force=True, dedupe=STATE["dedupe"])
                self._json({"ok": True, "summary": f"{stats.get('converted',0)} chats from new source"})
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/export":
            try:
                req = self._read_json(); stems = req.get("stems", []); clean = bool(req.get("clean"))
                man = load_manifest(); buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for stem in stems:
                        rec = man.get(stem)
                        if not rec or not rec.get("jsonfile"):
                            continue
                        jp = os.path.join(STATE["out"], rec["jsonfile"])
                        if not os.path.exists(jp):
                            continue
                        try:
                            payload = json.load(open(jp, encoding="utf-8"))
                        except Exception:
                            continue
                        md = payload_to_md(payload, clean, {"user": "You", "ai": "Claude"})
                        base = sanitize(rec.get("date", "") + "_" + (payload.get("title") or "chat"))[:60]
                        z.writestr(base + "_" + stem[:6] + ".md", md)
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="ccvault-export.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/quit":
            self._json({"ok": True})
            import threading
            threading.Timer(0.4, lambda: os._exit(0)).start()
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


def main():
    ap = argparse.ArgumentParser(description="ccvault — back up & browse Claude Code conversations")
    ap.add_argument("--src", default=None, help="Claude Code transcripts folder (default: auto-detect ~/.claude/projects)")
    ap.add_argument("--out", default=None, help="archive output folder (default: ~/.ccvault/archive)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--copy-raw", action="store_true", help="also copy original .jsonl into the archive")
    ap.add_argument("--update-only", action="store_true", help="rebuild the archive and exit (no server)")
    ap.add_argument("--no-dedupe", dest="dedupe", action="store_false", help="keep every snapshot (don't merge resume duplicates of the same conversation)")
    ap.add_argument("--no-open", action="store_true")
    ap.set_defaults(dedupe=True)
    args = ap.parse_args()

    cfg = load_config()
    STATE["src"] = args.src or cfg.get("src") or DEFAULT_SRC
    STATE["out"] = args.out or cfg.get("out") or DEFAULT_OUT
    STATE["copy_raw"] = args.copy_raw or cfg.get("copy_raw", False)
    STATE["dedupe"] = args.dedupe

    if not os.path.isdir(STATE["src"]):
        print(f"⚠️  Claude Code folder not found: {STATE['src']}")
        print("    Pass --src PATH, or set it later in the web UI (⚙︎ Source).")

    # First run (or --update-only): build the archive.
    manifest_exists = os.path.exists(os.path.join(STATE["out"], ".manifest.json"))
    if args.update_only or not manifest_exists:
        print("Building archive from:", STATE["src"])
        stats = build_archive(STATE["src"], STATE["out"], STATE["copy_raw"], dedupe=STATE["dedupe"])
        print("Done:", stats)
        if args.update_only:
            return

    # serve
    httpd = None
    port = args.port
    for p in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("⚠️  No free port found.")
        sys.exit(1)

    url = f"http://localhost:{port}/"
    try:
        with open(os.path.join(CONFIG_DIR, "port"), "w") as f:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            f.write(str(port))
    except Exception:
        pass
    print("=" * 50)
    print("  📦 ccvault is running")
    print(f"  Open: {url}")
    print("  Close this window (or click Quit in the UI) to stop.")
    print("=" * 50)
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
