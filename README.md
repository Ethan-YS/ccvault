# 📦 ccvault — Claude Code Vault

**English** · [中文](README.zh.md)

<a href="https://linux.do" alt="LINUX DO"><img src="https://img.shields.io/badge/LINUX-DO-FFB003.svg?logo=data:image/svg%2bxml;base64,DQo8c3ZnIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgd2lkdGg9IjEwMCIgaGVpZ2h0PSIxMDAiPjxwYXRoIGQ9Ik00Ni44Mi0uMDU1aDYuMjVxMjMuOTY5IDIuMDYyIDM4IDIxLjQyNmM1LjI1OCA3LjY3NiA4LjIxNSAxNi4xNTYgOC44NzUgMjUuNDV2Ni4yNXEtMi4wNjQgMjMuOTY4LTIxLjQzIDM4LTExLjUxMiA3Ljg4NS0yNS40NDUgOC44NzRoLTYuMjVxLTIzLjk3LTIuMDY0LTM4LjAwNC0yMS40M1EuOTcxIDY3LjA1Ni0uMDU0IDUzLjE4di02LjQ3M0MxLjM2MiAzMC43ODEgOC41MDMgMTguMTQ4IDIxLjM3IDguODE3IDI5LjA0NyAzLjU2MiAzNy41MjcuNjA0IDQ2LjgyMS0uMDU2IiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZWNlY2VjO2ZpbGwtb3BhY2l0eToxIi8+PHBhdGggZD0iTTQ3LjI2NiAyLjk1N3EyMi41My0uNjUgMzcuNzc3IDE1LjczOGE0OS43IDQ5LjcgMCAwIDEgNi44NjcgMTAuMTU3cS00MS45NjQuMjIyLTgzLjkzIDAgOS43NS0xOC42MTYgMzAuMDI0LTI0LjM4N2E2MSA2MSAwIDAgMSA5LjI2Mi0xLjUwOCIgc3R5bGU9InN0cm9rZTpub25lO2ZpbGwtcnVsZTpldmVub2RkO2ZpbGw6IzE5MTkxOTtmaWxsLW9wYWNpdHk6MSIvPjxwYXRoIGQ9Ik03Ljk4IDcwLjkyNmMyNy45NzctLjAzNSA1NS45NTQgMCA4My45My4xMTNRODMuNDI2IDg3LjQ3MyA2Ni4xMyA5NC4wODZxLTE4LjgxIDYuNTQ0LTM2LjgzMi0xLjg5OC0xNC4yMDMtNy4wOS0yMS4zMTctMjEuMjYyIiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZjlhZjAwO2ZpbGwtb3BhY2l0eToxIi8+PC9zdmc+" /></a>

Back up, browse, search, and export your [Claude Code](https://claude.com/claude-code) conversations — **100% local, zero dependencies, no network**.

![ccvault — browse, search and export your Claude Code conversations](docs/screenshot-en.png)

*Browse, search, filter, archive, and export — in English or 中文 (one-click toggle). All data stays on your machine.*

Claude Code stores your chats as `.jsonl` transcripts under `~/.claude/projects`. They're complete but hard to read, and they can be lost if Claude Code is reinstalled or cleaned. **ccvault** turns them into a clean local archive you fully own, with a browser UI to read, search, and export them.

## Features

- 🗂 **Browse** every conversation in a foldered, searchable sidebar
- 💬 **Readable** chat-bubble view; tool calls & results collapsible; a **"chat only"** mode that hides everything but the messages
- 🔍 **Filter** which projects to show; **archive** (hide) individual chats — restorable anytime
- ⬇ **Export** a single chat, a whole project, or everything you currently see — as Markdown / a zip
- 🔄 **Incremental update** — only new or changed chats get reprocessed
- ♊ **Auto-dedupe** — Claude Code saves a fresh snapshot every time you resume a chat; ccvault keeps only the most complete one, so the same conversation never shows up twice (`--no-dedupe` keeps them all)
- 🔒 **Private by design** — runs entirely on `127.0.0.1`, never touches the network, never modifies your original transcripts

## Requirements

- **Python 3.8+** — standard library only, nothing to `pip install`
- A modern web browser

## Quick start

```bash
git clone https://github.com/Ethan-YS/ccvault.git
cd ccvault
python3 ccvault.py
```

It auto-detects `~/.claude/projects`, builds a local archive at `~/.ccvault/archive`, and opens your browser.

- **macOS** — double-click `ccvault.command`
- **Windows** — double-click `ccvault.bat`
- **Linux / anything** — `python3 ccvault.py`

## Options

```
python3 ccvault.py --src PATH      # custom transcripts folder
python3 ccvault.py --out PATH      # custom archive output folder
python3 ccvault.py --port 8765
python3 ccvault.py --copy-raw      # also copy the original .jsonl into the archive
python3 ccvault.py --no-dedupe     # keep every snapshot (don't merge resume duplicates)
python3 ccvault.py --update-only   # rebuild the archive and exit (no server)
```

You can also point ccvault at a different transcripts folder from the web UI (**⚙︎ Source**) — useful if your `.claude` lives somewhere non-standard, or to browse a backup.

## Privacy

- Everything runs **locally**. There are **no network calls, ever.**
- Your transcripts are read **read-only**; the originals under `~/.claude/projects` are never modified.
- The archive lives at `~/.ccvault/archive` — **outside this repo**. Conversation data is **never committed** (`.gitignore` blocks it).

## Notes

- Claude Code does **not** store the model's *thinking* text in transcripts (only an encrypted signature), so thinking can't be shown or exported. This is a limitation of the source data, not ccvault.

## License

[MIT](LICENSE)
