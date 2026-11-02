"""Breeze Developer API 客户端（服务端用）

- 音色库/模型列表：优先用游客自带的 API key（能看自己的私有音色），没有就用服务器 session cookie
- TTS / 音色设计：只能用游客自己的 API key
- key 只在本进程内存里用于一次转发，不落盘、不写日志、不进 URL
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.breeze.blue"
COOKIE_FILE = os.environ.get("BREEZE_COOKIE_FILE") or os.path.expanduser("~/.config/breezeblue/cookies.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ---------------------------------------------------------------
# 演示数据：没配置 Breeze cookie 也没填自己的 key 时，音色库用这份示例
# 让项目 clone 下来就能直接看到完整界面（README 截图也用它）
# ---------------------------------------------------------------
_P = "/static/demo-preview.mp3"
DEMO_VOICES = [
    {"voice_id": "voc_demo0001", "name": "晨光少年", "desc": "清亮干净的年轻男声，语气平稳，适合科技解说与产品介绍。",
     "lang": "zh", "gender": "male", "age": "young", "tone": ["bright", "articulate"], "accent": "chinese", "category": "narration"},
    {"voice_id": "voc_demo0002", "name": "晚风轻语", "desc": "二十多岁的温柔女声，语速偏慢，尾音轻软，像临睡前的闲聊。",
     "lang": "zh", "gender": "female", "age": "young", "tone": ["gentle", "warm"], "accent": "chinese", "category": "podcast"},
    {"voice_id": "voc_demo0003", "name": "沉稳叙述", "desc": "沉稳的中年男声，低沉有磁性，适合纪录片旁白与品牌宣传片。",
     "lang": "zh", "gender": "male", "age": "middle_aged", "tone": ["calm", "authoritative"], "accent": "chinese", "category": "cinematic"},
    {"voice_id": "voc_demo0004", "name": "元气少女", "desc": "活泼的少女音，语速偏快，带笑意，适合短视频口播与产品种草。",
     "lang": "zh", "gender": "female", "age": "young", "tone": ["energetic", "playful"], "accent": "chinese", "category": "social-media"},
    {"voice_id": "voc_demo0005", "name": "电台夜话", "desc": "气声较多的成熟女声，节奏舒缓，适合睡前故事与深夜电台。",
     "lang": "zh", "gender": "female", "age": "middle_aged", "tone": ["reflective", "gentle"], "accent": "chinese", "category": "podcast"},
    {"voice_id": "voc_demo0006", "name": "课堂讲解", "desc": "口齿清晰的青年教师音色，语速适中，适合课件与知识科普。",
     "lang": "zh", "gender": "female", "age": "young", "tone": ["precise", "friendly"], "accent": "chinese", "category": "learning"},
    {"voice_id": "voc_demo0007", "name": "North Light", "desc": "A warm British female voice with soft consonants, made for calm narration.",
     "lang": "en", "gender": "female", "age": "young", "tone": ["warm", "refined"], "accent": "british", "category": "narration"},
    {"voice_id": "voc_demo0008", "name": "Studio Anchor", "desc": "A confident American male voice with a broadcast tone and steady pacing.",
     "lang": "en", "gender": "male", "age": "middle_aged", "tone": ["authoritative", "steady"], "accent": "american", "category": "ads-brand"},
    {"voice_id": "voc_demo0009", "name": "关西口音", "desc": "带一点方言味道的男声，语气亲切，适合生活类内容与角色扮演。",
     "lang": "zh", "gender": "male", "age": "middle_aged", "tone": ["friendly", "playful"], "accent": "mandarin_sichuan", "category": "roleplay"},
    {"voice_id": "voc_demo0010", "name": "少女旁白", "desc": "轻柔的少女旁白，气声细腻，适合动画配音与治愈系内容。",
     "lang": "zh", "gender": "female", "age": "young", "tone": ["gentle", "compassionate"], "accent": "chinese", "category": "animation"},
    {"voice_id": "voc_demo0011", "name": "游戏旁白", "desc": "偏低沉的男声，略带颗粒感，适合游戏角色与世界观介绍。",
     "lang": "zh", "gender": "male", "age": "young", "tone": ["calm", "refined"], "accent": "chinese", "category": "gaming"},
    {"voice_id": "voc_demo0012", "name": "客服小晴", "desc": "自然亲切的女声，语气耐心，适合客服应答与语音提示。",
     "lang": "zh", "gender": "female", "age": "young", "tone": ["friendly", "sincere"], "accent": "chinese", "category": "support-agents"},
]
for _v in DEMO_VOICES:
    _v["preview"] = _P


def demo_mode() -> bool:
    return server_cookie() is None


def demo_list(params: dict) -> dict:
    """按前端传的筛选条件过滤示例数据，并用 offset 做游标分页"""
    items = list(DEMO_VOICES)
    q = (params.get("search") or "").strip().lower()
    if q:
        items = [v for v in items if q in v["name"].lower() or q in v["desc"].lower() or q in " ".join(v["tone"]).lower()]
    if params.get("language_code"):
        items = [v for v in items if v["lang"] == params["language_code"]]
    if params.get("gender"):
        items = [v for v in items if v["gender"] == params["gender"]]
    if params.get("primary_category_code"):
        items = [v for v in items if v["category"] == params["primary_category_code"]]
    size = int(params.get("page_size") or 24)
    offset = int(params.get("next_page_token") or 0)
    page = items[offset:offset + size]
    nxt = offset + size
    return {
        "voices": page, "total": len(items), "page": offset // max(size, 1) + 1,
        "page_size": size, "has_more": nxt < len(items),
        "next_page_token": str(nxt) if nxt < len(items) else None,
        "demo": True,
    }


class ApiError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {payload}")


def server_cookie() -> str | None:
    """服务器自己的 breeze session（用于音色库浏览，不用游客 key）"""
    try:
        raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
        data = json.loads(raw)
        if isinstance(data, list):
            return "; ".join(f"{c['name']}={c['value']}" for c in data if "name" in c)
        return data if isinstance(data, str) else None
    except Exception:
        return None


def call(path: str, method: str = "GET", body: dict | None = None,
         api_key: str | None = None, raw: bool = False) -> tuple[dict | bytes, dict]:
    """返回 (payload, headers)。raw=True 时 payload 是 bytes（音频）"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    if api_key:
        req.add_header("xi-api-key", api_key)
    else:
        ck = server_cookie()
        if not ck:
            raise ApiError(503, {"error": "服务器未配置 session，请填入自己的 API key"})
        req.add_header("Cookie", ck)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            head = {k.lower(): v for k, v in r.getheaders()}
            payload = r.read()
            if raw:
                return payload, head
            try:
                return json.loads(payload.decode() or "{}"), head
            except json.JSONDecodeError:
                return {"raw": payload[:500].decode("utf-8", "replace")}, head
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(txt)
        except json.JSONDecodeError:
            payload = {"detail": txt[:400]}
        raise ApiError(e.code, payload)


# ---------- 轻量缓存（音色库这类公共数据） ----------
_cache: dict[str, tuple[float, object]] = {}
TTL = 600


def cached(key: str, fn, ttl: int = TTL):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def list_voices(params: dict, api_key: str | None = None) -> dict:
    # next_page_token 存在时不要再带 page（sort=trend 要求 page=1），直接置 1 保险
    if params.get("next_page_token"):
        params = {**params, "page": 1}
    qs = "&".join(
        f"{urllib.parse.quote(str(k))}={urllib.parse.quote(str(v))}"
        for k, v in params.items() if v not in (None, "")
    )
    path = "/v1/voices" + (f"?{qs}" if qs else "")

    def fetch():
        data, _ = call(path, api_key=api_key)
        out = []
        for v in data.get("voices", []):
            out.append({
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "desc": v.get("description") or "",
                "preview": v.get("preview_url"),
                "lang": v.get("language_code"),
                "gender": v.get("gender"),
                "age": v.get("age"),
                "tone": v.get("tone") or [],
                "accent": v.get("accent"),
                "tags": v.get("tags") or [],
                "category": v.get("primary_category_code"),
                "type": v.get("voice_type"),
                "created": v.get("created_at_unix"),
            })
        return {
            "voices": out,
            "total": data.get("total"),
            "page": data.get("page"),
            "page_size": data.get("page_size"),
            "has_more": data.get("has_more"),
            "next_page_token": data.get("next_page_token"),
        }

    # 带 key 时（能看到私有音色）不缓存
    if api_key:
        return fetch()
    if demo_mode():
        return demo_list(params)
    return cached(path, fetch)


def list_models(api_key: str | None = None) -> list:
    def fetch():
        data, _ = call("/v1/models", api_key=api_key)
        return data
    return cached("models", fetch)


def current_key_info(api_key: str) -> dict:
    data, _ = call("/v1/api-keys/current", api_key=api_key)
    return data


def balance(api_key: str) -> dict:
    data, _ = call("/v1/balance", api_key=api_key)
    return data


def synthesize(api_key: str, voice_id: str, text: str, model_id: str | None,
               language_code: str | None, output_format: str = "mp3",
               instructions: str | None = None,
               voice_settings: dict | None = None) -> tuple[bytes, dict]:
    body = {"text": text}
    if model_id:
        body["model_id"] = model_id
    if language_code:
        body["language_code"] = language_code
    if instructions:
        body["instructions"] = instructions
    if voice_settings:
        body["voice_settings"] = voice_settings
    fmt = output_format if output_format in ("mp3", "wav_24000", "wav") else "mp3"
    return call(f"/v1/text-to-speech/{urllib.parse.quote(voice_id)}?output_format={fmt}",
                method="POST", body=body, api_key=api_key, raw=True)


def design_previews(api_key: str, description: str, text: str,
                    model_id: str | None = None, language_code: str | None = None,
                    preview_count: int = 2, guidance_scale: float | None = None) -> dict:
    body = {"voice_description": description, "text": text or None,
            "preview_count": max(1, min(3, preview_count))}
    if model_id:
        body["model_id"] = model_id
    if language_code:
        body["language_code"] = language_code
    if guidance_scale:
        body["guidance_scale"] = guidance_scale
    data, _ = call("/v1/voice-previews/design?output_format=mp3",
                   method="POST", body=body, api_key=api_key)
    return data


def save_designed_voice(api_key: str, generated_voice_id: str, name: str,
                        description: str | None = None,
                        language_code: str | None = None) -> dict:
    """字段名是 voice_name（不是 name），文档里没写清楚，实测才确认"""
    body = {"voice_name": name[:60]}
    if description:
        body["voice_description"] = description[:500]
    if language_code:
        body["language_code"] = language_code
    data, _ = call(f"/v1/voice-previews/{urllib.parse.quote(generated_voice_id)}/save",
                   method="POST", body=body, api_key=api_key)
    return data



def voice_info(voice_id: str, api_key: str | None = None) -> dict:
    data, _ = call(f"/v1/voices/{urllib.parse.quote(voice_id)}", api_key=api_key)
    return data
