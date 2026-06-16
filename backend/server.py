"""bayou-desktop backend — a real local AGENT (tools) streamed to the chat UI.

Routes through bayou's agent machinery: MossBackend.stream() parses the model's
<tool_call> blocks, we run the tool, feed the result back, and loop — exactly
what makes the model agentic. Safe tools (web_search/web_fetch/read/grep/glob/ls)
auto-run; destructive tools (bash/write/edit) require an approve/deny round-trip
over the socket before they execute.

Run:
    pip install fastapi uvicorn 'websockets==12'
    BAYOU_OSS=~/projects/bayou-oss BAYOU_MODEL=~/.cache/moss_qwen9b_model \
    BAYOU_EXPERTS=~/.cache/moss_qwen9b_model BAYOU_FORCE=1 python3 backend/server.py
"""
from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import re
import sys
import time
import uuid
import random
import secrets
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# MLX GPU streams are THREAD-BOUND: model load + every generation must run on
# the SAME thread, or the stream is lost. All model work goes through this one.
MODEL_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")

BAYOU_OSS = os.path.expanduser(os.environ.get("BAYOU_OSS", "~/projects/bayou-oss"))
MODEL_DIR = os.path.expanduser(os.environ.get("BAYOU_MODEL", "~/.cache/moss_v2lite_model"))
EXPERTS_DIR = os.path.expanduser(os.environ.get("BAYOU_EXPERTS", "~/.cache/moss_v2lite_experts"))
CAP = int(os.environ.get("BAYOU_CAP", "6"))
MAX_TOKENS = int(os.environ.get("BAYOU_MAX_TOKENS", "640"))
MAX_TOOL_LOOPS = int(os.environ.get("BAYOU_MAX_TOOL_LOOPS", "8"))
MAX_TOOL_BYTES = 8000
FORCE_MOCK = os.environ.get("BAYOU_MOCK") == "1"
GATED = {"bash", "write", "edit"}          # require approval (is_destructive)
BLOCKED = {"agent", "ask_user"}            # need the App object; not exposed here
if BAYOU_OSS not in sys.path:
    sys.path.insert(0, BAYOU_OSS)

# Module-level FastAPI import so `ws: WebSocket` annotations resolve under
# `from __future__ import annotations` (else FastAPI 403s every handshake).
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    import uvicorn
except ImportError:
    FastAPI = None


def _rss_gb():
    try:
        kb = int(subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                                capture_output=True, text=True).stdout.strip() or 0)
        return kb / 1048576
    except Exception:
        return 0.0

def _clear_mlx():
    try:
        import mlx.core as mx
        (getattr(mx, "clear_cache", None) or getattr(getattr(mx, "metal", None),
                                                     "clear_cache", lambda: None))()
    except Exception:
        pass
    gc.collect()

_C = Path.home() / ".cache"
MODELS = {  # name -> (model_dir, experts_dir)   (qwen9b is dense: experts=model dir)
    "qwen35b": (_C/"moss_qwen35b_model", _C/"moss_qwen35b_experts"),
    "qwen30b": (_C/"moss_model", _C/"moss_experts"),
    "v2lite":  (_C/"moss_v2lite_model", _C/"moss_v2lite_experts"),
    "qwen9b":  (_C/"moss_qwen9b_model", _C/"moss_qwen9b_model"),
}

HELPER_HOME = Path.home() / ".bayou_desktop"
CONFIG_FILE = HELPER_HOME / "config.json"

# ----------------------- security -----------------------
# A local helper that can run bash/edit must NOT be drivable by any random
# website the user visits. Two gates:
#  1. Origin allowlist — browsers send a truthful Origin on the WS handshake;
#     we reject any origin not explicitly allowed (localhost/file in dev).
#  2. Pairing token — a per-install secret the UI must present (?token=…),
#     fetched once from /pair (which is itself origin-gated).
TOKEN_FILE = HELPER_HOME / "token"

def ensure_token():
    if os.environ.get("BAYOU_NO_TOKEN") == "1":
        return ""
    HELPER_HOME.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return t

TOKEN = ensure_token()
# Production: set BAYOU_ALLOWED_ORIGINS=https://your.site (comma-separated).
# Empty = dev mode (localhost + file:// permitted).
ALLOWED_ORIGINS = {o.strip().lower() for o in
                   os.environ.get("BAYOU_ALLOWED_ORIGINS", "").split(",") if o.strip()}
_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")

def origin_ok(origin):
    if not ALLOWED_ORIGINS:                 # dev: permit localhost + file:// (null) + no-origin
        return True
    if not origin or origin.lower() == "null":
        return False                        # prod: no file://, no header-less clients
    o = origin.lower()
    return bool(_LOCAL_ORIGIN.match(o)) or o in ALLOWED_ORIGINS

def token_ok(tok):
    return (not TOKEN) or (tok == TOKEN)

def read_config():
    try:
        return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        return {}

def write_config(d):
    HELPER_HOME.mkdir(parents=True, exist_ok=True)
    cur = read_config(); cur.update(d); CONFIG_FILE.write_text(json.dumps(cur))

def _has_weights(d):
    d = Path(d)
    return (d / "config.json").exists() and any(d.glob("*.safetensors"))

# First-run: small, tool-capable OSS models downloadable straight from HF as
# ready-to-run MLX builds (dense, no conversion needed). gb ≈ on-disk size.
DL_DIR = Path.home() / ".cache" / "bayou_models"
DOWNLOADS = {}   # name -> subprocess.Popen (for cancel)
DOWNLOADABLE = [
    {"name": "Qwen2.5-3B", "repo": "mlx-community/Qwen2.5-3B-Instruct-4bit", "gb": 1.8, "note": "fast · tools"},
    {"name": "Qwen2.5-7B", "repo": "mlx-community/Qwen2.5-7B-Instruct-4bit", "gb": 4.3, "note": "balanced · tools"},
    {"name": "Llama-3.2-3B", "repo": "mlx-community/Llama-3.2-3B-Instruct-4bit", "gb": 1.9, "note": "fast"},
    # MoE: downloaded then split into the per-expert offload layout on-device.
    {"name": "DeepSeek-V2-Lite", "repo": "mlx-community/DeepSeek-V2-Lite-Chat-4bit-mlx",
     "gb": 8.5, "note": "MoE · offload · smaller", "moe": True},
    {"name": "Qwen3-30B-MoE", "repo": "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",
     "gb": 16, "note": "MoE · offload · tools · big", "moe": True},
]

def downloaded_models():
    """name -> (model_dir, experts_dir). A '<name>__experts' sibling with
    per-layer files means the model was split for MoE offload."""
    out = {}
    if DL_DIR.exists():
        for d in DL_DIR.iterdir():
            if d.name.endswith("__experts") or not (d / "config.json").exists():
                continue
            exp = DL_DIR / (d.name + "__experts")
            ed = exp if (exp.exists() and any(exp.glob("layer_*.safetensors"))) else d
            out[d.name] = (d, ed)
    return out

def all_models():
    m = dict(MODELS); m.update(downloaded_models()); return m

def downloadable_list():
    out = []
    for d in DOWNLOADABLE:
        model_ok = (DL_DIR / d["name"] / "config.json").exists()
        exp_ok = (not d.get("moe")) or any((DL_DIR / (d["name"] + "__experts")).glob("layer_*.safetensors")) \
                 if model_ok else False
        out.append({**d, "installed": bool(model_ok and exp_ok)})
    return out

def any_model_installed():
    return any(md.exists() and (md / "config.json").exists() for md, _ in all_models().values())

def list_models():
    """Installed models + whether each fits current memory (for the picker)."""
    out = [{"name": "auto", "installed": True, "fits": True}]
    try:
        from bayou.runtime_safety import current_hardware, estimate_footprint
        hw = current_hardware()
        budget = getattr(hw, "metal_budget_gb", 0) or hw.total_gb * 0.72
    except Exception:
        budget = 0
    for name, (md, ed) in all_models().items():
        installed = md.exists() and (md/"config.json").exists()
        # offload = has a per-expert experts dir ⇒ loads via the MoE offload engine
        offload = bool(ed and Path(ed).exists() and any(Path(ed).glob("layer_*.safetensors")))
        fits = False
        if installed and budget:
            try:
                fp = estimate_footprint(md, Path(ed) if offload else None)
                peak = getattr(fp, "load_peak_gb", 0) or getattr(fp, "resident_gb", 0)
                fits = peak <= budget
            except Exception:
                fits = False
        out.append({"name": name, "installed": installed, "fits": fits, "offload": offload})
    return out

def current_model_name():
    md = str(getattr(ENGINE, "model_dir", "") or "")
    for name, (m, _e) in all_models().items():
        if str(m) == md:
            return name
    return ENGINE.model_name

# ----------------------- image OCR (macOS Vision) -----------------------
def ocr_image(data: bytes) -> str:
    """On-device OCR via macOS Vision. The model is text-only, so a dropped
    image (usually a screenshot) is converted to its text here."""
    try:
        import Quartz
        import Vision
        from Cocoa import NSData
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        src = Quartz.CGImageSourceCreateWithData(nsdata, None)
        if not src:
            return ""
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if not cg:
            return ""
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)            # accurate
        req.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        handler.performRequests_error_([req], None)
        lines = []
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1)
            if cands and len(cands):
                lines.append(str(cands[0].string()))
        return "\n".join(lines)
    except Exception as e:
        return f"(OCR failed: {type(e).__name__}: {e})"


# ----------------------- chat persistence -----------------------
CHAT_DIR = Path.home() / ".bayou_desktop" / "chats"

def _ser_msg(m):
    d = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls]
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    return d

def _de_msg(d):
    from bayou.conversation.state import Message, ToolCall
    tcs = [ToolCall(id=t["id"], name=t["name"], arguments=t.get("arguments", {}))
           for t in d.get("tool_calls", [])]
    return Message(role=d["role"], content=d.get("content", ""),
                   tool_calls=tcs, tool_call_id=d.get("tool_call_id"))

def chat_list():
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in CHAT_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            out.append({"id": d["id"], "title": d.get("title", "New chat"),
                        "updated": d.get("updated", 0)})
        except Exception:
            pass
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out

def chat_load(cid, system):
    from bayou.conversation.state import Conversation
    conv = Conversation(system=system)
    f = CHAT_DIR / f"{cid}.json"
    if f.exists():
        try:
            for md in json.loads(f.read_text()).get("messages", []):
                conv.messages.append(_de_msg(md))
        except Exception:
            pass
    return conv

def chat_save(cid, conv, title=None):
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    f = CHAT_DIR / f"{cid}.json"
    prev = {}
    if f.exists():
        try: prev = json.loads(f.read_text())
        except Exception: prev = {}
    rec = {"id": cid, "title": title or prev.get("title") or "New chat",
           "created": prev.get("created", time.time()), "updated": time.time(),
           "messages": [_ser_msg(m) for m in conv.messages if m.role != "system"]}
    f.write_text(json.dumps(rec))

def chat_delete(cid):
    f = CHAT_DIR / f"{cid}.json"
    if f.exists():
        f.unlink()

def chat_rename(cid, title):
    f = CHAT_DIR / f"{cid}.json"
    if f.exists():
        d = json.loads(f.read_text()); d["title"] = title; f.write_text(json.dumps(d))

def chat_title_from(conv):
    for m in conv.messages:
        if m.role == "user" and (m.content or "").strip():
            t = m.content.strip().replace("\n", " ")
            return t[:42] + ("…" if len(t) > 42 else "")
    return "New chat"

def renderable(conv):
    return [{"role": m.role, "content": m.content} for m in conv.messages
            if m.role in ("user", "assistant") and (m.content or "").strip()]

def has_msgs(conv):
    return any(m.role != "system" for m in conv.messages)

def drop_last_assistant(conv):
    """Remove trailing tool messages + the last assistant turn (for regenerate)."""
    while conv.messages and conv.messages[-1].role in ("tool", "assistant"):
        popped = conv.messages.pop()
        if popped.role == "assistant":
            break

def truncate_to_user(conv, n):
    """Keep system + everything before the n-th user message (0-based)."""
    seen = 0; keep = len(conv.messages)
    for i, m in enumerate(conv.messages):
        if m.role == "user":
            if seen == n:
                keep = i; break
            seen += 1
    del conv.messages[keep:]


def reexec(active=None):
    """Re-launch this process (fresh ⇒ no leaked wired Metal memory). The active
    model is persisted to config.json, so it survives launchd restarts too."""
    if active is not None:
        write_config({"model": None if active == "auto" else active})
    try:
        ENGINE.shutdown()
    except Exception:
        pass
    os.execve(sys.executable, [sys.executable] + sys.argv, dict(os.environ))


async def do_download(send, repo, name, moe=False):
    """Download an OSS MLX model from HF in a KILLABLE subprocess (so it can be
    cancelled), with progress, then activate it via config + re-exec. For MoE
    models, also split the experts into the per-expert offload layout."""
    import shutil
    loop = asyncio.get_event_loop()
    if name in DOWNLOADS and DOWNLOADS[name].poll() is None:
        return                                     # already downloading this model
    dest = DL_DIR / name
    expdir = DL_DIR / (name + "__experts")
    shutil.rmtree(dest, ignore_errors=True)       # start clean
    shutil.rmtree(expdir, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    expected = next((d["gb"] for d in DOWNLOADABLE if d["repo"] == repo), 4) * 1e9
    code = (f"from huggingface_hub import snapshot_download;"
            f"snapshot_download(repo_id={repo!r}, local_dir={str(dest)!r})")
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    DOWNLOADS[name] = proc
    await send({"type": "download_progress", "name": name, "pct": 0})
    while proc.poll() is None:
        await asyncio.sleep(1.0)
        try:
            sz = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        except Exception:
            sz = 0
        await send({"type": "download_progress", "name": name,
                    "pct": min(99, round(sz / max(1, expected) * 100))})
    DOWNLOADS.pop(name, None)
    rc = proc.returncode
    if rc and rc < 0:                              # terminated → cancelled
        shutil.rmtree(dest, ignore_errors=True)
        await send({"type": "download_cancelled", "name": name})
        return
    if rc != 0 or not _has_weights(dest):
        err = (proc.stderr.read().decode()[-280:] if proc.stderr else "") or "no weights"
        shutil.rmtree(dest, ignore_errors=True)
        await send({"type": "download_error", "name": name, "error": err})
        return

    if moe:
        # Split the stacked experts into the per-expert offload layout (the same
        # convert step that produced the bundled MoE models). mmap I/O — light.
        script = Path(BAYOU_OSS) / "scripts" / "convert_to_per_expert.py"
        if not script.exists():
            await send({"type": "download_error", "name": name,
                        "error": "convert_to_per_expert.py not found (set BAYOU_OSS)"})
            return
        expdir.mkdir(parents=True, exist_ok=True)
        cproc = subprocess.Popen(
            [sys.executable, str(script), "--model", str(dest), "--out", str(expdir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        DOWNLOADS[name] = cproc
        nlayers = 0
        while cproc.poll() is None:
            await asyncio.sleep(1.0)
            try:
                nlayers = len(list(expdir.glob("layer_*.safetensors")))
            except Exception:
                pass
            await send({"type": "download_progress", "name": name, "pct": 100,
                        "phase": "converting", "detail": f"{nlayers} layers split"})
        DOWNLOADS.pop(name, None)
        crc = cproc.returncode
        if crc and crc < 0:
            shutil.rmtree(dest, ignore_errors=True); shutil.rmtree(expdir, ignore_errors=True)
            await send({"type": "download_cancelled", "name": name}); return
        if crc != 0 or not any(expdir.glob("layer_*.safetensors")):
            err = (cproc.stderr.read().decode()[-280:] if cproc.stderr else "") or "convert failed"
            shutil.rmtree(expdir, ignore_errors=True)
            await send({"type": "download_error", "name": name,
                        "error": "expert split failed: " + err}); return

    await send({"type": "download_progress", "name": name, "pct": 100})
    await send({"type": "download_done", "name": name})
    await asyncio.sleep(0.4)
    reexec(active=name)                            # persist + load it (offload if experts present)

def cancel_download(name):
    proc = DOWNLOADS.get(name)
    if proc and proc.poll() is None:
        proc.terminate()


def _ram_gb():
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True).stdout) / 1e9
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        free = sum(int(l.split()[-1].rstrip(".")) for l in vm.splitlines()
                   if "free" in l or "inactive" in l) * 16384 / 1e9
        return round(total - free, 1)
    except Exception:
        return 0.0


def build_system():
    import datetime
    today = datetime.date.today().isoformat()
    return (
        f"You are bayou, a private assistant running locally on the user's Mac. "
        f"Today's date is {today}.\n\n"
        "IDENTITY: You are bayou. Never say you are DeepSeek, Qwen, Claude, GPT, "
        "OpenAI, or any other model or company, and never speculate about your "
        "underlying architecture. If asked who or what you are, who made you, or "
        "whether you were trained by some company, answer only that you are "
        "bayou, a local assistant.\n\n"
        "CURRENT INFO: Your training data ends well before today's date, so it is "
        "STALE and often wrong about anything that changes over time. For ANY "
        "question about who currently holds an office or title (e.g. a president), "
        "current events, recent versions or releases, prices, sports, weather, or "
        "'latest/current/now/today' anything — you MUST call web_search FIRST and "
        "base your answer ONLY on the results. Do not answer such questions from "
        "memory; your memory is outdated and will be wrong.\n\n"
        "TOOLS: Use read/grep/glob/ls to inspect files, web_fetch to read a "
        "specific URL, and bash/write/edit to act when asked. Prefer tools over "
        "guessing. Be concise and do not narrate your reasoning.\n\n"
        "Emit tool calls immediately and silently. NEVER write sentences like "
        "'I need to call web_search', 'let me search', or 'please hold on' — "
        "those are useless to the user. Just make the tool call directly; you'll "
        "get the result and can then answer."
    )


# Tells we use to detect "announced a tool but didn't actually call it".
_NUDGE_RE = re.compile(
    r"(web[_ ]?search|search the (web|internet)|let me (search|look|check)|"
    r"i\s*('?ll|will|need to|should|am going to)\s+(search|look|call|use|check)|"
    r"hold on|one moment|please (wait|hold)|i'?ll get|let me find)",
    re.I)


class Engine:
    def __init__(self):
        self.ready = False
        self.mock = FORCE_MOCK
        self.be = None
        self.model_name = "mock"
        self.blocks = []
        self.layers, self.experts = 32, 64
        self.tools = None
        self.schemas = []
        self.err = None

    def _candidates(self):
        # 1) config.json active model (set by download/swap) — only if its
        #    weights are actually present (guards incomplete downloads).
        mp = all_models()
        cfg = read_config().get("model")
        if cfg and cfg in mp and _has_weights(mp[cfg][0]):
            md, ed = mp[cfg]; return [(cfg, md, ed)]
        if cfg:                                   # stale / incomplete → forget it
            write_config({"model": None})
        # 2) explicit env override (dev)
        if "BAYOU_MODEL" in os.environ and _has_weights(MODEL_DIR):
            return [("custom", MODEL_DIR, EXPERTS_DIR)]
        # 3) auto-select: prefer the largest MoE-OFFLOAD model whose LOAD PEAK
        #    fits the Metal budget (so the offload engine is the default, and
        #    qwen35b's ~24 GB construction peak can't thrash this Mac).
        try:
            from bayou.runtime_safety import current_hardware, estimate_footprint
            hw = current_hardware()
            budget = getattr(hw, "metal_budget_gb", 0) or hw.total_gb * 0.72
        except Exception:
            budget = 1e9
        scored = []
        for n, (md, ed) in mp.items():
            if not _has_weights(md):
                continue
            has = bool(ed and Path(ed).exists() and any(Path(ed).glob("layer_*.safetensors")))
            try:
                fp = estimate_footprint(md, Path(ed) if has else None)
                peak = getattr(fp, "load_peak_gb", 0) or getattr(fp, "resident_gb", 0)
            except Exception:
                peak = 1e9
            if peak <= budget:
                scored.append((not has, -peak, n, md, ed))   # MoE first, then largest
        scored.sort()
        cands = [(n, md, ed) for _, _, n, md, ed in scored]
        return cands or [("custom", MODEL_DIR, EXPERTS_DIR)]

    def _force_smallest(self):
        from bayou.runtime_safety import estimate_footprint, ModelChoice
        cands = []
        for name, md, ed in self._candidates():
            md = Path(md)
            if not (md.exists() and (md/"config.json").exists()):
                continue
            has = bool(ed and Path(ed).exists() and any(Path(ed).glob("layer_*.safetensors")))
            fp = estimate_footprint(md, Path(ed) if has else None)
            peak = getattr(fp, "load_peak_gb", 0) or getattr(fp, "resident_gb", 0)
            cands.append((not has, peak, name, md, ed, has))
        if not cands:
            return None
        cands.sort()
        _, _, name, md, ed, has = cands[0]
        return ModelChoice(name=name, model_dir=md, experts_dir=Path(ed) if has else None,
                           offload=has, cap=CAP, plan=None)

    def load(self):
        if self.ready or self.mock:
            self.ready = True
            return
        try:
            from bayou.model.moss_backend import MossBackend
            from bayou.runtime_safety import current_hardware, select_best_model
            from bayou.tools.registry import default_registry
            hw = current_hardware()
            choice, plans = select_best_model(self._candidates(), hw, requested_cap=CAP)
            if choice is None and os.environ.get("BAYOU_FORCE") == "1":
                choice = self._force_smallest()
                if choice:
                    print(f"[backend] FORCE: loading {choice.name} ({hw.available_gb:.1f} GB free)",
                          file=sys.stderr)
            if choice is None:
                why = "; ".join(f"{n}: {p.reason}" for n, p in plans) or "no model"
                raise RuntimeError(f"nothing fits {hw.available_gb:.1f} GB free — {why}")
            print(f"[backend] right-sized: {choice.name} "
                  f"({'offload' if choice.offload else 'vanilla'})", file=sys.stderr)
            self.be = MossBackend(
                model_dir=choice.model_dir, offload=choice.offload,
                experts_dir=str(choice.experts_dir or EXPERTS_DIR), cap=choice.cap,
                cap_explicit=False, force_unsafe=os.environ.get("BAYOU_FORCE") == "1",
                kill_orphans=True)
            self.model_name = choice.name
            self.model_dir = str(choice.model_dir)
            layers = self.be.model.layers if hasattr(self.be.model, "layers") else self.be.model.model.layers
            self.blocks = [b for layer in layers
                           if (b := getattr(getattr(layer, "mlp", None), "switch_mlp", None)) is not None
                           and hasattr(b, "last_fired")]
            # tool registry — drop the tools that need the App object
            self.tools = default_registry()
            for n in BLOCKED:
                self.tools._tools.pop(n, None)
            self.schemas = self.tools.schemas()
            print(f"[backend] ready — {self.model_name}, {len(self.blocks)} MoE blocks, "
                  f"{len(self.schemas)} tools: {', '.join(t.name for t in self.tools.list())}",
                  file=sys.stderr)
            self.ready = True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            print(f"[backend] real engine unavailable -> mock. {self.err}", file=sys.stderr)
            self.mock = True
            self.ready = True

    def hello(self):
        return {"type": "hello", "model": self.model_name if not self.mock else "mock",
                "mock": self.mock, "tools": [t.name for t in (self.tools.list() if self.tools else [])],
                "models": list_models(), "current": current_model_name(),
                "offloading": bool(self.blocks),   # MoE expert-offload engine active
                "needs_model": bool(self.mock), "downloadable": downloadable_list(),
                "system_prompt": build_system(), "max_tokens": MAX_TOKENS, "error": self.err}

    def shutdown(self):
        try:
            if self.be:
                self.be.close()
        except Exception:
            pass
        _clear_mlx()


ENGINE = Engine()


class ThinkStripper:
    OPEN, CLOSE = "<think>", "</think>"
    def __init__(self):
        self.in_think = False; self.buf = ""
    @staticmethod
    def _tail(s, tag):
        for k in range(min(len(s), len(tag) - 1), 0, -1):
            if tag.startswith(s[-k:]):
                return k
        return 0
    def feed(self, text):
        self.buf += text; out = ""
        while self.buf:
            if not self.in_think:
                i = self.buf.find(self.OPEN)
                if i == -1:
                    keep = self._tail(self.buf, self.OPEN)
                    out += self.buf[:len(self.buf)-keep]; self.buf = self.buf[len(self.buf)-keep:]; break
                out += self.buf[:i]; self.buf = self.buf[i+len(self.OPEN):]; self.in_think = True
            else:
                i = self.buf.find(self.CLOSE)
                if i == -1:
                    self.buf = self.buf[len(self.buf)-self._tail(self.buf, self.CLOSE):]; break
                self.buf = self.buf[i+len(self.CLOSE):]; self.in_think = False
        return out


# ----------------------- tool helpers -----------------------
def _arg_summary(args):
    if not isinstance(args, dict):
        return ""
    for k in ("query", "command", "cmd", "path", "file_path", "pattern", "url"):
        if k in args:
            v = str(args[k]); return v if len(v) < 90 else v[:87] + "…"
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])[:90]

def _short(content, n=240):
    content = (content or "").strip()
    return content if len(content) <= n else content[:n] + f" … (+{len(content)-n} chars)"

# ----------------------- tool sandbox -----------------------
# Defense-in-depth: even an auto-run or user-approved tool must not read
# secrets or run obviously destructive commands. Enforced regardless of model
# or approval. BAYOU_WORKSPACE (if set) additionally confines file tools to it.
TOOL_TIMEOUT = int(os.environ.get("BAYOU_TOOL_TIMEOUT", "90"))
WORKSPACE = (os.path.realpath(os.path.expanduser(os.environ["BAYOU_WORKSPACE"]))
             if os.environ.get("BAYOU_WORKSPACE") else None)
_SENSITIVE_PATHS = ["/.ssh", "/.aws", "/.gnupg", "/.config/gh", "/.config/gcloud",
                    "/.kube", "/.docker/config", "/.netrc", "/library/keychains",
                    "/library/cookies", "/library/application support/google/chrome",
                    "/library/application support/firefox", "/.mozilla"]
_SENSITIVE_NAMES = re.compile(r"(id_rsa|id_ed25519|\.pem$|\.key$|\.p12$|/\.env(\.|$)|credential|secret)", re.I)
_DANGEROUS_BASH = re.compile(
    r"(rm\s+-[rf]*\s+(/|~|\$HOME|\*)|:\(\)\s*\{|mkfs|dd\s+if=.*of=/dev/|>\s*/dev/sd|"
    r"\bsudo\b|shutdown|reboot|\bhalt\b|chmod\s+-R\s+777\s+/|chown\s+-R\b.*\s+/|"
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh|\bwget\b[^|]*\|\s*(ba)?sh|"
    r"defaults\s+write|/etc/(passwd|sudoers))", re.I)

def _path_blocked(p):
    if not p:
        return None
    rp = os.path.realpath(os.path.expanduser(str(p))); low = rp.lower()
    if any(s in low for s in _SENSITIVE_PATHS) or _SENSITIVE_NAMES.search(rp):
        return f"sensitive path ({p})"
    if WORKSPACE and not (rp == WORKSPACE or rp.startswith(WORKSPACE + os.sep)):
        return f"outside the workspace ({p})"
    return None

def policy_violation(call):
    a = call.arguments if isinstance(call.arguments, dict) else {}
    if call.name == "bash":
        cmd = str(a.get("command") or a.get("cmd") or "")
        if _DANGEROUS_BASH.search(cmd):
            return "dangerous shell command"
        if any(s in cmd.lower() for s in _SENSITIVE_PATHS) or _SENSITIVE_NAMES.search(cmd):
            return "command references a sensitive path"
        return None
    for k in ("path", "file_path"):
        v = _path_blocked(a.get(k))
        if v:
            return v
    return None

async def _run_tool(tool, call):
    from bayou.tools.registry import ToolContext
    v = policy_violation(call)
    if v:
        return f"[error: blocked by sandbox — {v}]"
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: tool.run(call.arguments, ToolContext(app=None))),
            timeout=TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return f"[error: tool timed out after {TOOL_TIMEOUT}s]"
    except Exception as e:
        return f"[error: {type(e).__name__}: {e}]"


# ----------------------- the agent turn -----------------------
async def _stream_once(send, conv, max_tokens=MAX_TOKENS, enable_thinking=False):
    """One model response: stream text (CoT-stripped) to the UI, collect tool
    calls. Runs the whole generation on the MLX thread via a queue."""
    from bayou.conversation.state import ToolCall
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def producer():
        try:
            for ev in ENGINE.be.stream(conv, ENGINE.schemas, max_tokens=max_tokens,
                                       enable_thinking=enable_thinking):
                if ev.kind == "text":
                    loop.call_soon_threadsafe(q.put_nowait, ("text", ev.text))
                elif ev.kind == "tool_call":
                    loop.call_soon_threadsafe(q.put_nowait, ("call", ev.tool_call))
                elif ev.kind == "end":
                    break
        except Exception as e:  # noqa
            loop.call_soon_threadsafe(q.put_nowait, ("err", repr(e)))
        loop.call_soon_threadsafe(q.put_nowait, None)
    loop.run_in_executor(MODEL_EXEC, producer)

    stripper = ThinkStripper(); parts = []; calls = []; t0 = time.time(); n = 0
    while True:
        item = await q.get()
        if item is None:
            break
        kind, payload = item
        if kind == "err":
            print(f"[backend] gen error: {payload}", file=sys.stderr); break
        if kind == "text":
            n += 1
            disp = stripper.feed(payload)
            if disp:
                parts.append(disp)
            await send({"type": "token", "word": disp,
                        "metrics": {"toks": round(n/max(1e-3, time.time()-t0), 1), "ram": _ram_gb()}})
        elif kind == "call" and isinstance(payload, dict):
            calls.append(ToolCall.new(payload.get("name", ""), payload.get("arguments", {}) or {}))
    return "".join(parts).strip(), calls


async def agent_turn(send, conv, approvals, stop_evt, max_tokens=MAX_TOKENS, enable_thinking=False):
    from bayou.conversation.state import ToolResult
    await send({"type": "assistant_start", "id": "t"})
    loop = asyncio.get_event_loop()
    mem_rss = _rss_gb()
    nudged = False
    for _ in range(MAX_TOOL_LOOPS):
        if stop_evt.is_set():
            break
        text, calls = await _stream_once(send, conv, max_tokens, enable_thinking)
        conv.add_assistant(text, tool_calls=calls)
        if not calls or stop_evt.is_set():
            # Recovery: model ANNOUNCED a tool ("I need to call web_search…")
            # but didn't emit one. Nudge it once to actually make the call.
            if (not calls and not stop_evt.is_set() and not nudged
                    and _NUDGE_RE.search(text)):
                nudged = True
                conv.add_user("Do not describe or announce the tool — emit the "
                              "actual tool call now and use its result.")
                continue
            break
        for call in calls:
            tool = ENGINE.tools.get(call.name)
            viol = policy_violation(call) if tool is not None else None
            if tool is None:
                content = f"[no such tool: {call.name}]"
                await send({"type": "tool_result", "id": call.id, "name": call.name,
                            "ok": False, "summary": content})
            elif viol:
                # Hard block — never run, never even ask for approval.
                content = f"[blocked by sandbox: {viol}]"
                await send({"type": "tool_result", "id": call.id, "name": call.name,
                            "ok": False, "summary": "🛡 blocked — " + viol})
            else:
                if tool.is_destructive or call.name in GATED:
                    fut = loop.create_future(); approvals[call.id] = fut
                    await send({"type": "approval_request", "id": call.id, "name": call.name,
                                "summary": _arg_summary(call.arguments), "args": call.arguments})
                    try:
                        decision = await fut
                    except asyncio.CancelledError:
                        decision = "deny"
                    if decision != "approve":
                        content = "[denied by user]"
                        await send({"type": "tool_result", "id": call.id, "name": call.name,
                                    "ok": False, "summary": "denied"})
                        conv.add_tool_result(ToolResult(call.id, call.name, content, True))
                        continue
                await send({"type": "tool_start", "id": call.id, "name": call.name,
                            "summary": _arg_summary(call.arguments)})
                content = await _run_tool(tool, call)
                ok = not content.startswith("[error")
                await send({"type": "tool_result", "id": call.id, "name": call.name,
                            "ok": ok, "summary": _short(content)})
            conv.add_tool_result(ToolResult(call.id, call.name, content[:MAX_TOOL_BYTES],
                                            content.startswith("[error")))
    _clear_mlx()
    print(f"[mem] turn done: rss={_rss_gb():.1f}GB Δ{_rss_gb()-mem_rss:+.2f}GB", file=sys.stderr)
    await send({"type": "assistant_end", "id": "t"})


async def mock_turn(send):
    reply = ("(mock) The real model isn't loaded — free memory and restart the "
             "backend with a model. Until then I can't use tools.")
    words = reply.split(); i = 0
    await send({"type": "assistant_start", "id": "t"})
    while i < len(words):
        acc = random.choice([1, 1, 2]); chunk = words[i:i+acc]
        word = ("" if i == 0 else " ") + " ".join(chunk); i += acc
        await send({"type": "token", "word": word, "metrics": {"toks": 38.0, "ram": _ram_gb()}})
        await asyncio.sleep(0.05)
    await send({"type": "assistant_end", "id": "t"})


# ----------------------------- self-test --------------------------
def selftest():
    ENGINE.load()
    print(json.dumps(ENGINE.hello()))


# ----------------------------- server -----------------------------
def serve():
    if FastAPI is None:
        sys.exit("[backend] need: pip install fastapi uvicorn 'websockets==12'")
    app = FastAPI()
    GEN_LOCK = asyncio.Lock()

    # CORS so a hosted UI can read /pair etc. In production (BAYOU_ALLOWED_ORIGINS
    # set) we restrict to the allowlist + localhost; in dev we fall back to "*"
    # (the server-side origin_ok() is the real gate either way).
    from fastapi.middleware.cors import CORSMiddleware
    if ALLOWED_ORIGINS:
        app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS),
                           allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
                           allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
    else:
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                           allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def _pna(request, call_next):
        if request.method == "OPTIONS":
            from starlette.responses import Response
            r = Response(status_code=204)
            r.headers["Access-Control-Allow-Private-Network"] = "true"
            r.headers["Access-Control-Allow-Origin"] = request.headers.get("origin", "*")
            r.headers["Access-Control-Allow-Methods"] = "*"
            r.headers["Access-Control-Allow-Headers"] = "*"
            return r
        return await call_next(request)

    from starlette.responses import JSONResponse

    @app.get("/pair")
    async def pair(request: Request):
        if not origin_ok(request.headers.get("origin")):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        return JSONResponse({"token": TOKEN})

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        from bayou.conversation.state import Conversation, Message
        # auth gate: origin allowlist + pairing token (before accept)
        if not origin_ok(ws.headers.get("origin")) or not token_ok(ws.query_params.get("token")):
            await ws.close(code=1008)
            return
        await ws.accept()
        await asyncio.get_event_loop().run_in_executor(MODEL_EXEC, ENGINE.load)

        async def send(obj):
            await ws.send_text(json.dumps(obj))
        await send(ENGINE.hello())

        st = {"system": build_system(), "max_tokens": MAX_TOKENS}
        approvals: dict = {}
        stop_evt = asyncio.Event()
        q: asyncio.Queue = asyncio.Queue()

        # active conversation: resume most recent, else start fresh
        cl = chat_list()
        if cl:
            active = cl[0]["id"]; conv = chat_load(active, st["system"])
        else:
            active = uuid.uuid4().hex[:12]; conv = Conversation(system=st["system"])
        await send({"type": "chats", "chats": chat_list(), "active": active})
        await send({"type": "chat_loaded", "id": active, "messages": renderable(conv)})

        async def reader():
            try:
                while True:
                    msg = json.loads(await ws.receive_text())
                    t = msg.get("type")
                    if t == "approval":
                        fut = approvals.pop(msg.get("id"), None)
                        if fut and not fut.done():
                            fut.set_result(msg.get("decision"))
                    elif t == "stop":
                        stop_evt.set()
                    elif t == "switch_model":
                        await send({"type": "switching", "model": msg.get("name")})
                        await asyncio.sleep(0.25)
                        reexec(active=msg.get("name"))
                    elif t == "cancel_download":
                        cancel_download(msg.get("name"))
                    else:
                        q.put_nowait(msg)
            except WebSocketDisconnect:
                q.put_nowait(None)
        reader_task = asyncio.create_task(reader())

        last_dial = {"v": 58}

        def _dial_params(dial):
            # The deliberation dial spends free local compute for quality by
            # giving the agent more room to work (longer answers + more tool
            # steps): lazy → terse; easy → normal; deep → generous.
            # (We keep enable_thinking OFF: models that don't wrap CoT in
            # <think> leak their reasoning into the answer.)
            if dial < 33:
                return (max(192, min(st["max_tokens"], 320)), False)
            if dial < 67:
                return (st["max_tokens"], False)
            return (max(1200, st["max_tokens"]), False)

        async def run_turn(dial=None):
            if dial is not None:
                last_dial["v"] = dial
            maxtok, think = _dial_params(last_dial["v"])
            stop_evt.clear()
            async with GEN_LOCK:
                if ENGINE.mock:
                    await mock_turn(send)
                else:
                    await agent_turn(send, conv, approvals, stop_evt, maxtok, think)
            if len([m for m in conv.messages if m.role != "system"]) > 60:
                conv.messages[:] = [conv.messages[0]] + conv.messages[-60:]
            chat_save(active, conv, chat_title_from(conv))
            await send({"type": "chats", "chats": chat_list(), "active": active})

        try:
            while True:
                msg = await q.get()
                if msg is None:
                    break
                t = msg.get("type")
                if t == "prompt":
                    text = (msg.get("text") or "").strip()
                    images = msg.get("images") or []
                    if images:
                        loop = asyncio.get_event_loop()
                        parts = []
                        for im in images:
                            try:
                                raw = base64.b64decode(im.get("data", ""))
                                txt = await loop.run_in_executor(None, ocr_image, raw)
                            except Exception as e:
                                txt = f"(could not read image: {e})"
                            parts.append(f"Attached image \"{im.get('name','image')}\" — text "
                                         f"read from it via OCR:\n{txt or '(no text found)'}")
                        text = ("\n\n".join(parts) + ("\n\n" + text if text else "")).strip()
                    if text:
                        conv.add_user(text)
                        await run_turn(dial=msg.get("dial", 58))
                elif t == "regenerate":
                    drop_last_assistant(conv)
                    if any(m.role == "user" for m in conv.messages):
                        await run_turn()
                elif t == "edit_resend":
                    truncate_to_user(conv, int(msg.get("index", 0)))
                    conv.add_user((msg.get("text") or "").strip())
                    await run_turn()
                elif t == "new_chat":
                    if has_msgs(conv):
                        chat_save(active, conv, chat_title_from(conv))
                    active = uuid.uuid4().hex[:12]; conv = Conversation(system=st["system"])
                    await send({"type": "chats", "chats": chat_list(), "active": active})
                    await send({"type": "chat_loaded", "id": active, "messages": []})
                elif t == "load_chat":
                    if has_msgs(conv):
                        chat_save(active, conv, chat_title_from(conv))
                    active = msg.get("id"); conv = chat_load(active, st["system"])
                    await send({"type": "chat_loaded", "id": active, "messages": renderable(conv)})
                    await send({"type": "chats", "chats": chat_list(), "active": active})
                elif t == "delete_chat":
                    chat_delete(msg.get("id"))
                    if msg.get("id") == active:
                        cl2 = chat_list()
                        if cl2:
                            active = cl2[0]["id"]; conv = chat_load(active, st["system"])
                        else:
                            active = uuid.uuid4().hex[:12]; conv = Conversation(system=st["system"])
                        await send({"type": "chat_loaded", "id": active, "messages": renderable(conv)})
                    await send({"type": "chats", "chats": chat_list(), "active": active})
                elif t == "rename_chat":
                    chat_rename(msg.get("id"), msg.get("title") or "Untitled")
                    await send({"type": "chats", "chats": chat_list(), "active": active})
                elif t == "list_chats":
                    await send({"type": "chats", "chats": chat_list(), "active": active})
                elif t == "list_models":
                    await send({"type": "models", "models": list_models(), "current": current_model_name()})
                elif t == "list_downloadable":
                    await send({"type": "downloadable", "models": downloadable_list()})
                elif t == "download_model":
                    repo = msg.get("repo")
                    moe = next((d.get("moe", False) for d in DOWNLOADABLE if d["repo"] == repo), False)
                    asyncio.create_task(do_download(send, repo, msg.get("name"), moe))
                elif t == "settings":
                    if msg.get("system_prompt") is not None:
                        st["system"] = (msg.get("system_prompt") or "").strip() or build_system()
                        if conv.messages and conv.messages[0].role == "system":
                            conv.messages[0] = Message(role="system", content=st["system"])
                    if msg.get("max_tokens"):
                        st["max_tokens"] = max(64, int(msg["max_tokens"]))
                    await send({"type": "settings_ok",
                                "system_prompt": st["system"], "max_tokens": st["max_tokens"]})
        finally:
            if has_msgs(conv):
                chat_save(active, conv, chat_title_from(conv))
            reader_task.cancel()

    print("[backend] ws://127.0.0.1:8780/ws", file=sys.stderr)
    port = int(os.environ.get("BAYOU_PORT", "8780"))
    mode = ("origins=" + ",".join(sorted(ALLOWED_ORIGINS))) if ALLOWED_ORIGINS else "DEV (localhost+file)"
    print(f"[backend] security: {mode}; token={'on' if TOKEN else 'OFF'}", file=sys.stderr)
    if not ALLOWED_ORIGINS:
        print("[backend] ⚠ DEV MODE: any localhost/file:// page may connect. For a "
              "public deploy set BAYOU_ALLOWED_ORIGINS=https://your.site", file=sys.stderr)
    if not TOKEN:
        print("[backend] ⚠ token auth DISABLED (BAYOU_NO_TOKEN=1)", file=sys.stderr)
    if TOKEN:
        print(f"[backend] pairing token (paste into a hosted UI if needed): {TOKEN}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", ws="websockets")


if __name__ == "__main__":
    import atexit
    atexit.register(ENGINE.shutdown)
    selftest() if "--selftest" in sys.argv else serve()
