"""Breeze 音色试听台 —— 在线填 key 试听官方音色 / 文本合成 / 音色创作

设计原则（苏苏的要求）：
1. API key 只保存在访客浏览器（localStorage），服务端只在一次请求内转发，不落盘、不写日志、不进 URL
2. 音频默认只在内存里播放；访客手动打开「允许保存在本机」才写入浏览器 IndexedDB
3. 每个浏览器各自一份 key 和记录，互不干扰
"""
import io
import json
import logging
import os
import re
import time

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

import bb_api

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("breeze-voice")

# 兜底：任何日志里出现疑似 key 一律打码
_KEY_RE = re.compile(r"brz_[A-Za-z0-9_\-]{6,}")
class _Scrub(logging.Filter):
    def filter(self, record):
        try:
            record.msg = _KEY_RE.sub("brz_***", str(record.msg))
        except Exception:
            pass
        return True
for h in logging.getLogger().handlers:
    h.addFilter(_Scrub())


def req_key() -> str | None:
    """从请求头取访客的 API key（只存在于本次请求内存中）"""
    k = request.headers.get("X-BB-Key") or ""
    k = k.strip()
    return k if k.startswith("brz_") and len(k) > 12 else None


def err(status: int, message: str, code: str | None = None):
    return jsonify({"ok": False, "code": code, "message": message}), status


# OpenAI 兼容网关允许跨域（key 由调用方每次自带，服务器不留存）
_CORS_PATHS = ("/v1/",)


@app.after_request
def _cors(resp):
    if request.path.startswith(_CORS_PATHS):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, xi-api-key, X-BB-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@app.after_request
def _harden(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # 绝不缓存带用户 key 的响应
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/api/config")
def api_config():
    root = request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and root.startswith("http://"):
        root = "https://" + root[len("http://"):]
    return jsonify({"ok": True, "base_url": root + "/v1", "demo": bb_api.demo_mode()})


@app.route("/api/models")
def api_models():
    if bb_api.demo_mode() and not req_key():
        return jsonify({"ok": True, "models": [
            {"model_id": "breeze-tts-2", "name": "Breeze TTS 2"},
            {"model_id": "breeze-tts-2-multilingual", "name": "Breeze TTS 2 Multilingual"},
        ]})
    try:
        return jsonify({"ok": True, "models": bb_api.list_models(req_key())})
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))


@app.route("/api/voices")
def api_voices():
    # 统一走 next_page_token 游标分页：sort=trend 时 page 必须是 1（官方限制），
    # 其他排序也会返回 token，所以前端只用 cursor 翻页，上一页靠前端的 token 栈。
    params = {
        "voice_type": request.args.get("voice_type", "default"),
        "page": 1,
        "page_size": min(int(request.args.get("page_size", 24) or 24), 100),
        "search": request.args.get("search") or None,
        "language_code": request.args.get("language_code") or None,
        "gender": request.args.get("gender") or None,
        "primary_category_code": request.args.get("category") or None,
        "sort": request.args.get("sort") or None,
        "next_page_token": request.args.get("cursor") or None,
    }
    try:
        data = bb_api.list_voices(params, req_key())
        return jsonify({"ok": True, **data})
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))


@app.route("/api/key-check", methods=["POST"])
def api_key_check():
    key = req_key()
    if not key:
        return err(400, "请先填入 API key")
    try:
        raw = bb_api.current_key_info(key)
        info = raw.get("api_key", raw) if isinstance(raw, dict) else {}
        bal = None
        try:
            b = bb_api.balance(key)
            if isinstance(b, dict):
                bal = b.get("balance")
        except Exception:
            pass
        return jsonify({"ok": True, "key": info, "balance": bal})
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))


@app.route("/api/tts", methods=["POST"])
def api_tts():
    key = req_key()
    if not key:
        return err(401, "需要先填入自己的 Breeze API key", "NO_KEY")
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    voice_id = (body.get("voice_id") or "").strip()
    if not text or not voice_id:
        return err(400, "text 和 voice_id 都是必填")
    if len(text) > 2000:
        return err(400, "文本太长了（最多 2000 字）")
    try:
        settings = {}
        try:
            sp = float(body.get("speed") or 1)
            if 0.5 <= sp <= 2.0 and abs(sp - 1) > 0.001:
                settings["speed"] = sp
        except (TypeError, ValueError):
            pass
        audio, head = bb_api.synthesize(
            key, voice_id, text,
            model_id=body.get("model_id") or None,
            language_code=body.get("language_code") or None,
            output_format=body.get("output_format") or "mp3",
            instructions=(body.get("instructions") or None),
            voice_settings=settings or None,
        )
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))
    resp = Response(audio, mimetype="audio/mpeg")
    resp.headers["X-History-Id"] = head.get("history-item-id", "")
    resp.headers["X-Audio-Bytes"] = str(len(audio))
    return resp


@app.route("/api/design", methods=["POST"])
def api_design():
    key = req_key()
    if not key:
        return err(401, "需要先填入自己的 Breeze API key", "NO_KEY")
    body = request.get_json(silent=True) or {}
    desc = (body.get("voice_description") or "").strip()
    text = (body.get("text") or "").strip()
    if len(desc) < 3:
        return err(400, "音色描述至少写 3 个字")
    try:
        data = bb_api.design_previews(
            key, desc, text,
            model_id=body.get("model_id") or None,
            language_code=body.get("language_code") or None,
            preview_count=int(body.get("preview_count") or 2),
            guidance_scale=body.get("guidance_scale") or None,
        )
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))
    return jsonify({"ok": True, **data})


@app.route("/api/design/save", methods=["POST"])
def api_design_save():
    key = req_key()
    if not key:
        return err(401, "需要先填入自己的 Breeze API key", "NO_KEY")
    body = request.get_json(silent=True) or {}
    gid = (body.get("generated_voice_id") or "").strip()
    name = (body.get("name") or "").strip()
    if not gid:
        return err(400, "缺少 generated_voice_id")
    if not name:
        name = "设计音色 " + time.strftime("%m-%d %H:%M")
    try:
        return jsonify({"ok": True, **bb_api.save_designed_voice(
            key, gid, name,
            description=(body.get("description") or "").strip() or None,
            language_code=(body.get("language_code") or "").strip() or None)})
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))


def _msg(e: bb_api.ApiError) -> str:
    p = e.payload if isinstance(e.payload, dict) else {}
    detail = p.get("detail") or p.get("error") or p.get("message")
    if isinstance(detail, list):
        detail = "；".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    if not detail:
        detail = {401: "API key 无效或已被禁用", 402: "额度不足或 key 的预算已用尽",
                  403: "没有权限", 429: "请求太频繁，稍等一下再试"}.get(e.status, f"上游返回 {e.status}")
    return str(detail)[:300]


def _code(e: bb_api.ApiError) -> str | None:
    p = e.payload if isinstance(e.payload, dict) else {}
    return p.get("code")


# ---------------------------------------------------------------
# OpenAI 兼容层：让 openai SDK / 各类客户端直接调用
#   POST /v1/audio/speech   {model, input, voice, response_format, speed, instructions}
#   GET  /v1/models
# 鉴权：Authorization: Bearer brz_xxx  （也接受 xi-api-key / X-BB-Key）
# ---------------------------------------------------------------

_FMT = {
    "mp3": ("mp3", "audio/mpeg"),
    "wav": ("wav_24000", "audio/wav"),
    "opus": ("opus", "audio/opus"),
    "aac": ("aac", "audio/aac"),
    "flac": ("flac", "audio/flac"),
    "pcm": ("pcm", "audio/pcm"),
}
_MODEL_ALIAS = {
    "breeze-tts-2": "breeze-tts-2",
    "breeze-tts-2-multilingual": "breeze-tts-2-multilingual",
    "tts-1": "breeze-tts-2",
    "tts-1-hd": "breeze-tts-2",
    "breeze": "breeze-tts-2",
}


def bearer_key() -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        k = auth[7:].strip()
        if k.startswith("brz_"):
            return k
    k = (request.headers.get("xi-api-key") or "").strip()
    return k if k.startswith("brz_") else None


def oai_err(status: int, message: str, etype: str = "invalid_request_error", code: str | None = None):
    return jsonify({"error": {"message": message, "type": etype, "param": None, "code": code}}), status


@app.route("/v1/audio/speech", methods=["POST", "OPTIONS"])
def openai_speech():
    if request.method == "OPTIONS":
        return ("", 204)
    key = req_key() or bearer_key()
    if not key:
        return oai_err(401, "缺少 API key：请用 Authorization: Bearer brz_... 传你自己的 Breeze key",
                       "authentication_error", "invalid_api_key")
    body = request.get_json(silent=True) or {}
    text = (body.get("input") or body.get("text") or "").strip()
    voice = (body.get("voice") or body.get("voice_id") or "").strip()
    if not text:
        return oai_err(400, "缺少 input（要合成的文字）", code="missing_input")
    if not voice:
        return oai_err(400, "缺少 voice（Breeze 的音色 ID，形如 voc_xxx）", code="missing_voice")

    fmt = (body.get("response_format") or "mp3").lower()
    o_fmt, ctype = _FMT.get(fmt, _FMT["mp3"])
    model = _MODEL_ALIAS.get((body.get("model") or "").strip().lower(), body.get("model") or None)
    settings = {}
    try:
        sp = float(body.get("speed") or 1)
        if sp and 0.5 <= sp <= 2.0 and abs(sp - 1) > 0.001:
            settings["speed"] = sp
    except (TypeError, ValueError):
        pass
    try:
        audio, head = bb_api.synthesize(
            key, voice, text[:2000],
            model_id=model,
            language_code=(body.get("language") or body.get("language_code") or None),
            output_format=o_fmt,
            instructions=(body.get("instructions") or None),
            voice_settings=settings or None,
        )
    except bb_api.ApiError as e:
        etype = {401: "authentication_error", 402: "insufficient_quota", 403: "permission_error",
                 429: "rate_limit_error"}.get(e.status, "upstream_error")
        return oai_err(e.status if e.status < 500 else 502, _msg(e), etype, _code(e))

    resp = Response(audio, mimetype=ctype)
    resp.headers["X-History-Id"] = head.get("history-item-id", "")
    return resp


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def openai_models():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        models = bb_api.list_models(req_key() or bearer_key())
    except bb_api.ApiError:
        models = [{"model_id": "breeze-tts-2", "name": "Breeze TTS 2"},
                  {"model_id": "breeze-tts-2-multilingual", "name": "Breeze TTS 2 Multilingual"}]
    data = [{"id": m.get("model_id"), "object": "model", "created": 0,
             "owned_by": "breezeblue", "name": m.get("name")} for m in models]
    return jsonify({"object": "list", "data": data})


@app.route("/api/voice-info")
def api_voice_info():
    vid = (request.args.get("voice_id") or "").strip()
    if not vid.startswith("voc_"):
        return err(400, "音色 ID 应该以 voc_ 开头")
    if bb_api.demo_mode() and not req_key():
        for v in bb_api.DEMO_VOICES:
            if v["voice_id"] == vid:
                return jsonify({"ok": True, "voice": v})
        return err(404, "示例数据里没有这个音色 ID（接入真实后端后可用）")
    try:
        v = bb_api.voice_info(vid, req_key())
    except bb_api.ApiError as e:
        return err(e.status, _msg(e), _code(e))
    return jsonify({"ok": True, "voice": {
        "voice_id": v.get("voice_id"), "name": v.get("name"),
        "desc": v.get("description") or "", "lang": v.get("language_code"),
        "gender": v.get("gender"), "accent": v.get("accent"),
        "tone": v.get("tone") or [], "preview": v.get("preview_url"),
    }})


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8674))
    from waitress import serve
    print(f"breeze-voice on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8, channel_timeout=200)
