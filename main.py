# -*- coding: utf-8 -*-
"""
astrbot_plugin_ace_singing - 专精原声原曲翻唱插件
基于 Seed-VC V1 (44.1kHz) 神经歌声转换与 AstrBot 4.x 架构
"""

import array
import asyncio
import base64
import json
import math
import os
import random
import re
import subprocess
import time
import uuid
import urllib.request
from collections import Counter

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.star import StarTools
    _DATA_DIR = str(StarTools.get_data_dir("astrbot_plugin_ace_singing"))
except Exception:
    _DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
TMP_DIR = os.path.join(_DATA_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)
CRED_PATH = os.path.join(_DATA_DIR, "qq_credential.json")

# QQ 音乐的 vkey 与「取键时的出口 IP」绑定。若宿主进程带有系统级代理环境变量
# （HTTP_PROXY/HTTPS_PROXY），qqmusic_api（httpx 默认 trust_env=True）取键会走代理，
# 而音源下载插件端明确不走代理，两个 IP 不一致会被 QQ 音乐 CDN 全量 404。
# 这里只为 QQ 音乐相关域名注入 NO_PROXY（不影响 AstrBot 其他流量走代理）。
def _ensure_qqmusic_no_proxy():
    domains = [
        "qq.com", ".qq.com", "y.qq.com", "c.y.qq.com", "u.y.qq.com", "i.y.qq.com",
        "c6.y.qq.com", "isure.stream.qqmusic.qq.com", "dl.stream.qqmusic.qq.com",
        "ws.stream.qqmusic.qq.com", "mobileservice.music.tc.qq.com",
        "ssl.ptlogin2.qq.com", "ssl.ptlogin2.graph.qq.com", "xui.ptlogin2.qq.com",
        "graph.qq.com", "open.weixin.qq.com", "lp.open.weixin.qq.com",
        "mu.y.qq.com",
    ]
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        items = [x.strip() for x in current.split(",") if x.strip()]
        changed = False
        for d in domains:
            low = d.lower()
            if not any(item == low or item == "." + low or item.endswith(low) for item in items):
                items.append(d)
                changed = True
        if changed:
            os.environ[key] = ",".join(dict.fromkeys(items))
_ensure_qqmusic_no_proxy()

# qqmusic_api 0.8.x 的 snapshot_payload 依赖 niquests Cookies.keys() 产出纯字符串键；
# niquests 更新版本会产出 Cookie 对象，QQ 扫码链路（ETK）据此抛 KeyError 使扫码全程失败。
# 这里做运行时 monkeypatch：不改库文件、不重建容器，插件加载即生效。
def _patch_qqmusic_snapshot() -> None:
    try:
        import qqmusic_api.core.response as _resp_mod

        original = _resp_mod.snapshot_payload
        if getattr(original, "_ace_cookie_safe", False):
            return

        def _cookie_safe_snapshot(response):
            try:
                return original(response)
            except Exception:
                cookies: dict[str, str] = {}
                try:
                    for c in response.cookies.jar:
                        nm = getattr(c, "name", None)
                        val = getattr(c, "value", None)
                        if nm and val is not None:
                            cookies[nm] = val
                except Exception:
                    try:
                        for k, v in dict(response.cookies).items():
                            nm = k if isinstance(k, str) else (getattr(k, "name", None) or "")
                            val = getattr(v, "value", None) if not isinstance(k, str) else v
                            if nm:
                                cookies[nm] = str(val)
                    except Exception:
                        cookies = {}
                return _resp_mod.RawPayload(
                    status_code=getattr(response, "status_code", None),
                    url=getattr(response, "url", None),
                    headers=getattr(response, "headers", {}) or {},
                    cookies=cookies,
                    content=getattr(response, "content", b"") or b"",
                    text=getattr(response, "text", "") or "",
                )

        _cookie_safe_snapshot._ace_cookie_safe = True
        _resp_mod.snapshot_payload = _cookie_safe_snapshot
        # 关键：executor/engine 在模块导入时按引用绑定了 snapshot_payload，
        # 必须把这些模块里的绑定也一并替换，否则补丁被旁路。
        import importlib

        patched_mods = []
        for mod_name in ("qqmusic_api.core.response", "qqmusic_api.core.executor",
                         "qqmusic_api.core.engine", "qqmusic_api.core.client",
                         "qqmusic_api.modules._base"):
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            if hasattr(mod, "snapshot_payload"):
                setattr(mod, "snapshot_payload", _cookie_safe_snapshot)
                patched_mods.append(mod_name.split(".")[-1])

        # 更底层兜底：niquests 的 Cookies 容器在新版本里对"无 domain/path 的 Cookie"
        # （QQ 扫码链路的 ETK 等）按名取值会抛 KeyError。给 __getitem__/get
        # 增加 jar 扫描兜底，无论哪一层引用都拿到值。
        try:
            import niquests as _nq
        except Exception:
            _nq = None
        if _nq is not None:
            logger.info(f"[ace_singing] niquests 版本: {getattr(_nq, '__version__', '?')}")
            from niquests import cookies as _nck
            _CookiesCls = _nck.Cookies
            if not getattr(_CookiesCls, "_ace_key_safe", False):
                _orig_getitem = _CookiesCls.__getitem__
                _orig_get = _CookiesCls.get

                def _ace_getitem(self, name, *a, **kw):
                    nm = name if isinstance(name, str) else (getattr(name, "name", None) or name)
                    try:
                        return _orig_getitem(self, nm, *a, **kw)
                    except KeyError:
                        try:
                            for c in getattr(self, "jar", ()) or ():
                                if getattr(c, "name", None) == nm and getattr(c, "value", None) is not None:
                                    return c.value
                        except Exception:
                            pass
                        raise

                def _ace_get(self, name, default=None, domain=None, path=None):
                    try:
                        return _orig_get(self, name, default, domain, path)
                    except Exception:
                        try:
                            for c in getattr(self, "jar", ()) or ():
                                if getattr(c, "name", None) == name and getattr(c, "value", None) is not None:
                                    return c.value
                        except Exception:
                            pass
                        return default

                _CookiesCls.__getitem__ = _ace_getitem
                _CookiesCls.get = _ace_get
                _CookiesCls._ace_key_safe = True
                logger.info("[ace_singing] 已应用 niquests Cookies jar 兜底补丁")
        logger.warning(
            f"[ace_singing] qqmusic_api Cookie 补丁生效范围: response={_, resp_mod_ok} modules={patched_mods} niquests=done"
            if False else
            f"[ace_singing] qqmusic_api Cookie 补丁生效范围: modules={patched_mods} + niquests getitem/get 兜底"
        )
    except Exception as e:
        logger.warning(f"[ace_singing] qqmusic_api Cookie 补丁未生效（库未安装或接口变动）: {e}")
_patch_qqmusic_snapshot()

# ================= 拟人化文案与歌名清洗 =================
_SONG_PREFIX_RE = re.compile(r"^\s*(?:点唱(?:一首)?|唱一首|唱(?:首歌)?|来一首|翻唱|点一首|听一首|来一(?:首歌|段))\s*")

def _clean_song_name(raw: str) -> str:
    """把 '点唱 探故知'、'唱一首 哈哈哈' 之类输入剥成纯歌名。"""
    name = str(raw or "").strip()
    prev = None
    while prev != name:
        prev = name
        name = _SONG_PREFIX_RE.sub("", name).strip()
        name = name.replace("完整版", "").replace("完整", "").strip()
    return name

_COVER_ACK_VARIANTS = [
    "稍等哦，我去把《{song}》录一段给你们听~",
    "好呀！《{song}》马上安排，唱好了丢语音~",
    "让我翻翻伴奏…《{song}》马上就来~",
    "这就来！《{song}》唱好了发上来，你们坐稳~",
    "呜哇这首我熟！等我几分钟，唱段《{song}》给你们~",
    "等我来唱个《{song}》，唱好了别夸我(๑•̀ㅂ•́)و✧",
]

_COVER_FAIL_VARIANTS = [
    "呜呜…《{song}》这首歌的音源全都抽风了，我暂时拿不到，先欠着这首好不好？",
    "《{song}》的源真是一个都爬不出来…要不换个歌？这首的债我记下了~",
    "《{song}》下载全失败了，等我缓一缓再试，或者你点首别的我马上唱！",
]

try:
    from qqmusic_api import Client as QMClient
except Exception as e:
    logger.warning(f"[ace_singing] qqmusic_api.Client 导入失败: {e}")
    QMClient = None

try:
    from qqmusic_api import Credential
except Exception:
    try:
        from qqmusic_api.models.credential import Credential
    except Exception:
        Credential = None

try:
    from qqmusic_api.models.song import SongFileInfo, SongFileType
except Exception:
    try:
        from qqmusic_api.modules.song import SongFileType
    except Exception:
        SongFileType = None
    SongFileInfo = None

if SongFileInfo is None:
    from typing import NamedTuple

    class SongFileInfo(NamedTuple):
        mid: str
        file_type: object = None
        song_type: int = 0
        media_mid: str = ""

try:
    from gradio_client import Client as GrClient, handle_file
    GRADIO_CLIENT_OK = True
except Exception:
    GRADIO_CLIENT_OK = False
    GrClient = None
    handle_file = None


def _make_song_file_info(mid: str, file_type=None, song_type: int = 0, media_mid: str = ""):
    if SongFileInfo is not None:
        try:
            return SongFileInfo(mid=mid, file_type=file_type, song_type=song_type, media_mid=media_mid or mid)
        except Exception:
            pass
    from types import SimpleNamespace
    return SimpleNamespace(mid=mid, file_type=file_type, song_type=song_type, media_mid=media_mid or mid)


def _qr_login_type():
    try:
        from qqmusic_api.models.login import QRLoginType
        return QRLoginType.QQ
    except Exception:
        try:
            from qqmusic_api.login import QRLoginType
            return QRLoginType.QQ
        except Exception:
            return 1


# ==================== QMCv2（QQ 音乐加密流）本地解密器 ====================
# 算法逐字节核对自 unlock-music 生态权威实现：
#   ekey 派生  : lib_um_crypto_rust/um_crypto/qmc/src/ekey.rs（v1/v2，TeX VC 官方向量对拍通过）
#   tc_tea CBC : jixunmoe/tc_tea_rust src/cbc.rs + ecb_impl.rs（16 轮 + 转置 CBC，官方 24B 向量对拍通过）
#   MapCipher  : unlock-music qmc_cipher.ts（(off² + 71214) % n + rotate((idx&7+4)%8)）
#   RC4 变体   : unlock-music cli algo/qmc/rc4.go（首段 0x80 + 5120 字节分段 skipLen RC4）
#   ekey 流程  : ownlight6/qmc-decoder TECHNICAL_DETAILS（GetEVkey API 直发 ekey 的分层回退）

_QMC_EKEY_V2_PREFIX = b"UVFNdXNpYyBFbmNWMixLZXk6"  # = base64("QQMusic EncV2,Key:")
_QMC_EKEY_V2_KEY1 = bytes((
    0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
    0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28,
))
_QMC_EKEY_V2_KEY2 = bytes((
    0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
    0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54,
))
# 解密结果音频魔数（OggS/mflac→fLaC/MP3 帧头）
_QMC_AUDIO_MAGICS = (b"OggS", b"fLaC", b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xfa", b"\xff\xf2")
_B64_RE = re.compile(r"[A-Za-z0-9+/=\r\n_\-]")


def _qmc_b64d(data: bytes) -> bytes:
    """宽松 base64 解码（容忍 url-safe 字符与缺失 '='）。"""
    d = bytes(data).strip()
    d = d.replace(b"-", b"+").replace(b"_", b"/")
    d = b"".join(d.split())
    d += b"=" * (-len(d) % 4)
    return base64.b64decode(d)


def _qmc_tea_ecb_decrypt(y: int, z: int, k0: int, k1: int, k2: int, k3: int) -> tuple:
    """16 轮 TEA ECB 解密（腾讯魔改：16 轮而非标准 32/64 轮）。"""
    DELTA = 0x9E3779B9
    MASK = 0xFFFFFFFF
    s = (DELTA * 16) & MASK
    for _ in range(16):
        t = ((y << 4) & MASK) + k2
        u = (y >> 5) + k3
        mid = (s + y) & MASK
        z = (z - ((t ^ mid ^ u) & MASK)) & MASK
        t = ((z << 4) & MASK) + k0
        u = (z >> 5) + k1
        mid = (s + z) & MASK
        y = (y - ((t ^ mid ^ u) & MASK)) & MASK
        s = (s - DELTA) & MASK
    return y, z


def _qmc_tea_decrypt(data: bytes, key: bytes) -> bytes:
    """tc_tea CBC 解密帧（PadLen|Padding|Salt(2)|Body|Zero(7)）。"""
    if len(key) != 16 or len(data) < 16 or len(data) % 8:
        raise ValueError(f"tc_tea 数据/密钥尺寸不合法 (data={len(data)})")
    k = [int.from_bytes(key[i:i + 4], "big") for i in (0, 4, 8, 12)]
    out = bytearray()
    iv1 = 0
    iv2 = 0
    for i in range(0, len(data), 8):
        block = int.from_bytes(data[i:i + 8], "big")
        result = block ^ iv2
        y, z = _qmc_tea_ecb_decrypt(result >> 32, result & 0xFFFFFFFF, *k)
        next_iv2 = (y << 32) | z
        plain_block = next_iv2 ^ iv1
        iv2 = next_iv2
        iv1 = block
        out += plain_block.to_bytes(8, "big")
    pad = out[0] & 7
    start = 1 + pad + 2
    end = len(out) - 7
    if any(out[end:]):
        raise ValueError("tc_tea 零填充校验失败（ekey 非包裹格式或密钥错误）")
    return bytes(out[start:end])


def _qmc_simple_key(n: int = 8, salt: int = 106) -> bytes:
    return bytes(int(abs(math.tan(salt + i * 0.1)) * 100.0) & 0xFF for i in range(n))


def _qmc_decrypt_v1(ekey_str: bytes) -> bytes:
    """EncV1: base64 → 头 8 字节与 simpleKey 交织成 TEA key 解密正文。"""
    if len(ekey_str) < 12:
        raise ValueError("ekey[EncV1] 过短")
    raw = _qmc_b64d(ekey_str)
    if len(raw) < 16:
        raise ValueError("ekey[EncV1] base64 解码过短")
    header, cipher = raw[:8], raw[8:]
    sk = _qmc_simple_key()
    tea_key = bytes(b for pair in zip(sk, header) for b in pair)
    try:
        plain = _qmc_tea_decrypt(cipher, tea_key)
    except ValueError:
        # GetEVkey API 直发的"裸密钥"没有 EncV1 包裹：整段解码结果就是文件密钥
        return raw
    return header + plain


def _qmc_derive_file_key(ekey_str) -> bytes:
    """ekey 字符串 → 文件密钥字节。自动识别 EncV2 / EncV1 / 裸密钥。"""
    if isinstance(ekey_str, str):
        ekey_str = ekey_str.encode("utf-8", "ignore")
    ek = bytes(ekey_str).strip()
    if ek.startswith(_QMC_EKEY_V2_PREFIX):
        body = _qmc_b64d(ek[len(_QMC_EKEY_V2_PREFIX):])
        body = _qmc_tea_decrypt(body, _QMC_EKEY_V2_KEY1)
        body = _qmc_tea_decrypt(body, _QMC_EKEY_V2_KEY2)
        body = body.rstrip(b"\x00")
        return _qmc_decrypt_v1(body)
    return _qmc_decrypt_v1(ek)


class _QmcMapCipher:
    def __init__(self, key: bytes):
        if not key:
            raise ValueError("map cipher: 空密钥")
        self.key = bytes(key)
        self.n = len(key)

    @staticmethod
    def _rotl(v: int, bits: int) -> int:
        shift = (bits + 4) % 8
        return ((v << shift) | (v >> shift)) & 0xFF

    def decrypt(self, buf: bytearray, offset: int = 0) -> None:
        key = self.key
        n = self.n
        for i in range(len(buf)):
            off = offset + i
            if off > 0x7FFF:
                off %= 0x7FFF
            idx = (off * off + 71214) % n
            buf[i] ^= self._rotl(key[idx], idx & 7)


class _QmcRc4Cipher:
    def __init__(self, key: bytes):
        if not key:
            raise ValueError("rc4 cipher: 空密钥")
        self.key = bytes(key)
        n = self.n = len(key)
        box = [i & 0xFF for i in range(n)]
        j = 0
        for i in range(n):
            j = (j + box[i] + key[i % n]) % n
            box[i], box[j] = box[j], box[i]
        self.box = bytes(box)
        h = 1
        for v in self.key:
            if v == 0:
                continue
            nxt = (h * v) & 0xFFFFFFFF
            if nxt == 0 or nxt <= h:
                break
            h = nxt
        self.hash = h

    def decrypt(self, buf: bytearray, offset: int = 0) -> None:
        data = bytes(buf)
        pos = 0
        out = bytearray(len(data))
        if offset < 128:
            size = min(len(data), 128 - offset)
            if size > 0:
                out[pos:pos + size] = self._first_segment(data[pos:pos + size], offset + pos)
                pos += size
        if pos < len(data) and (offset + pos) % 5120 != 0:
            size = min(len(data) - pos, 5120 - (offset + pos) % 5120)
            out[pos:pos + size] = self._segment(data[pos:pos + size], offset + pos)
            pos += size
        while len(data) - pos > 5120:
            out[pos:pos + 5120] = self._segment(data[pos:pos + 5120], offset + pos)
            pos += 5120
        if pos < len(data):
            out[pos:] = self._segment(data[pos:], offset + pos)
        buf[:] = bytes(out)

    def _first_segment(self, buf: bytes, offset: int) -> bytes:
        key = self.key
        n = self.n
        out = bytearray(buf)
        for i in range(len(out)):
            idx1 = offset + i
            denom = (idx1 + 1) * key[idx1 % n]
            if denom == 0:
                continue
            try:
                idx2 = int(float(self.hash) / float(denom) * 100.0)
            except ZeroDivisionError:
                continue
            out[i] ^= key[idx2 % n]
        return bytes(out)

    def _segment(self, buf: bytes, offset: int) -> bytes:
        key = self.key
        n = self.n
        box = bytearray(self.box)
        segment_id = (offset // 5120) & 0x1FF
        if n <= segment_id:
            return bytes(buf)
        denom = (offset // 5120 + 1) * key[segment_id]
        if denom == 0:
            return bytes(buf)
        idx2 = int(float(self.hash) / float(denom) * 100.0)
        skip_len = (idx2 & 0x1FF) + (offset % 5120)
        out = bytearray(buf)
        j = 0
        k = 0
        for i in range(-skip_len, len(out)):
            j = (j + 1) % n
            k = (box[j] + k) % n
            box[j], box[k] = box[k], box[j]
            if i >= 0:
                out[i] ^= box[(box[j] + box[k]) % n]
        return bytes(out)


def _qmc_tail_parse(data: bytes) -> tuple:
    """识别 Tauri/qmc-decoder 生态的三种尾部：musicex / QTag / V1 内嵌 ekey。
    返回 (audio_len, 内嵌ekey或None, 尾部描述)。"""
    length = len(data)
    if length > 200 and data[-8:] == b"musicex\x00":
        # musicex footer 总长 0xC0=192，ekey 不内嵌（服务器已提供）
        return length - 192, None, "musicex"
    if length > 1024 and data[-4:] == b"QTag":
        meta_len = int.from_bytes(data[-8:-4], "big")
        if 0 < meta_len < length - 100:
            meta = data[length - 8 - meta_len:length - 8]
            first = meta.split(b",", 1)[0] if meta else b""
            first = first.strip()
            if first and _B64_RE.match(first.decode("latin-1", "ignore")[0]):
                emb = first.decode("latin-1", "ignore")
                return length - 8 - meta_len, emb, "QTag"
    if length > 1024:
        v = int.from_bytes(data[-4:], "little")
        if 0 < v <= 0xFFFF:
            audio = length - 4 - v
            if audio > 0:
                emb_raw = data[audio:length - 4]
                try:
                    emb_txt = emb_raw.decode("ascii")
                    if emb_txt and all(_B64_RE.match(ch) for ch in emb_txt[:64]):
                        return audio, emb_txt, f"V1(内嵌{v}B)"
                except UnicodeDecodeError:
                    pass
    return length, None, "无尾部"


def _qmc_unlock_data(data: bytes, ekey_str) -> tuple:
    """QMCv2 加密字节流 → 本地解密。返回 (明文音频bytes|None, 说明)。
    ekey_str：服务器 ekey（UrlinfoItem.ekey）；若文件尾部有内嵌 ekey 则优先内嵌。"""
    try:
        data = bytes(data)
        if isinstance(ekey_str, bytes):
            ekey_str = ekey_str.decode("utf-8", "ignore")
        else:
            ekey_str = str(ekey_str or "").strip()
        if not data:
            return None, "空数据"
        audio_len, embedded, tail_desc = _qmc_tail_parse(data)
        reason = f"尾部={tail_desc}"
        attempts = []
        if embedded:
            attempts.append((f"内嵌ekey({tail_desc})", audio_len, embedded))
        attempts.append((f"服务器ekey({tail_desc})", audio_len, ekey_str))
        if audio_len != len(data):
            attempts.append((f"服务器ekey(全量)", len(data), ekey_str))
        last_err = reason
        for label, alen, kek in attempts:
            try:
                key = _qmc_derive_file_key(kek)
            except Exception as ke:
                last_err = f"{label} 派生失败: {ke}"
                continue
            n = len(key)
            if n == 0:
                last_err = f"{label} 派生后密钥长度为 0"
                continue
            cipher_name = "RC4" if n > 300 else "Map"
            cipher = _QmcRc4Cipher(key) if n > 300 else _QmcMapCipher(key)
            audio = bytearray(data[:alen])
            cipher.decrypt(audio, 0)
            audio = bytes(audio)
            magic = audio[:4]
            if magic[:3] == b"ID3" or magic[:4] in _QMC_AUDIO_MAGICS or magic[:2] in _QMC_AUDIO_MAGICS:
                return audio, f"{label} {cipher_name}(key_len={n}) 音频头={magic[:4].hex()}"
            last_err = (
                f"{label} {cipher_name}(key_len={n}) 解密后无音频魔数 "
                f"(头部: {audio[:16].hex()})"
            )
        return None, last_err
    except Exception as e:
        return None, f"QMCv2 解密异常: {e}"


@register(
    "astrbot_plugin_ace_singing",
    "Wanch",
    "专注原声翻唱：QQ 音乐找歌 + Seed-VC V1 (44.1kHz) 神经歌声转换，支持人格专属翻唱音色映射与智能乐句吸附截取",
    "1.9.3",
)
class AceSingingPlugin(Star):
    DEFAULT_VOICE_PROFILE = "112"

    def _cfg_int(self, key, default):
        try:
            return int(self.cfg.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key, default):
        try:
            return float(self.cfg.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key, default=""):
        v = self.cfg.get(key, default)
        if v is None:
            v = default
        return str(v).strip() or default

    def _cfg_bool(self, key, default=False):
        v = self.cfg.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "on", "yes", "是", "开")

    def _apply_config(self, config=None) -> bool:
        """全量读取并热应用配置，返回配置是否有变动"""
        if config is not None:
            self.cfg = config
        before = (
            getattr(self, "cover_provider_id", ""),
            getattr(self, "svc_voice_profile", ""),
            getattr(self, "svc_voice_path", ""),
            getattr(self, "svc_segment_sec", 0),
            getattr(self, "svc_n_step", 0),
            getattr(self, "svc_cfg", 0.0),
            getattr(self, "svc_seed", 0),
            getattr(self, "svc_acapella", False),
            getattr(self, "svc_prompt_vocal_sep", False),
            getattr(self, "timeout", 0),
            json.dumps(getattr(self, "_persona_cover_map", {}), ensure_ascii=False, sort_keys=True),
        )

        self.cover_provider_id = self._cfg_str("cover_provider", "music")
        self.svc_voice_profile = self._cfg_str("svc_voice_profile", self.DEFAULT_VOICE_PROFILE)
        self.svc_voice_path = self._cfg_str("svc_voice_path", "")
        self.svc_segment_sec = self._cfg_int("svc_segment_sec", 36)
        self.svc_n_step = self._cfg_int("svc_n_step", 50)
        self.svc_cfg = self._cfg_float("svc_cfg", 0.95)
        self.svc_seed = self._cfg_int("svc_seed", -1)
        self.svc_timbre_eq = self._cfg_str("svc_timbre_eq", "")
        self.svc_acapella = self._cfg_bool("svc_acapella", False)
        self.svc_prompt_vocal_sep = self._cfg_bool("svc_prompt_vocal_sep", False)
        self.timeout = self._cfg_int("timeout_sec", 600)

        raw_map = self.cfg.get("persona_cover_map", {}) or {}
        self._persona_cover_map = self._parse_persona_cover_map(raw_map)

        after = (
            self.cover_provider_id,
            self.svc_voice_profile,
            self.svc_voice_path,
            self.svc_segment_sec,
            self.svc_n_step,
            self.svc_cfg,
            self.svc_seed,
            self.svc_acapella,
            self.svc_prompt_vocal_sep,
            self.timeout,
            json.dumps(self._persona_cover_map, ensure_ascii=False, sort_keys=True),
        )
        return before != after

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self._persona_cover_map = {}
        self.svc_voice_profile = self.DEFAULT_VOICE_PROFILE
        self._apply_config(config)

        self._login_task: asyncio.Task | None = None
        self._pending_retry: dict | None = None
        self._client = None
        self._last_url_err: str = ""

        # 翻唱任务可观测性
        self._active_covers = {}      # task_key -> 起始时间戳
        self._recent_covers = {}      # task_key -> 上次成功时间戳（5min 去重）
        self._active_jobs = {}        # task_key -> gradio_client Job（用于取消）
        self._cancelled = set()       # 已被用户取消的 task_key
        self._user_cover_count = {}   # umo -> 翻唱次数统计

    @filter.on_llm_request()
    async def _refresh_runtime_cfg(self, event, req):
        """面板修改配置后，无需重启容器即可自动热加载生效"""
        try:
            if self._apply_config():
                logger.info(f"[ace_singing] 配置热更新: 兜底音色={self.svc_voice_profile} | 步数={self.svc_n_step} | cfg={self.svc_cfg}")
        except Exception as e:
            logger.warning(f"[ace_singing] 配置热更新失败: {e}")

    @staticmethod
    def _parse_persona_cover_map(raw_map) -> dict:
        """
        支持：
        1. {"猫娘": "galgame2"}                     → 字符串即翻唱音色
        2. {"猫娘": {"cover_voice": "galgame2"}}     → 结构化字典
        3. [{"persona": "猫娘", "cover_voice": "galgame2"}] → 列表
        4. 多行文本 "猫娘: galgame2"
        """
        result = {}
        if not raw_map:
            return result

        if isinstance(raw_map, str):
            t = raw_map.strip()
            if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
                try:
                    raw_map = json.loads(t)
                except Exception:
                    pass

        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                k = str(k).strip()
                if not k:
                    continue
                if isinstance(v, dict):
                    cv = (v.get("cover_voice") or v.get("cover") or v.get("voice") or "").strip()
                    if cv:
                        result[k] = cv
                elif isinstance(v, str):
                    parts = [p.strip() for p in v.split(":") if p.strip()]
                    if parts:
                        result[k] = parts[0]
            return result

        if isinstance(raw_map, list):
            for item in raw_map:
                if isinstance(item, dict):
                    p = str(item.get("persona") or item.get("name") or "").strip()
                    cv = (item.get("cover_voice") or item.get("cover") or item.get("voice") or "").strip()
                    if p and cv:
                        result[p] = cv
            return result

        if isinstance(raw_map, str):
            for line in raw_map.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for delim in (":", "：", "="):
                    if delim in line:
                        parts = line.split(delim, 1)
                        p = parts[0].strip()
                        rest = parts[1].strip()
                        cv = rest.split(":")[0].strip() if ":" in rest else rest
                        if p and cv:
                            result[p] = cv
                        break
            return result

        return result

    def _get_cover_api_base(self) -> str:
        """从用户下拉选择的翻唱提供商中动态解析 API Base，支持平滑兜底"""
        default_base = "http://127.0.0.1:6767"
        if not getattr(self, "cover_provider_id", ""):
            return default_base
        try:
            p = self.context.get_provider_by_id(self.cover_provider_id)
            if p:
                cfg = getattr(p, "provider_config", {}) or getattr(p, "config", {}) or {}
                base = cfg.get("api_base") or cfg.get("base_url") or getattr(p, "api_base", "") or getattr(p, "base_url", "")
                if base:
                    clean = str(base).rstrip("/")
                    if clean.endswith("/v1"):
                        clean = clean[:-3]
                    return clean
        except Exception as e:
            logger.warning(f"[ace_singing] 解析翻唱提供商异常: {e}")
        if str(self.cover_provider_id).startswith("http"):
            clean = str(self.cover_provider_id).rstrip("/")
            if clean.endswith("/v1"):
                clean = clean[:-3]
            return clean
        return default_base

    def _get_provider_default_voice(self, provider_id: str, fallback: str = "112") -> str:
        """从 AstrBot 模型提供商的配置中读取其设置的 voice（模型默认配置）"""
        if not provider_id:
            return fallback
        try:
            p = self.context.get_provider_by_id(provider_id)
            if p:
                cfg = getattr(p, "provider_config", {}) or getattr(p, "config", {}) or {}
                v = cfg.get("voice") or cfg.get("speaker") or cfg.get("openai-tts-voice") or getattr(p, "voice", "") or getattr(p, "speaker", "")
                if v and str(v).strip():
                    return str(v).strip()
        except Exception:
            pass
        return fallback

    # ================= 音色精度 =================

    async def _fetch_voice_profiles(self) -> list:
        """从翻唱网关 /v1/models 拉取真实音色 ID 列表并缓存，保证 model_dropdown 名称精确。"""
        now = time.time()
        cache = getattr(self, "_voice_profiles_cache", None)
        if cache and (now - cache[0]) < 300:
            return cache[1]
        profiles = []
        try:
            base = self._get_cover_api_base()
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as s:
                async with s.get(f"{base}/v1/models") as r:
                    if r.status == 200:
                        data = await r.json()
                        for m in (data.get("data") or []):
                            for v in (m.get("voices") or []):
                                name = str(v).strip()
                                if name:
                                    profiles.append(name)
        except Exception as e:
            logger.warning(f"[ace_singing] 拉取音色列表失败: {e}")
        self._voice_profiles_cache = (now, profiles)
        return profiles

    @staticmethod
    def _match_voice_profile(name: str, profiles: list) -> str:
        """把配置/映射中的音色名精确对齐到网关真实下拉项。"""
        name = (name or "").strip()
        if not name:
            return name
        if not profiles:
            return name
        for p in profiles:
            if p == name:
                return p
        low = name.lower()
        for p in profiles:
            if p.lower() == low:
                return p
        for p in profiles:
            pl = p.lower()
            if low and (low in pl or pl in low):
                return p
        return name

    def _lookup_persona_cover(self, pid: str) -> str:
        """人格映射检索：精确 -> 忽略大小写 -> 双向子串。"""
        pid = (pid or "").strip()
        if not pid or not self._persona_cover_map:
            return ""
        entries = list(self._persona_cover_map.items())
        for k, v in entries:
            if str(k).strip() == pid:
                return str(v).strip()
        pid_l = pid.lower()
        for k, v in entries:
            if str(k).strip().lower() == pid_l:
                return str(v).strip()
        for k, v in entries:
            kl = str(k).strip().lower()
            if kl and (kl in pid_l or pid_l in kl):
                return str(v).strip()
        return ""

    async def _resolve_persona_id(self, event_or_umo) -> str:
        """三级优先级精确解析当前会话人格：会话强制 > 会话绑定 > agent 默认"""
        if isinstance(event_or_umo, str):
            umo = event_or_umo
            platform_name = ""
            event = None
        else:
            umo = getattr(event_or_umo, "unified_msg_origin", "")
            platform_name = event_or_umo.get_platform_name() if hasattr(event_or_umo, "get_platform_name") else ""
            event = event_or_umo

        if not umo:
            return ""

        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm and hasattr(pm, "resolve_selected_persona"):
                pid, persona, _force_id, _webchat = await pm.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=None,
                    platform_name=platform_name,
                )
                if persona and isinstance(persona, dict):
                    return str(persona.get("name") or pid or "").strip()
                if pid:
                    return str(pid).strip()
        except Exception as e:
            logger.warning(f"[ace_singing] resolve_selected_persona 异常: {e}")

        # 兜底从 event 取
        if event:
            return str(getattr(event, "persona_id", "") or "").strip()
        return ""

    async def _resolve_cover_voice(self, event_or_umo) -> str:
        """
        根据当前会话绑定的人格解析翻唱音色：
        1. 优先使用映射表 (persona_cover_map) 中为该人格配置的专属音色；
        2. 若无映射，优先使用插件面板显式设置的兜底音色 (svc_voice_profile)；
        3. 若插件面板未设置，使用模型提供商配置的 voice；
        4. 终极兜底使用 DEFAULT_VOICE_PROFILE ("112")。
        """
        default = (self.svc_voice_profile or "").strip() or self._get_provider_default_voice(
            self.cover_provider_id, self.DEFAULT_VOICE_PROFILE
        )
        if not getattr(self, "_persona_cover_map", None):
            return default

        pid = await self._resolve_persona_id(event_or_umo)
        if not pid:
            return default

        entry = self._lookup_persona_cover(pid)
        if entry:
            return entry
        return default

    async def _send(self, umo: str, components: list) -> bool:
        """统一发送：后台任务返回后依然可用。"""
        try:
            await self.context.send_message(umo, MessageChain(components))
            return True
        except Exception as e:
            logger.error(f"[ace_singing] 发送失败: {e}")
            return False

    # ================= QQ 音乐 =================

    def _load_cred(self) -> dict | None:
        try:
            return json.load(open(CRED_PATH, encoding="utf-8"))
        except Exception:
            return None

    def _save_cred(self, cred_dict: dict):
        json.dump(cred_dict, open(CRED_PATH, "w", encoding="utf-8"), ensure_ascii=False)

    async def _qm_client(self, force_new: bool = False):
        if force_new and self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
        if self._client is not None:
            return self._client
        cred_data = self._load_cred()
        kwargs = {}
        if cred_data:
            try:
                if Credential and callable(Credential):
                    kwargs["credential"] = Credential(
                        musicid=cred_data.get("musicid"),
                        musickey=cred_data.get("musickey"),
                        str_musicid=cred_data.get("str_musicid") or str(cred_data.get("musicid", "")),
                        refresh_token=cred_data.get("refresh_token") or "",
                    )
            except Exception as e:
                logger.warning(f"[ace_singing] 凭据恢复失败: {e}")

        if QMClient is None:
            raise RuntimeError("系统未检测到可用 qqmusic_api 库或 Client 导入异常")

        self._client = QMClient(**kwargs)
        await self._client.__aenter__()
        return self._client

    async def _qq_search(self, c, keyword: str, num: int = 5):
        """
        QQ 音乐检索：优先通过 SDK Client 异步检索，若版本结构差异则平滑回退至官方 HTTP 接口。
        """
        # 1. 尝试 Client 内部 search API
        try:
            if c and hasattr(c, "search"):
                s_obj = getattr(c, "search")
                if hasattr(s_obj, "search_by_type"):
                    res = await s_obj.search_by_type(keyword=keyword, num=num)
                    songs = getattr(res, "song", None)
                    if songs is not None:
                        out = []
                        for s in songs:
                            mid = getattr(s, "mid", "") or getattr(s, "songmid", "")
                            name = getattr(s, "name", "") or getattr(s, "title", "") or getattr(s, "songname", "")
                            singers = getattr(s, "singer", []) or []
                            singer_list = []
                            for sg in singers:
                                if isinstance(sg, dict):
                                    singer_list.append(sg)
                                else:
                                    singer_list.append({"name": getattr(sg, "name", "")})
                            if mid:
                                out.append({"mid": mid, "name": name, "singer": singer_list})
                        if out:
                            return out
                    if isinstance(res, dict):
                        data = res.get("data") or res
                        sl = data.get("list") or (data.get("song") or {}).get("list") or []
                        if sl:
                            return sl
                elif callable(s_obj):
                    res = await s_obj(keyword, num=num)
                    if isinstance(res, dict):
                        data = res.get("data") or res
                        sl = data.get("list") or (data.get("song") or {}).get("list") or []
                        if sl:
                            return sl
        except Exception as e:
            logger.warning(f"[ace_singing] Client search 调用异常: {e}")

        # 2. 尝试从 qqmusic_api.modules.search 导入
        try:
            import qqmusic_api.modules.search as s_mod
            func = getattr(s_mod, "search", None) or getattr(s_mod, "search_by_type", None)
            if func:
                res = await func(keyword, num=num, page=1, client=c)
                if isinstance(res, dict):
                    data = res.get("data") or res
                    sl = data.get("list") or (data.get("song") or {}).get("list") or []
                    if sl:
                        return sl
        except Exception as e:
            logger.warning(f"[ace_singing] modules.search 调用异常: {e}")

        # 3. 终极官方 HTTP 检索兜底（无需凭据，稳定高可用）
        try:
            loop = asyncio.get_running_loop()
            def _http_search():
                import urllib.parse
                import urllib.request
                import json
                enc = urllib.parse.quote(keyword)
                url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n={num}&w={enc}&format=json"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://y.qq.com/",
                })
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                raw_list = (data.get("data") or {}).get("song", {}).get("list", [])
                out = []
                for s in raw_list:
                    mid = s.get("songmid") or s.get("mid")
                    name = s.get("songname") or s.get("name")
                    singers = s.get("singer") or []
                    if mid:
                        out.append({"mid": mid, "name": name, "singer": singers})
                return out

            out_songs = await loop.run_in_executor(None, _http_search)
            if out_songs:
                return out_songs
        except Exception as e:
            logger.error(f"[ace_singing] HTTP 兜底搜索失败: {e}")

        return []

    async def _qq_lyric(self, c, mid: str):
        try:
            if c and hasattr(c, "lyric"):
                l_obj = getattr(c, "lyric")
                if callable(l_obj):
                    l_obj = l_obj()
                fn = getattr(l_obj, "get_lyrics", None) or getattr(l_obj, "get_lyric", None)
                if fn:
                    return await fn(mid)
            import qqmusic_api.modules.lyric as lyric_mod
            fn = getattr(lyric_mod, "get_lyrics", None) or getattr(lyric_mod, "get_lyric", None)
            if fn:
                return await fn(mid, client=c)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_lrc_text(res) -> str | None:
        """从 qqmusic_api 歌词返回结构中稳健地抽出 LRC 文本。"""
        if not res:
            return None
        candidates = []
        if isinstance(res, str):
            candidates.append(res)
        elif isinstance(res, dict):
            for key in ("lyric", "lrc", "base64", "data"):
                v = res.get(key)
                if isinstance(v, str):
                    candidates.append(v)
                elif isinstance(v, dict):
                    for k2 in ("lyric", "lrc", "base64"):
                        v2 = v.get(k2)
                        if isinstance(v2, str):
                            candidates.append(v2)
        else:
            for attr in ("lyric", "lrc", "base64"):
                v = getattr(res, attr, None)
                if isinstance(v, str):
                    candidates.append(v)
            data = getattr(res, "data", None)
            if isinstance(data, dict):
                v = data.get("lyric") or data.get("lrc") or data.get("base64")
                if isinstance(v, str):
                    candidates.append(v)

        import base64
        for c in candidates:
            c = (c or "").strip()
            if not c:
                continue
            if c.startswith("["):
                return c
            # QQ 音乐常把 LRC 以 base64 返回
            if len(c) > 40:
                try:
                    dec = base64.b64decode(c).decode("utf-8", "ignore")
                    if dec.lstrip().startswith("["):
                        return dec
                except Exception:
                    pass
        return None

    @staticmethod
    def _parse_lrc(lrc_text: str) -> list:
        """解析 LRC，返回按时间排序的 [(秒, 歌词)]。"""
        lines = []
        for raw in (lrc_text or "").splitlines():
            tags = re.findall(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]", raw)
            text = re.sub(r"\[[^\]]*\]", "", raw).strip()
            if not text or not tags:
                continue
            for m in tags:
                mins, secs = int(m[0]), int(m[1])
                frac = (m[2] or "0").ljust(3, "0")[:3]
                t = mins * 60 + secs + int(frac) / 1000.0
                lines.append((t, text))
        lines.sort(key=lambda x: x[0])
        return lines

    @staticmethod
    def _is_singable(text: str) -> bool:
        """过滤前奏/间奏/尾奏/纯音乐等无人声歌词行。"""
        t = (text or "").strip()
        if not t:
            return False
        low = t.lower()
        if low in ("♪", "♪♪", "music", "instrumental", "纯音乐", "间奏", "前奏", "尾奏"):
            return False
        for marker in ("间奏", "前奏", "尾奏", "纯音乐", "instrumental", "solo"):
            if marker in low:
                return False
        return True

    @staticmethod
    def _extract_url_from_res(res, mid: str) -> tuple:
        """返回 (relative_purl, filename)。filename 是 QQ 自算的"真实文件名"。"""
        if not res:
            return "", ""

        _SONG_URL_BASE = "https://isure.stream.qqmusic.qq.com/"

        # 1. qqmusic-api-python 0.8.2 GetSongUrlsResponse:
        # res.data -> list[UrlinfoItem(mid='...', filename='...', purl='...')]
        # 返回"相对 purl"（含查询串），域名由 GetCdnDispatch 动态提供，不再写死。
        from urllib.parse import urlparse as _urlparse

        data_list = getattr(res, "data", None)
        if data_list is None and isinstance(res, dict):
            data_list = res.get("data")

        _RESULT_MEANING = {
            104003: "无权限（非VIP/风控/账号权限不足）",
            104004: "VKey 获取失败",
            104013: "登录设备数量超限",
        }

        matched_filename = ""
        matched_ekey = ""
        if isinstance(data_list, list):
            for item in data_list:
                item_mid = getattr(item, "mid", None) or (item.get("mid") if isinstance(item, dict) else None)
                result_val = getattr(item, "result", None)
                if result_val is None and isinstance(item, dict):
                    result_val = item.get("result")
                filename_val = getattr(item, "filename", None) or (item.get("filename") if isinstance(item, dict) else "")
                ekey_val = getattr(item, "ekey", None) or (item.get("ekey") if isinstance(item, dict) else "")
                logger.info(
                    f"[ace_singing] UrlinfoItem mid={item_mid} result={result_val} filename={filename_val} ekey={'<有>' if ekey_val else '<无>'}"
                )
                if item_mid and str(item_mid) != str(mid):
                    continue
                if result_val not in (None, 0):
                    meaning = _RESULT_MEANING.get(int(result_val), "其他错误") if isinstance(result_val, int) else str(result_val)
                    logger.error(
                        f"[ace_singing] QQ 音源被拒（result={result_val} → {meaning}），"
                        "purl 即使返回也会在 CDN 侧 404"
                    )
                    continue
                if filename_val:
                    matched_filename = str(filename_val)
                if ekey_val:
                    matched_ekey = str(ekey_val)
                purl = getattr(item, "purl", None) or (item.get("purl") if isinstance(item, dict) else None)
                if purl and isinstance(purl, str) and purl.strip():
                    purl = purl.strip()
                    if purl.startswith("http://") or purl.startswith("https://"):
                        parsed = _urlparse(purl)
                        rel = parsed.path or "/"
                        if parsed.query:
                            rel += "?" + parsed.query
                        return rel, matched_filename, matched_ekey
                    return purl, matched_filename, matched_ekey

        # 2. 如果是字典映射 {mid: url} 或嵌套结构
        if isinstance(res, dict):
            u = res.get(mid) or res.get(str(mid))
            if isinstance(u, str) and u.startswith("http"):
                parsed = _urlparse(u)
                rel = parsed.path or "/"
                if parsed.query:
                    rel += "?" + parsed.query
                return rel, matched_filename, matched_ekey
            for v in res.values():
                if isinstance(v, str) and v.startswith("http"):
                    parsed = _urlparse(v)
                    rel = parsed.path or "/"
                    if parsed.query:
                        rel += "?" + parsed.query
                    return rel, matched_filename, matched_ekey
                if isinstance(v, dict):
                    inner_u = v.get("url") or v.get("purl")
                    if isinstance(inner_u, str) and inner_u.strip():
                        if inner_u.startswith("http"):
                            parsed = _urlparse(inner_u)
                            rel = parsed.path or "/"
                            if parsed.query:
                                rel += "?" + parsed.query
                            return rel, matched_filename, matched_ekey
                        return inner_u, matched_filename, matched_ekey

        # 3. 如果直接是字符串 URL
        if isinstance(res, str) and res.startswith("http"):
            parsed = _urlparse(res)
            rel = parsed.path or "/"
            if parsed.query:
                rel += "?" + parsed.query
            return rel, matched_filename, matched_ekey

        return "", ""

    async def _qq_url(self, c, mid: str, media_mid: str = "", downgrade: bool = False) -> str | None:
        """
        获取歌曲播放音频直链：
        适配 qqmusic-api-python 0.8.2 的 SongApi.get_song_urls(file_info=[SongFileInfo(...)])
        按 MP3_320 -> MP3_128 逐级降级重试。
        """
        self._last_url_err = ""
        media_mid = media_mid or mid

        song_api = getattr(c, "song", None)
        if callable(song_api):
            song_api = song_api()

        fn = getattr(song_api, "get_song_urls", None) if song_api else None
        if not fn:
            fn = getattr(c, "get_song_urls", None)

        if not fn:
            self._last_url_err = "Client 未暴露 get_song_urls 接口"
            logger.error(f"[ace_singing] {self._last_url_err}")
            return [], "", "", ""

        # 动态 CDN 前缀池（音乐官方用法）：GetCdnDispatch.sip + 相对 purl
        sips: list = []
        try:
            dispatch_fn = getattr(song_api, "get_cdn_dispatch", None) if song_api else None
            if dispatch_fn:
                dispatch = await dispatch_fn()
                sips = [str(s).strip() for s in (getattr(dispatch, "sip", []) or []) if str(s).strip()]
                if sips:
                    logger.info(f"[ace_singing] GetCdnDispatch 动态 CDN 前缀 {len(sips)} 个: {sips[:2]}")
        except Exception as dispatch_err:
            logger.warning(f"[ace_singing] 获取动态 CDN 前缀失败，退回固定域名: {dispatch_err}")

        # 降级尝试序列：320k -> 128k -> 默认
        qualities = []
        if SongFileType:
            if hasattr(SongFileType, "MP3_320"):
                qualities.append(SongFileType.MP3_320)
            if hasattr(SongFileType, "MP3_128"):
                qualities.append(SongFileType.MP3_128)
        qualities.append(None)
        if downgrade and len(qualities) > 1:
            # 320k 直链 404 时跳过最高档，从 MP3_128 开始取
            qualities = qualities[1:]

        # 普通通道失败后的加密通道（GetEVkey）：OGG_320(HQ) -> FLAC -> OGG_96 -> TRY(试听)
        enc_cls = None
        try:
            from qqmusic_api.modules.song import EncryptedSongFileType as enc_cls
        except Exception:
            try:
                from qqmusic_api.models.song import EncryptedSongFileType as enc_cls
            except Exception:
                enc_cls = None
        if enc_cls is not None and len(sips) > 0:
            for name in ("OGG_320", "FLAC", "OGG_96", "TRY"):
                t = getattr(enc_cls, name, None)
                if t is not None and t not in qualities:
                    qualities.append(t)
                    logger.info(f"[ace_singing] 追加加密品质 {name} ({getattr(t, 's', '?')}{getattr(t, 'e', '?')})")

        for ft in qualities:
            try:
                fi = _make_song_file_info(mid=mid, file_type=ft, song_type=0, media_mid=media_mid)
                kwargs = {}
                if ft is not None:
                    kwargs["file_type"] = ft
                res = await fn([fi], **kwargs)
                purl_rel, item_filename, item_ekey = self._extract_url_from_res(res, mid)
                if purl_rel:
                    q_name = getattr(ft, "name", str(ft) if ft else "默认")
                    logger.info(f"[ace_singing] 成功获取音源 purl (品质: {q_name}): {purl_rel[:70]}... filename={item_filename}")
                    return sips, purl_rel, item_filename, item_ekey
                else:
                    logger.warning(f"[ace_singing] get_song_urls 返回成功但 purl 为空 (品质: {ft})")
            except Exception as e:
                self._last_url_err = str(e)
                logger.warning(f"[ace_singing] get_song_urls 尝试品质 {ft} 失败: {e}")

        return [], "", "", ""

    def _audio_url_variants(self, purl: str, sips: list, filename: str = "") -> list:
        """按 GetCdnDispatch 动态域名优先,老 CDN 域兜底,展开候选 URL。"""
        purl = (purl or "").strip()
        if not purl:
            return []
        bases = []
        for s in (sips or []):
            s = str(s or "").strip()
            if s:
                if not s.startswith("http"):
                    s = "https://" + s
                if not s.endswith("/"):
                    s += "/"
                bases.append(s)
        bases += [
            "https://isure.stream.qqmusic.qq.com/",
            "https://dl.stream.qqmusic.qq.com/",
            "https://ws.stream.qqmusic.qq.com/",
        ]
        out = []
        query = ""
        if "?" in purl:
            purl_path, query = purl.split("?", 1)
            query = "?" + query
        else:
            purl_path = purl
        for b in bases:
            u = b + purl_path.lstrip("/") + query
            if u not in out:
                out.append(u)
        # QQ 自算的 filename 若与 purl 文件名不同，用它再生成一组候选
        fname = (filename or "").strip()
        if fname and fname not in purl_path:
            for b in bases:
                u = b + fname.lstrip("/") + query
                if u not in out:
                    out.append(u)
        return out

    _DOWNLOAD_HEADER_VARIANTS = [
        # (User-Agent, Referer, 额外头)
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
         "https://y.qq.com/", None),
        ("QQMusic/20.9.0.8 (iPhone; iOS 17.5.1; iPhone15,3)",
         "https://y.qq.com/m/", None),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 QQMusic/12.0.5.6",
         "https://y.qq.com/portal/player.html", {"Range": "bytes=0-"}),
        ("okhttp/4.12.0", None, None),
    ]

    async def _download(self, url: str, ua: str | None = None, referer: str | None = None, extra: dict | None = None) -> bytes:
        headers = {
            "User-Agent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": referer or "https://y.qq.com/",
        }
        if extra:
            headers.update(extra)
        # QQ 新版 CDN 对 y.qq.com 取流链路校验会话一致性：下载时带上登录 Cookie
        # （官网网页播放同样携带），能显著降低“vkey 合法但 404”的概率。
        try:
            cred = self._load_cred()
            if cred and cred.get("musickey"):
                headers["Cookie"] = (
                    f"uin={cred.get('musicid', '')}; qqmusic_uin={cred.get('musicid', '')}; "
                    f"qqmusic_key={cred.get('musickey')}; qm_keyst={cred.get('musickey')}"
                )
        except Exception:
            pass
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=headers)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=30) as resp:
                return resp.read()

        return await loop.run_in_executor(None, _fetch)

    def _dump_ekey(self, song_mid: str, quality: str, ekey: str):
        """把完整 ekey 落盘并全量打日志（解密器取证，防截断丢证）。"""
        try:
            path = os.path.join(TMP_DIR, "ekey_samples.txt")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} | mid={song_mid} | quality={quality} | len={len(ekey)} | ekey={ekey}\n")
            logger.info(f"[ace_singing] [QMC] 完整 ekey 已落盘: {path}")
        except Exception as e:
            logger.warning(f"[ace_singing] [QMC] ekey 落盘失败: {e}")
        logger.info(f"[ace_singing] [QMC] 完整 ekey({len(ekey)} 字符): {ekey}")

    async def _download_and_unlock(self, u: str, ekey: str, quality: str = "", song_mid: str = "", combo_idx: int = 0):
        """下载一个 URL；若带 ekey 则本地 QMCv2 解密（线程池，避免阻塞事件循环）。"""
        if not u:
            return None
        ua, ref, extra = self._DOWNLOAD_HEADER_VARIANTS[min(combo_idx, len(self._DOWNLOAD_HEADER_VARIANTS) - 1)]
        try:
            data = await self._download(u, ua=ua, referer=ref, extra=extra)
        except Exception as de:
            logger.warning(f"[ace_singing] 下载失败 ({de}) URL: {u} (UA={ua[:20]}...)")
            return None
        if not ekey:
            return data
        q_label = quality or "加密流"
        logger.warning(
            f"[ace_singing] 加密流探测成功 (品质: {q_label}): size={len(data)}B, ekey_len={len(ekey)}"
        )
        self._dump_ekey(song_mid, q_label, ekey)
        from functools import partial

        try:
            loop = asyncio.get_running_loop()
            dec, why = await loop.run_in_executor(None, partial(_qmc_unlock_data, data, ekey))
        except Exception as ue:
            dec, why = None, f"解密异常: {ue}"
        if dec:
            logger.info(f"[ace_singing] [QMC] QMCv2 本地解密成功 ({why})，本轮恢复 QQ 音源直用")
            return dec
        logger.warning(f"[ace_singing] [QMC] QMCv2 解密失败 ({why})，该品质转后备通道")
        return None

    _ENCRYPTED_QUALITY_ORDER = ("OGG_320", "FLAC", "OGG_96", "TRY")

    async def _qq_encrypted_probe(self, c, mid: str, media_mid: str = "", sips=None) -> bytes | None:
        """加密通道：music.vkey.GetEVkey 取 ekey + 加密流，QMCv2 本地解密恢复 QQ 音源。
        成功返回解密后的音频字节，失败返回 None（不阻塞用户，由备用源兜底）。"""
        media_mid = media_mid or mid
        song_api = getattr(c, "song", None)
        if callable(song_api):
            song_api = song_api()
        fn = getattr(song_api, "get_song_urls", None) if song_api else None
        if not fn:
            return None
        enc_cls = None
        try:
            from qqmusic_api.modules.song import EncryptedSongFileType as enc_cls
        except Exception:
            try:
                from qqmusic_api.models.song import EncryptedSongFileType as enc_cls
            except Exception:
                enc_cls = None
        if enc_cls is None:
            logger.warning("[ace_singing] [QMC] 库内无 EncryptedSongFileType，加密通道不可用")
            return None
        if not sips:
            try:
                dispatch_fn = getattr(song_api, "get_cdn_dispatch", None)
                if dispatch_fn:
                    dispatch = await dispatch_fn()
                    sips = [str(s).strip() for s in (getattr(dispatch, "sip", []) or []) if str(s).strip()]
            except Exception:
                sips = []
        logger.info(f"[ace_singing] [QMC] 加密通道探测开启 (品质序列: {' -> '.join(self._ENCRYPTED_QUALITY_ORDER)})")
        for name in self._ENCRYPTED_QUALITY_ORDER:
            ft = getattr(enc_cls, name, None)
            if ft is None:
                continue
            try:
                fi = _make_song_file_info(mid=mid, file_type=ft, song_type=0, media_mid=media_mid)
                res = await fn([fi], file_type=ft)
                purl_rel, item_filename, item_ekey = self._extract_url_from_res(res, mid)
                if not purl_rel:
                    logger.info(f"[ace_singing] [QMC] 加密品质 {name} 无有效 purl (result 详情见 UrlinfoItem 行)")
                    continue
                if not item_ekey:
                    logger.info(f"[ace_singing] [QMC] 加密品质 {name} 返回 purl 但无 ekey，无法解密 (filename={item_filename})")
                    continue
                logger.info(f"[ace_singing] [QMC] {name} 已取得加密链 + ekey，下载并本地解密...")
                urls = self._audio_url_variants(purl_rel, sips, item_filename or "")[:4]
                for combo_idx, u in enumerate(urls):
                    audio = await self._download_and_unlock(
                        u, item_ekey, quality=name, song_mid=mid, combo_idx=combo_idx
                    )
                    if audio:
                        return audio
                # 同一个加密文件换域名内容不变，解密失败重试 URL 无意义，直接换下一品质
            except Exception as e:
                logger.warning(f"[ace_singing] [QMC] 加密品质 {name} 出错: {e}")
        logger.info("[ace_singing] [QMC] 加密品质全部查完，本轮无可用 QQ 音源")
        return None

    async def _pop_login(self, umo: str, retry_info: dict | None = None):
        if self._login_task and not self._login_task.done():
            return
        self._pending_retry = retry_info
        self._login_task = asyncio.create_task(self._poll_login(umo))

    @staticmethod
    def _cred_to_dict(cred):
        if cred is None:
            return None
        if isinstance(cred, dict):
            return {
                "musicid": cred.get("musicid"),
                "musickey": cred.get("musickey"),
                "str_musicid": cred.get("str_musicid"),
                "refresh_token": cred.get("refresh_token"),
            }
        return {
            "musicid": getattr(cred, "musicid", None),
            "musickey": getattr(cred, "musickey", None),
            "str_musicid": getattr(cred, "str_musicid", None),
            "refresh_token": getattr(cred, "refresh_token", None),
        }

    def _static_cookies_dict(self, cookies_obj) -> dict:
        """把 niquests/requests 的 Cookie 容器快照成 name->value 映射（全形态兜底）。"""
        out: dict[str, str] = {}
        try:
            for c in cookies_obj.jar:
                nm = getattr(c, "name", None)
                val = getattr(c, "value", None)
                if nm and val is not None:
                    out[nm] = val
        except Exception:
            pass
        try:
            sc = ""
            raw_set_cookie = None
            for key, val in (cookies_obj.headers or {}).items():
                if key.lower() == "set-cookie":
                    raw_set_cookie = val
            for line in (raw_set_cookie or "").split("\n"):  # 多条 Set-Cookie 可能拼一起
                head = line.split(";")[0]
                if "=" in head:
                    nm, val = head.split("=", 1)
                    if nm.strip() and val.strip():
                        out.setdefault(nm.strip(), val.strip())
        except Exception:
            pass
        return out

    async def _qr_login_modern(self, umo: str):
        """自管理扫码登录（避开库内部对 ptlogin 域 Cookie 的快照解析）。

        1) 用库的 get_qrcode 取二维码（该响应不含 ETK，快照安全）；
        2) ptqrlogin 轮询 / check_sig / OAuth 用 niquests 裸会话自己做；
        3) 最后用 client.execute(CgiRequest) 交换凭据（该链路实测与歌曲 CGI 同域，快照安全）。
        """
        import random
        import re as _re
        from time import time as _time
        from uuid import uuid4 as _uuid4

        # 首选：QQ 音乐 App 扫码（MOBILE）。整条链路走 music.login.LoginServer CGI + MQTT，
        # 全程实测不触发库内部对 ptlogin 域 Cookie（ETK）的解析崩溃。
        try:
            from qqmusic_api.models.login_utils import QRCodeLoginSession
        except Exception:
            try:
                from qqmusic_api.modules.login_utils import QRCodeLoginSession
            except Exception:
                QRCodeLoginSession = None
        if QRCodeLoginSession is not None:
            try:
                c = await self._qm_client(force_new=True)
                login_api = getattr(c, "login", None) or getattr(c, "auth", None)
                if login_api is not None:
                    from qqmusic_api.models.login import QRLoginType as _QRT
                    session = QRCodeLoginSession(login_api, _QRT.MOBILE, interval=2.0, timeout_seconds=120.0)
                    qr = await session.get_qrcode()
                    data = getattr(qr, "data", None) or getattr(qr, "qrcode", None)
                    if isinstance(data, str) and data.startswith("data:image"):
                        import base64
                        data = base64.b64decode(data.split(",", 1)[1])
                    if data:
                        qr_path = os.path.join(TMP_DIR, f"qr_{int(_time())}.png")
                        with open(qr_path, "wb") as f:
                            f.write(data)
                        await self._send(umo, [
                            Plain("当前点播的歌曲需 QQ 音乐授权（VIP 或高品质音源）。\n请在 120 秒内使用手机 QQ 或 QQ音乐 App 扫描下方二维码：\n"),
                            Image.fromFileSystem(qr_path),
                        ])
                        try:
                            cred = await session.wait_qrcode_login()
                        except Exception as we:
                            logger.warning(f"[ace_singing] MOBILE 扫码未完成: {we}")
                            return None
                        return self._cred_to_dict(cred)
            except Exception as mobile_err:
                logger.warning(f"[ace_singing] MOBILE 扫码线路异常，转 QQ 网页扫码: {mobile_err}")

        import niquests
        from qqmusic_api.core.request import CgiRequest
        from qqmusic_api.models.login import QRLoginType
        from qqmusic_api.utils import hash33

        c = await self._qm_client(force_new=True)
        qr = await c.login.get_qrcode(QRLoginType.QQ)
        data = getattr(qr, "data", None) or getattr(qr, "qrcode", None)
        if isinstance(data, str) and data.startswith("data:image"):
            import base64
            data = base64.b64decode(data.split(",", 1)[1])
        if not data:
            await self._send(umo, [Plain("获取 QQ 音乐登录二维码失败")])
            return None
        qrsig = getattr(qr, "identifier", None) or getattr(qr, "qrsig", "")
        if not qrsig:
            await self._send(umo, [Plain("获取二维码会话标识失败")])
            return None

        qr_path = os.path.join(TMP_DIR, f"qr_{int(_time())}.png")
        with open(qr_path, "wb") as f:
            f.write(data)
        await self._send(umo, [
            Plain("当前点播的歌曲需 QQ 音乐授权（VIP 或高品质音源）。\n请在 120 秒内使用手机 QQ 扫描下方二维码登录：\n"),
            Image.fromFileSystem(qr_path),
        ])

        sess = niquests.Session()
        try:
            sess.trust_env = False
        except Exception:
            pass
        sess.headers.update({"Referer": "https://xui.ptlogin2.qq.com/"})

        login_url = None
        t0 = _time()
        while _time() - t0 < 120:
            try:
                r = sess.get(
                    "https://ssl.ptlogin2.qq.com/ptqrlogin",
                    params={
                        "u1": "https://graph.qq.com/oauth2.0/login_jump",
                        "ptqrtoken": str(hash33(qrsig)),
                        "ptredirect": "0",
                        "h": "1",
                        "t": "1",
                        "g": "1",
                        "from_ui": "1",
                        "ptlang": "2052",
                        "action": f"0-0-{int(_time() * 1000)}",
                        "js_ver": "20102616",
                        "js_type": "1",
                        "pt_uistyle": "40",
                        "aid": "716027609",
                        "daid": "383",
                        "pt_3rd_aid": "100497308",
                        "has_onekey": "1",
                    },
                    cookies={"qrsig": qrsig},
                    timeout=15,
                )
            except Exception as poll_err:
                logger.warning(f"[ace_singing] ptqrlogin 轮询异常: {poll_err}")
                return None
            text = r.text or ""
            m = _re.search(r"ptuiCB\((.*?)\)", text)
            if not m:
                logger.warning(f"[ace_singing] ptqrlogin 响应解析失败: {text[:200]}")
                return None
            args = _re.findall(r"'((?:\\.|[^'])*)'", m.group(1))
            if not args or not args[0].isdigit():
                await asyncio.sleep(2)
                continue
            code = int(args[0])
            if code in (66, 67):
                await asyncio.sleep(2)
                continue
            if code == 0 and len(args) >= 3:
                sigx_m = _re.search(r"(?:\?|&)ptsigx=(.+?)&s_url", args[2])
                uin_m = _re.search(r"(?:\?|&)uin=(.+?)&service", args[2])
                if sigx_m and uin_m:
                    login_url = (
                        "https://ssl.ptlogin2.graph.qq.com/check_sig?" + args[2].replace("&amp;", "&")
                    ) if "&ptsigx=" in args[2] else None
                break
            if code in (65, 71):
                logger.warning(f"[ace_singing] ptqrlogin 状态码 {code}")
                await asyncio.sleep(2)
                continue
            logger.warning(f"[ace_singing] ptqrlogin 终态码 {code}: {text[:150]}")
            return None

        if not (code == 0 and len(args) >= 3):
            logger.warning("[ace_singing] 扫码超时或二维码过期")
            return None
        sigx_match = _re.search(r"(?:\?|&)ptsigx=(.+?)&s_url", args[2])
        uin_match = _re.search(r"(?:\?|&)uin=(.+?)&service", args[2])
        if not sigx_match or not uin_match:
            logger.warning("[ace_singing] 登录完成后无法解析 sigx/uin")
            return None

        r2 = sess.get(
            "https://ssl.ptlogin2.graph.qq.com/check_sig",
            params={
                "uin": uin_match[0],
                "pttype": "1",
                "service": "ptqrlogin",
                "nodirect": "0",
                "ptsigx": sigx_match[0],
                "s_url": "https://graph.qq.com/oauth2.0/login_jump",
                "ptlang": "2052",
                "ptredirect": "100",
                "aid": "716027609",
                "daid": "383",
                "j_later": "0",
                "low_login_hour": "0",
                "regmaster": "0",
                "pt_login_type": "3",
                "pt_aid": "0",
                "pt_aaid": "16",
                "pt_light": "0",
                "pt_3rd_aid": "100497308",
            },
            cookies={},
            allow_redirects=False,
            timeout=15,
        )
        sig_cookies = self._static_cookies_dict(r2.cookies)
        sess.cookies.update(sig_cookies)
        p_skey = sig_cookies.get("p_skey", "")
        if not p_skey:
            # 兜底：直接解析响应头的 Set-Cookie（多个 cookie 可能被合并或各持一条）
            try:
                set_cookie_lines = []
                try:
                    set_cookie_lines = list(r2.raw.headers.getlist("Set-Cookie"))
                except Exception:
                    pass
                if not set_cookie_lines:
                    sc_val = (r2.headers or {}).get("Set-Cookie", "")
                    if sc_val:
                        set_cookie_lines = [sc_val]
                for line in set_cookie_lines:
                    for piece in line.split("\n"):
                        head = piece.split(";")[0]
                        if "=" in head:
                            nm, val = head.split("=", 1)
                            nm = nm.strip()
                            if nm and val:
                                sig_cookies.setdefault(nm, val.strip())
                logger.warning(
                    f"[ace_singing] check_sig 状态={r2.status_code}, "
                    f"Set-Cookie条数={len(set_cookie_lines)}, cookies={list(sig_cookies)}"
                )
            except Exception as parse_err:
                logger.warning(f"[ace_singing] check_sig Set-Cookie 解析异常: {parse_err}")
            p_skey = sig_cookies.get("p_skey", "")
        if not p_skey:
            logger.warning(
                f"[ace_singing] check_sig 未拿到 p_skey (status={getattr(r2, 'status_code', '?')}, "
                f"headers={dict(list((r2.headers or {}).items())[:8])}, body={ (r2.text or '')[:200] })"
            )
            return None

        r3 = sess.post(
            "https://graph.qq.com/oauth2.0/authorize",
            data={
                "response_type": "code",
                "client_id": "100497308",
                "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
                "scope": "get_user_info,get_app_friends",
                "state": "state",
                "switch": "",
                "from_ptlogin": "1",
                "src": "1",
                "update_auth": "1",
                "openapi": "1010_1030",
                "g_tk": hash33(p_skey, 5381),
                "auth_time": str(int(_time() * 1000)),
                "ui": str(_uuid4()),
            },
            cookies=dict(sess.cookies),
            allow_redirects=False,
            timeout=15,
        )
        location = (r3.headers or {}).get("Location", "")
        code_match = _re.findall(r"(?<=code=)(.+?)(?=&)", location)
        if not code_match:
            logger.warning(f"[ace_singing] OAuth authorize 未返回 code: {location[:150]}")
            return None

        req = CgiRequest(
            _executor=None,
            module="QQConnectLogin.LoginServer",
            method="QQLogin",
            param={"code": code_match[0]},
            comm={"tmeLoginType": 2},
            allow_error_codes=(1000, 104401, 104400, 20261, 20271, 20272, 20274, 20277, 20278, 20279, 20450, 104604),
        )
        try:
            data = await c.execute(req)
        except Exception as e:
            logger.error(f"[ace_singing] QQLogin CGI 调用失败: {e}")
            return None
        if not isinstance(data, dict):
            logger.error(f"[ace_singing] QQLogin CGI 返回非 dict: {type(data).__name__}")
            return None
        enc = data.get("code", 0)
        if enc != 0:
            logger.error(f"[ace_singing] QQLogin CGI 业务码异常: {data}")
            return None
        try:
            from qqmusic_api.models.request import Credential as _Credential
            cred = _Credential.model_validate(data)
        except Exception as e:
            logger.error(f"[ace_singing] 凭据解析失败: {e}; data={data}")
            return None
        return self._cred_to_dict(cred)

    async def _qr_login_legacy(self, umo: str):
        """旧版函数式扫码接口（get_qrcode / get_qrcode_result）。"""
        try:
            from qqmusic_api.modules.login import get_qrcode, get_qrcode_result
        except Exception:
            from qqmusic_api.login import get_qrcode, get_qrcode_result
        c = await self._qm_client(force_new=True)
        res = await get_qrcode(_qr_login_type(), client=c)
        qr_bytes = res.get("data")
        qrsig = res.get("qrsig")
        if not qr_bytes or not qrsig:
            await self._send(umo, [Plain("获取 QQ 音乐登录二维码失败")])
            return None

        qr_path = os.path.join(TMP_DIR, f"qr_{int(time.time())}.png")
        with open(qr_path, "wb") as f:
            f.write(qr_bytes)

        await self._send(umo, [
            Plain("当前点播的歌曲需 QQ 音乐授权（VIP 或高品质音源）。\n请在 120 秒内使用手机 QQ 扫描下方二维码登录：\n"),
            Image.fromFileSystem(qr_path),
        ])

        t0 = time.time()
        while time.time() - t0 < 120:
            await asyncio.sleep(2)
            try:
                chk = await get_qrcode_result(qrsig, _qr_login_type(), client=c)
                code = chk.get("code")
                if code == 0:
                    cred = chk.get("credential")
                    if cred:
                        return self._cred_to_dict(cred)
                elif code not in (66, 67):
                    break
            except Exception:
                pass
        return None

    async def _poll_login(self, umo: str):
        try:
            try:
                cred_dict = await self._qr_login_modern(umo)
            except ImportError as ie:
                logger.warning(f"[ace_singing] 新版扫码接口不可用 ({ie})，回退旧版接口")
                cred_dict = await self._qr_login_legacy(umo)
            if cred_dict and cred_dict.get("musickey"):
                self._save_cred(cred_dict)
                await self._qm_client(force_new=True)
                await self._send(umo, [Plain("QQ 音乐登录成功！凭据已保存。")])
                if self._pending_retry:
                    info = self._pending_retry
                    self._pending_retry = None
                    asyncio.create_task(self._bg_cover(info["umo"], info["keyword"], info["full"]))
            else:
                await self._send(umo, [Plain("扫码超时或取消，请稍后重新点唱。")])
        except Exception as e:
            logger.error(f"[ace_singing] 扫码流程异常: {e}")
            await self._send(umo, [Plain(f"登录流程出错：{e}")])
        finally:
            self._login_task = None

    # ================= 乐句起唱吸附与音频预处理 =================

    async def _prepare_cover_audio(
        self,
        audio_data: bytes,
        full_song: bool,
        target_sec: int = 36,
        lyric_text: str | None = None,
    ) -> str:
        """
        将下载到的原曲转换为 44.1kHz 立体声 WAV，并智能提取高潮乐句。
        高潮定位优先级：
        1) 带时间戳歌词（LRC）定位重复副歌段，保证落在人声乐句边界；
        2) 无歌词时用 RMS 能量包络定位最响的高潮段，并吸附人声起止谷点。
        全程带 0.6s 淡入与 2.2s 平滑渐隐。
        """
        raw_in = os.path.join(TMP_DIR, f"in_{uuid.uuid4().hex[:8]}.raw")
        out_wav = os.path.join(TMP_DIR, f"prep_{uuid.uuid4().hex[:8]}.wav")
        with open(raw_in, "wb") as f:
            f.write(audio_data)

        loop = asyncio.get_running_loop()

        def _probe_duration(path: str) -> float:
            try:
                probe_cmd = [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", path,
                ]
                res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
                return float(res.stdout.strip() or 0.0)
            except Exception:
                return 0.0

        def _decode_mono_pcm(path: str, sr: int = 8000):
            import sys
            cmd = [
                "ffmpeg", "-v", "error", "-i", path,
                "-ac", "1", "-ar", str(sr), "-f", "s16le", "-",
            ]
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            data = out.stdout
            n = len(data) // 2
            samples = array.array("h")
            samples.frombytes(data[: n * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            return samples, sr

        def _rms_envelope(samples, sr: int, win_sec: float = 0.5) -> list:
            win = max(1, int(sr * win_sec))
            rms = []
            for i in range(0, len(samples) - win + 1, win):
                chunk = samples[i:i + win]
                s = sum(v * v for v in chunk) / len(chunk)
                rms.append(math.sqrt(s))
            return rms

        def _smooth(values: list, radius: int = 1) -> list:
            out = []
            n = len(values)
            for i in range(n):
                lo = max(0, i - radius)
                hi = min(n, i + radius + 1)
                out.append(sum(values[lo:hi]) / (hi - lo))
            return out

        def _snap_valley(rms: list, idx: int, radius: int, direction: int) -> int:
            best = idx
            best_val = rms[idx]
            if direction < 0:
                lo = max(0, idx - radius)
                hi = idx + 1
            else:
                lo = idx
                hi = min(len(rms), idx + radius + 1)
            for j in range(lo, hi):
                if rms[j] < best_val:
                    best_val = rms[j]
                    best = j
            # 仅当谷点明显低于中心时才吸附，避免噪声误判
            if best_val < rms[idx] * 0.82:
                return best
            return idx

        def _locate_by_lyrics(lrc_text: str, total_dur: float, target: float):
            parsed = self._parse_lrc(lrc_text)
            sing = [(t, txt) for t, txt in parsed if self._is_singable(txt) and 0 <= t <= total_dur + 5]
            if not sing:
                return None

            def _norm(s: str) -> str:
                return re.sub(r"[\s，。！？、,.!?\"'()（）\-_]", "", s).lower()

            counts = Counter(_norm(txt) for _, txt in sing)
            chorus_norms = {n for n, c in counts.items() if c >= 2 and len(n) >= 2}

            region_lo = total_dur * 0.30
            region_hi = total_dur * 0.80

            start = None
            if chorus_norms:
                # 副歌通常是重复句，取 30%~80% 区间内第一次出现的重复句
                for t, txt in sing:
                    if _norm(txt) in chorus_norms and t >= region_lo - 6.0:
                        start = t
                        break
                if start is None:
                    for t, txt in sing:
                        if _norm(txt) in chorus_norms:
                            start = t
                            break
            else:
                # 无重复句：取 30%~80% 区间内歌词最密集的一簇（通常即副歌）
                window = max(12.0, target * 0.8)
                best_start = None
                best_count = -1
                times = [t for t, _ in sing]
                for t in times:
                    if t < region_lo - 6.0 or t > region_hi:
                        continue
                    cnt = sum(1 for x in times if t <= x < t + window)
                    if cnt > best_count:
                        best_count = cnt
                        best_start = t
                if best_start is None:
                    best_start = min(sing, key=lambda x: abs(x[0] - total_dur * 0.5))[0]
                start = best_start

            if start is None:
                return None

            start = max(0.0, start - 0.35)
            target_end = start + target
            end = target_end
            # 在目标时长附近寻找歌词乐句边界收尾，避免把一句歌词拦腰截断
            boundary_candidates = [t for t, _ in sing if start + 1.0 < t <= target_end + 8.0]
            if boundary_candidates:
                after = [t for t in boundary_candidates if t >= target_end]
                if after:
                    end = min(after[0] + 0.3, target_end + 8.0)
                else:
                    end = max(boundary_candidates) + 0.5
            end = min(end, total_dur - 1.5)
            if end - start < 12.0:
                end = min(total_dur - 1.5, start + target)
            return start, end

        def _locate_by_energy(path: str, total_dur: float, target: float):
            try:
                samples, sr = _decode_mono_pcm(path)
            except Exception as e:
                logger.warning(f"[ace_singing] PCM 解码失败，回退标称副歌区间: {e}")
                return None
            if not samples:
                return None
            rms = _smooth(_rms_envelope(samples, sr), radius=1)
            if not rms:
                return None

            win_sec = 0.5
            target_windows = max(1, int(target / win_sec))
            region_lo = int(len(rms) * 0.30)
            region_hi = int(len(rms) * 0.80)

            best_i = region_lo
            best_energy = -1.0
            for i in range(region_lo, min(region_hi, len(rms) - target_windows + 1)):
                e = sum(rms[i:i + target_windows])
                if e > best_energy:
                    best_energy = e
                    best_i = i

            start_idx = _snap_valley(rms, best_i, radius=6, direction=-1)
            end_idx = min(best_i + target_windows, len(rms) - 1)
            end_idx = _snap_valley(rms, end_idx, radius=8, direction=1)

            actual_start = max(0.0, start_idx * win_sec - 0.25)
            actual_end = min(total_dur - 1.0, end_idx * win_sec + 0.4)
            if actual_end - actual_start < 12.0:
                actual_start = max(0.0, total_dur * 0.38)
                actual_end = min(total_dur - 1.0, actual_start + target)
            return actual_start, actual_end

        def _ffmpeg_work():
            total_dur = _probe_duration(raw_in)

            # 完整版 / 短歌 / 接近目标时长的歌：整曲转换不截断
            if full_song or total_dur <= 50.0 or total_dur <= (target_sec + 10.0):
                cmd = [
                    "ffmpeg", "-y", "-i", raw_in,
                    "-ac", "2", "-ar", "44100",
                    "-af", "afade=t=in:ss=0:d=0.6",
                    out_wav,
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
                return out_wav

            span = None
            if lyric_text:
                try:
                    span = _locate_by_lyrics(lyric_text, total_dur, float(target_sec))
                    if span:
                        logger.info(f"[ace_singing] 歌词高潮定位: {span[0]:.2f}s ~ {span[1]:.2f}s")
                except Exception as e:
                    logger.warning(f"[ace_singing] 歌词定位失败: {e}")

            if span is None:
                span = _locate_by_energy(raw_in, total_dur, float(target_sec))
                if span:
                    logger.info(f"[ace_singing] 能量高潮定位: {span[0]:.2f}s ~ {span[1]:.2f}s")

            if span is None:
                actual_start = total_dur * 0.38
                actual_end = min(total_dur - 2.0, actual_start + target_sec)
            else:
                actual_start, actual_end = span

            clip_dur = max(12.0, actual_end - actual_start)
            fade_out_start = max(0.1, clip_dur - 2.2)
            filter_str = f"afade=t=in:ss=0:d=0.6,afade=t=out:st={fade_out_start:.2f}:d=2.2"
            cmd = [
                "ffmpeg", "-y", "-ss", f"{actual_start:.2f}",
                "-t", f"{clip_dur:.2f}",
                "-i", raw_in,
                "-ac", "2", "-ar", "44100",
                "-af", filter_str,
                out_wav,
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
            return out_wav

        await loop.run_in_executor(None, _ffmpeg_work)

        try:
            os.remove(raw_in)
        except Exception:
            pass

        return out_wav

    def _run_gradio_convert(self, cover_base: str, timeout_val: float, args: tuple, progress_q, task_key: str):
        """同步线程内调用网关 /convert：优先 submit + live_updates 回传真实进度；
        任何异常都回退到无进度的 predict，保证翻唱功能不被进度特性拖垮。"""
        if task_key and task_key in getattr(self, "_cancelled", set()):
            raise RuntimeError("task cancelled by user")
        client = GrClient(cover_base, httpx_kwargs={"timeout": timeout_val, "trust_env": False}, verbose=False)
        try:
            job = client.submit(*args, api_name="/convert")
            if task_key:
                self._active_jobs[task_key] = job
            try:
                for upd in job.live_updates():
                    code = getattr(upd, "code", None)
                    if isinstance(upd, dict):
                        code = code if code is not None else upd.get("code")
                    if code not in (None, "progress"):
                        continue
                    prog = getattr(upd, "progress", None)
                    if prog is None and isinstance(upd, dict):
                        prog = upd.get("progress")
                    if prog is None:
                        continue
                    msg = getattr(upd, "message", None) or getattr(upd, "msg", "") or ""
                    if isinstance(upd, dict):
                        msg = msg or upd.get("msg") or upd.get("message") or ""
                    if progress_q is not None:
                        try:
                            progress_q.put_nowait((float(prog), str(msg or "")))
                        except Exception:
                            pass
            except Exception:
                pass
            out = job.result()
        except Exception:
            try:
                self._active_jobs.pop(task_key, None)
            except Exception:
                pass
            out = client.predict(*args, api_name="/convert")
        if isinstance(out, (tuple, list)):
            return out[0]
        return out

    async def _call_cover_gateway(self, target_audio_path: str, voice_profile: str = "", prompt_vocal_sep: bool | None = None, song_name: str = "", auto_mix_acc: bool = True, progress_q: "asyncio.Queue | None" = None, task_key: str = "") -> bytes:
        """调用 Seed-VC 翻唱网关完成换声推理 + 原曲伴奏混音。

        prompt_vocal_sep 默认取配置 svc_prompt_vocal_sep（默认 False）。
        仅当参考音频本身是带伴奏的整曲/混音时才应开启——可避免伴奏被当音色特征学进去。
        音色库里的短人声样本已是干净人声，必须保持关闭，否则 MSST 二次分离会损伤音色、降低遵循度。
        """
        if prompt_vocal_sep is None:
            prompt_vocal_sep = bool(getattr(self, "svc_prompt_vocal_sep", False))
        if not GRADIO_CLIENT_OK:
            raise RuntimeError("系统缺少 gradio-client 依赖，请在容器中执行 pip install gradio-client")

        active_voice = (voice_profile or "").strip() or self.svc_voice_profile or self.DEFAULT_VOICE_PROFILE

        ref_upload = None
        if self.svc_voice_path and os.path.exists(self.svc_voice_path):
            ref_upload = handle_file(self.svc_voice_path)

        n_step = self.svc_n_step
        cfg = self.svc_cfg
        seed = self.svc_seed
        random_seed = (seed == -1)
        actual_seed = 42 if seed == -1 else seed

        args = (
            str(song_name or ""),               # 0: song_name_src（歌曲名/来源，网关日志显示用）
            active_voice,                       # 1: model_dropdown (人格音色)
            bool(prompt_vocal_sep),             # 2: prompt_vocal_sep（仅参考带伴奏时开启；干净人声样本保持 False）
            True,                               # 3: target_vocal_sep
            True,                               # 4: auto_shift
            bool(auto_mix_acc),                 # 5: auto_mix_acc（False = 输出清唱）
            0,                                  # 6: pitch_shift
            n_step,                             # 7: n_step (推理步数)
            cfg,                                # 8: cfg (1.0~3.0，越大音色越贴近参考)
            actual_seed,                        # 9: seed
            random_seed,                        # 10: random_seed
            handle_file(target_audio_path),     # 11: target_upload
            "",                                 # 12: reference_source
            ref_upload,                         # 13: reference_upload
            "msst",                             # 14: target_separation（用 MSST BS-Roformer 分离目标歌曲）
        )

        loop = asyncio.get_running_loop()
        out_wav_path = await loop.run_in_executor(
            None,
            lambda: self._run_gradio_convert(self._get_cover_api_base(), float(self.timeout), args, progress_q, task_key),
        )
        if not out_wav_path or not os.path.exists(out_wav_path):
            raise RuntimeError("网关推理返回空文件或路径不存在")

        with open(out_wav_path, "rb") as f:
            data = f.read()

        try:
            os.remove(out_wav_path)
        except Exception:
            pass

        # 通用亮度校正：对所有音色套用同一固定 EQ
        eq = getattr(self, "svc_timbre_eq", "")
        if eq:
            data = await loop.run_in_executor(None, self._apply_audio_eq, data, eq)

        return data

    async def _fetch_backup_source_audio(self, keyword: str):
        """QQ 音源失败时的备用音源：通过翻唱网关的 B 站 / 网易云 通道获取原始音频。"""
        mode = (self._cfg_str("fallback_source", "auto").strip().lower() or "auto")
        if mode in ("off", "disable", "关闭", "none"):
            return None, ""
        if mode == "netease":
            order = ["netease"]
        elif mode == "bilibili":
            order = ["bilibili"]
        else:
            order = ["netease", "bilibili"]

        for platform in order:
            try:
                def _predict_sync():
                    cover_base = self._get_cover_api_base()
                    timeout_val = float(self.timeout)
                    client = GrClient(cover_base, httpx_kwargs={"timeout": timeout_val, "trust_env": False}, verbose=False)
                    return client.predict(platform, keyword, 0, api_name="/fetch_source_gate")

                loop = asyncio.get_running_loop()
                src_path = await loop.run_in_executor(None, _predict_sync)
                if src_path and os.path.exists(src_path):
                    with open(src_path, "rb") as f:
                        data_bytes = f.read()
                    if data_bytes:
                        return data_bytes, platform
                logger.warning(f"[ace_singing] {platform} 备用源未产生有效文件")
            except Exception as e:
                logger.warning(f"[ace_singing] {platform} 备用源获取失败: {e}")
        return None, ""

    def _apply_audio_eq(self, data: bytes, eq: str) -> bytes:
        """对翻唱结果套用固定 EQ（ffmpeg），校正音色亮度。"""
        ext = self._audio_suffix(data)
        in_path = os.path.join(TMP_DIR, f"eqin_{uuid.uuid4().hex[:8]}.{ext}")
        out_path = os.path.join(TMP_DIR, f"eqout_{uuid.uuid4().hex[:8]}.{ext}")
        with open(in_path, "wb") as f:
            f.write(data)
        try:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", in_path, "-af", eq,
                "-c:a", "libmp3lame", "-b:a", "320k",
                out_path,
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in (in_path, out_path):
                try:
                    os.remove(p)
                except Exception:
                    pass

    @staticmethod
    def _audio_suffix(data: bytes) -> str:
        """根据音频文件二进制魔数智能纠正扩展名，杜绝扩展名欺骗导致 QQ 语音转码异常"""
        if data[:4] == b"RIFF":
            return "wav"
        if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
            return "mp3"
        if data[:4] == b"OggS":
            return "ogg"
        return "wav"

    def _save_tmp(self, data: bytes) -> str:
        ext = self._audio_suffix(data)
        path = os.path.join(TMP_DIR, f"cover_{uuid.uuid4().hex[:8]}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        return path

    async def _cleanup_old(self, keep_sec: int = 3600):
        now = time.time()
        for fn in os.listdir(TMP_DIR):
            fp = os.path.join(TMP_DIR, fn)
            try:
                if os.path.isfile(fp) and (now - os.path.getmtime(fp)) > keep_sec:
                    os.remove(fp)
            except Exception:
                pass

    async def _send_song(self, umo: str, audio_path: str):
        """以 QQ 原生语音形式发送翻唱音频。"""
        await self._send(umo, [Record(file=audio_path, url=audio_path)])

    def _maybe_risk_control(self, err_msg: str) -> bool:
        msg = str(err_msg).lower()
        if not msg:
            return False
        if any(k in msg for k in ("risk", "qrsig", "need login", "not login", "login expired", "凭据失效", "重新登录", "credential invalid")):
            return True
        if re.search(r"\b(code|errcode|status)[\s:=]+100\b", msg) or re.search(r"['\"]code['\"]\s*:\s*100\b", msg):
            return True
        return False

    # ================= 翻唱核心调度 =================

    async def _do_cover(self, umo: str, keyword: str, full_song: bool, acapella_override=None, progress_q=None, task_key: str = "") -> str:
        try:
            self._apply_config()
        except Exception:
            pass
        c = await self._qm_client()
        if not c:
            return "QQ 音乐客户端未就绪"

        # 关键词标准化：剥离误带的指令前缀与多余空格，避免污染精确匹配
        search_kw = re.sub(r"^\s*(点唱|唱|翻唱)?\s*", "", keyword or "").strip()
        logger.info(f"[ace_singing] 搜索 QQ 音乐: {search_kw}")
        song_list = await self._qq_search(c, search_kw, num=30)
        if not song_list:
            return f"未找到与《{search_kw}》相关的歌曲"

        # 严格匹配：候选歌名规范化后必须与关键词一致（或歌名是关键词前缀，兼容“歌名 歌手”写法）。
        # 匹配不上宁可拒绝，也不把模糊命中的另一首歌当目标。
        def _norm_name(s):
            return re.sub(r"\s+", "", str(s or "")).lower()

        kw_norm = _norm_name(search_kw)
        if not kw_norm:
            return "请告诉我要翻唱的歌曲名"
        target_song = None
        candidate_names = []
        for cand in song_list:
            cand_name = _norm_name(cand.get("name") or cand.get("songname") or cand.get("title"))
            if not cand_name:
                continue
            candidate_names.append(cand_name)
            if cand_name == kw_norm or kw_norm.startswith(cand_name):
                target_song = cand
                break
        if target_song is None:
            logger.warning(f"[ace_singing] 关键词《{search_kw}》没有任何完全匹配的搜索结果 (候选: {candidate_names})")
            return f"QQ 音乐没有搜到《{search_kw}》，请确认歌名后重试"
        song_mid = target_song.get("mid") or target_song.get("songmid")
        song_media_mid = target_song.get("media_mid") or target_song.get("str_media_mid") or song_mid
        song_name = target_song.get("name") or target_song.get("songname") or target_song.get("title") or keyword
        singer = "/".join([s.get("name", "") for s in target_song.get("singer", []) if s.get("name")])
        logger.info(f"[ace_singing] 命中原曲: {song_name} - {singer} (mid: {song_mid}, media_mid: {song_media_mid})")

        cred = self._load_cred()
        cdn_sips, audio_purl, audio_filename, audio_ekey = await self._qq_url(c, song_mid, media_mid=song_media_mid)

        header_variants = self._DOWNLOAD_HEADER_VARIANTS

        async def _try_download(u: str, combo_idx: int = 0, ekey: str = "", quality: str = ""):
            """下载 → （若带 ekey）QMCv2 本地解密恢复 QQ 源，失败返回 None。"""
            return await self._download_and_unlock(
                u, ekey, quality=quality, song_mid=song_mid, combo_idx=combo_idx
            )

        audio_data = None
        dl_err = ""
        if not audio_purl:
            last_err = getattr(self, "_last_url_err", "")
            # 1. 检查是否无凭据，或者凭据已失效/触发风控
            if not cred or self._maybe_risk_control(last_err):
                logger.info(f"[ace_singing] 未能取流且无凭据或触发风控 ({last_err})，唤起扫码登录")
                await self._send(umo, [Plain(f"《{song_name}》需要 QQ 音乐登录凭据，正在唤起扫码授权...")])
                await self._pop_login(umo, retry_info={"umo": umo, "keyword": keyword, "full": full_song})
                return "已弹出扫码登录"
            # 2. 凭据存在且非风控：普通通道无 purl，转加密通道 + 备用源兜底（不再直接放弃）
            logger.warning(f"[ace_singing] 普通通道无 purl ({last_err})，转加密通道 + 备用源兜底: mid={song_mid}")
            dl_err = f"普通通道无 purl: {last_err}"
        else:
            logger.info("[ace_singing] 正在下载音源...")

            for u in self._audio_url_variants(audio_purl, cdn_sips, audio_filename):
                audio_data = await _try_download(u, 0, audio_ekey)
                if audio_data is not None:
                    break
            if audio_data is None:
                # 换头组合扫前三个域名（手机 UA / Range 等）
                for combo_idx in range(1, len(header_variants)):
                    for u in self._audio_url_variants(audio_purl, cdn_sips, audio_filename)[:3]:
                        audio_data = await _try_download(u, combo_idx, audio_ekey)
                        if audio_data is not None:
                            logger.info(f"[ace_singing] 命中的下载头组合 #{combo_idx}")
                            break
                    if audio_data is not None:
                        break
            if audio_data is None:
                dl_err = "所有 CDN 镜像均下载失败"
                logger.warning("[ace_singing] 所有 CDN 镜像均 404，尝试降级音质...")
                if self._maybe_risk_control(dl_err):
                    await self._send(umo, [Plain("音源下载触发风控，正在唤起重新扫码授权...")])
                    await self._pop_login(umo, retry_info={"umo": umo, "keyword": keyword, "full": full_song})
                    return "已弹出扫码登录"
                try:
                    degraded_sips, degraded_purl, degraded_filename, degraded_ekey = await self._qq_url(
                        c, song_mid, media_mid=song_media_mid, downgrade=True
                    )
                    if degraded_purl:
                        logger.info("[ace_singing] 降级为更低音质，重试动态 CDN...")
                        for u in self._audio_url_variants(degraded_purl, degraded_sips, degraded_filename):
                            audio_data = await _try_download(u, 0, degraded_ekey)
                            if audio_data is not None:
                                break
                except Exception as e2:
                    dl_err = f"{dl_err}; 降级取链: {e2}"
                    logger.error(f"[ace_singing] 降级取链失败: {e2}")

        # 3. 加密通道（GetEVkey + 本地 QMCv2 解密）：普通链全灭后尝试恢复 QQ 源
        if audio_data is None and cred:
            try:
                audio_data = await self._qq_encrypted_probe(
                    c, song_mid, media_mid=song_media_mid, sips=cdn_sips
                )
            except Exception as pe:
                logger.warning(f"[ace_singing] 加密通道探测异常: {pe}")
        if audio_data is None:
            fb_mode = (self._cfg_str("fallback_source", "auto").strip().lower() or "auto")
            if fb_mode not in ("off", "disable", "关闭", "none"):
                logger.info(f"[ace_singing] QQ 音源失败，静默切换备用音源 ({fb_mode}) ...")
                fb_bytes, fb_platform = await self._fetch_backup_source_audio(search_kw)
                if fb_bytes:
                    audio_data = fb_bytes
                    logger.info(f"[ace_singing] 备用音源成功 (来源: {fb_platform})")
                else:
                    logger.warning("[ace_singing] 备用音源全部失败")
        if audio_data is None:
            logger.error(f"[ace_singing] 音源下载彻底失败: {dl_err}")
            import random as _random
            fail_text = _random.choice(_COVER_FAIL_VARIANTS).format(song=song_name)
            await self._send(umo, [Plain(fail_text)])
            return "这首歌暂时唱不了（音源都失败了）"

        # 拉取带时间戳歌词，用于优先按人声乐句定位高潮（缺省时回退能量法）
        lyric_text = None
        try:
            lyric_res = await asyncio.wait_for(self._qq_lyric(c, song_mid), timeout=12)
            lyric_text = self._extract_lrc_text(lyric_res)
            if lyric_text:
                logger.info(f"[ace_singing] 已获取 LRC 歌词 ({len(lyric_text)} 字符)，优先按歌词乐句定位高潮")
        except Exception as e:
            logger.warning(f"[ace_singing] 歌词获取失败，将回退能量法: {e}")

        song_wav = await self._prepare_cover_audio(audio_data, full_song, self.svc_segment_sec, lyric_text=lyric_text)
        auto_mix = not (self.svc_acapella if acapella_override is None else bool(acapella_override))
        try:
            matched_voice = await self._resolve_matched_voice(umo)
            logger.info(f"[ace_singing] Seed-VC 翻唱 (音色: {matched_voice}, n_step={self.svc_n_step}, cfg={self.svc_cfg}, 清唱={not auto_mix})")
            try:
                converted_bytes = await self._call_cover_gateway(song_wav, voice_profile=matched_voice, song_name=search_kw or keyword, auto_mix_acc=auto_mix, progress_q=progress_q, task_key=task_key)
            except Exception as first_err:
                # frp 公网链路偶发传输中断（典型：推理完成后结果文件下载断开），重试一次
                logger.warning(f"[ace_singing] 翻唱网关调用失败（{first_err}），10s 后自动重试一次 ...")
                await asyncio.sleep(10)
                converted_bytes = await self._call_cover_gateway(song_wav, voice_profile=matched_voice, song_name=search_kw or keyword, auto_mix_acc=auto_mix, progress_q=progress_q, task_key=task_key)
            out_file = self._save_tmp(converted_bytes)
            await self._send_song(umo, out_file)
            await self._cleanup_old()
            return "成功"
        finally:
            try:
                os.remove(song_wav)
            except Exception:
                pass

    @filter.command("点唱")
    async def cover_cmd(self, event: AstrMessageEvent):
        """/点唱 <歌名> - QQ 音乐找歌并用当前人格专属声线翻唱（原曲原旋律）"""
        keyword = _clean_song_name(str(event.message_str or ""))
        if not keyword:
            yield event.plain_result("用法：/点唱 歌名（例如 /点唱 晴天）")
            return
        full_song = "完整" in keyword
        keyword = keyword.replace("完整版", "").replace("完整", "").strip()
        acapella_inline = "清唱" in keyword
        keyword = keyword.replace("清唱版", "").replace("清唱", "").strip()
        if not keyword:
            yield event.plain_result("用法：/点唱 歌名（例如 /点唱 晴天、/点唱 晴天 清唱、/点唱 晴天 完整）")
            return
        import random as _random
        asyncio.create_task(self._bg_cover(event.unified_msg_origin, keyword, full_song, acapella_override=acapella_inline if acapella_inline else None))
        yield event.plain_result(_random.choice(_COVER_ACK_VARIANTS).format(song=keyword))

    @filter.llm_tool(name="cover_a_song")
    async def cover_a_song(self, event: AstrMessageEvent, song_name: str, full_song: bool = False, acapella: bool = False):
        """用户点名想听 bot 翻唱某首已有的歌时调用。
        插件会自动从 QQ 音乐搜索原曲，经由 Seed-VC 保持原曲旋律与原声伴奏换成当前人格的专属音色，并发送语音。

        Args:
            song_name (str): 歌名，可含歌手。
            full_song (bool): 是否唱整首。默认 False 只唱高潮片段（约 36 秒）。用户明确要求完整版时为 True。
            acapella (bool): 是否输出清唱（只有换声后的人声、不带伴奏）。用户说"清唱版"时为 True。
        """
        umo = event.unified_msg_origin
        import random as _random
        song_name = _clean_song_name(song_name)
        asyncio.create_task(self._bg_cover(umo, song_name, full_song, acapella_override=True if acapella else None))
        return _random.choice(_COVER_ACK_VARIANTS).format(song=song_name)

    async def _resolve_matched_voice(self, umo: str) -> str:
        """解析当前会话生效的翻唱音色：人格映射 → 提供商默认 → 兜底，并对齐网关真实音色名。"""
        cover_voice = await self._resolve_cover_voice(umo)
        profiles = await self._fetch_voice_profiles()
        return self._match_voice_profile(cover_voice, profiles)

    async def _bg_cover(self, umo: str, keyword: str, full_song: bool, acapella_override=None):
        # 先解析实际生效音色：换音色视为不同任务，允许重唱
        try:
            voice = await self._resolve_matched_voice(umo)
        except Exception:
            voice = ""
        acapella = self.svc_acapella if acapella_override is None else bool(acapella_override)
        task_key = f"{umo}|{keyword}|{bool(full_song)}|{voice}|{'清唱' if acapella else '混音'}"
        # 去重：同一会话同一首歌同一音色正在唱时不重复开任务
        if task_key in getattr(self, "_active_covers", {}):
            await self._send(umo, [Plain(f"《{keyword}》正在唱了，稍等一下哦~")])
            return
        # 去重：5 分钟内同会话同歌同音色已成功发送过，不重复唱
        if time.time() - getattr(self, "_recent_covers", {}).get(task_key, 0) < 300:
            await self._send(umo, [Plain(f"刚用「{voice}」唱过《{keyword}》没几分钟，换音色或换首试试~")])
            return
        self._active_covers[task_key] = time.time()
        progress_q: "asyncio.Queue | None" = asyncio.Queue()

        # 进度回传（克制版）：真人翻唱不会一步步播报，只在明显卡久时轻催一句，平时不打扰
        async def _drain_progress():
            started = time.time()
            nudged = False
            while True:
                item = await progress_q.get()
                if item is None:
                    break
                if not nudged and (time.time() - started) > 40:
                    try:
                        await self._send(umo, [Plain("还在给你弄呢，马上好哈~")])
                    except Exception:
                        pass
                    nudged = True

        drainer = asyncio.create_task(_drain_progress())
        try:
            res = await self._do_cover(umo, keyword, full_song, acapella_override=acapella_override, progress_q=progress_q, task_key=task_key)
            if res == "成功":
                self._recent_covers[task_key] = time.time()
                cnt = getattr(self, "_user_cover_count", {})
                cnt[umo] = cnt.get(umo, 0) + 1
            elif res and res != "成功" and "已弹出扫码登录" not in res:
                await self._send(umo, [Plain(f"这首《{keyword}》翻唱遇到点问题（{res}），换一首试试吧~")])
        except Exception as e:
            if task_key in getattr(self, "_cancelled", set()):
                logger.info(f"[ace_singing] 翻唱任务《{keyword}》已被用户取消")
            else:
                import traceback
                logger.error(f"[ace_singing] 翻唱失败: {e}")
                logger.error("[ace_singing] 翻唱失败完整堆栈:\n" + traceback.format_exc())
                try:
                    await self._send(umo, [Plain(f"翻唱《{keyword}》失败了，稍后换首歌试试吧~")])
                except Exception:
                    pass
        finally:
            try:
                progress_q.put_nowait(None)
            except Exception:
                pass
            try:
                drainer.cancel()
            except Exception:
                pass
            self._active_covers.pop(task_key, None)
            self._active_jobs.pop(task_key, None)
            self._cancelled.discard(task_key)

    @filter.command("qq登录")
    async def qq_login_cmd(self, event: AstrMessageEvent):
        """/qq登录 - 手动弹出 QQ 音乐扫码登录二维码"""
        self._login_task = None
        if self._load_cred():
            yield event.plain_result("QQ 音乐已登录。如需重新登录请先删除凭据文件。")
            return
        await self._pop_login(event.unified_msg_origin, retry_info=None)
        yield event.plain_result("二维码已发送（见上方消息），扫码后自动生效。")

    # ================= 翻唱服务网络与模型诊断 =================

    async def _test_services_network(self) -> str:
        try:
            self._apply_config()
        except Exception:
            pass
        cover_base = self._get_cover_api_base()

        report = []
        report.append("【Seed-VC 翻唱服务诊断报告】")
        report.append("─────────────────────")
        report.append(f"• 翻唱 Provider ID: {self.cover_provider_id}")
        report.append(f"• 翻唱网关地址: {cover_base}")

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as s:
            try:
                async with s.get(cover_base) as r:
                    report.append(f"• 网关 HTTP 连通: 正常 (状态码 {r.status})")
            except Exception as e:
                report.append(f"• 网关 HTTP 连通: 失败 ({e})")

            try:
                async with s.get(f"{cover_base}/v1/models") as r:
                    if r.status == 200:
                        data = await r.json()
                        voices = []
                        for m in (data.get("data") or []):
                            voices.extend(str(v) for v in (m.get("voices") or []))
                        report.append(f"• 可用翻唱音色({len(voices)}): {', '.join(voices) if voices else '无'}")
                    else:
                        report.append(f"• 音色列表获取异常: {r.status}")
            except Exception as e:
                report.append(f"• 音色列表探测失败: {e}")

        report.append("─────────────────────")
        report.append("注：日常说话已由 AstrBot 内置 TTS 或 actively_calls_tts 接管，本插件专精翻唱。")
        return "\n".join(report)

    @filter.command("服务测试")
    async def diag_cmd(self, event: AstrMessageEvent):
        """/服务测试 - 诊断翻唱服务与音色连通状态"""
        res = await self._test_services_network()
        yield event.plain_result(res)

    @filter.command("唱歌测试")
    async def cover_diag_cmd(self, event: AstrMessageEvent):
        """/唱歌测试 - 测试翻唱服务连通性与音色列表"""
        res = await self._test_services_network()
        yield event.plain_result(res)

    @filter.command("唱歌状态")
    async def status_cmd(self, event: AstrMessageEvent):
        """/唱歌状态 - 查看当前翻唱配置、人格音色映射与链路运行状态"""
        cred = self._load_cred()
        diag = await self._test_services_network()
        base_info = (
            "【翻唱配置概览】\n"
            f"• 翻唱 Provider：{self.cover_provider_id} (默认音色: {self.svc_voice_profile})\n"
            f"• 人格-翻唱映射：已配置 {len(self._persona_cover_map)} 个人格专属声线\n"
            f"• 推理参数：n_step={self.svc_n_step} | cfg={self.svc_cfg} | seed={self.svc_seed}\n"
            f"• 智能副歌时长：{self.svc_segment_sec}s (歌词乐句/能量谷点定位 + 平滑渐隐)\n"
            f"• QQ 音乐授权：{'已登录' if cred else '未登录（点唱 VIP 歌时会自动弹扫码）'}\n"
            f"• 进行中任务：{len(getattr(self, '_active_covers', {}))} 个 | 你的累计翻唱：{getattr(self, '_user_cover_count', {}).get(event.unified_msg_origin, 0)} 首\n\n"
        )
        yield event.plain_result(base_info + diag)

    # ================= 翻唱任务 / 缓存 可观测性命令 =================

    async def _gw_predict(self, api_name: str, *args):
        """同步调用网关 Gradio api_name（用于缓存查询/清理等管理操作）。"""
        if not GRADIO_CLIENT_OK:
            raise RuntimeError("系统缺少 gradio-client 依赖")
        loop = asyncio.get_running_loop()

        def _sync():
            cover_base = self._get_cover_api_base()
            client = GrClient(cover_base, httpx_kwargs={"timeout": float(self.timeout), "trust_env": False}, verbose=False)
            return client.predict(*args, api_name=api_name)

        return await loop.run_in_executor(None, _sync)

    @staticmethod
    def _format_cache_info(info) -> str:
        if not isinstance(info, dict):
            return f"缓存信息：{info}"
        lines = ["【翻唱缓存】"]
        for k, v in info.items():
            lines.append(f"• {k}: {v}")
        return "\n".join(lines)

    @filter.command("唱歌任务")
    async def list_tasks_cmd(self, event: AstrMessageEvent):
        """/唱歌任务 - 查看当前正在进行的翻唱任务与耗时"""
        tasks = getattr(self, "_active_covers", {})
        if not tasks:
            yield event.plain_result("当前没有正在进行的翻唱任务~")
            return
        lines = ["【进行中的翻唱任务】"]
        now = time.time()
        for k, t in tasks.items():
            parts = k.split("|")
            name = parts[1] if len(parts) > 1 else "?"
            voice = parts[3] if len(parts) > 3 else "?"
            mode = parts[4] if len(parts) > 4 else ""
            lines.append(f"• 《{name}》音色={voice} {mode} | 已耗时 {int(now - t)}s")
        yield event.plain_result("\n".join(lines))

    @filter.command("取消唱歌")
    async def cancel_cover_cmd(self, event: AstrMessageEvent):
        """/取消唱歌 [歌名] - 取消正在进行的翻唱任务（带歌名精确取消，不带则取消全部进行中）"""
        kw = _clean_song_name(str(event.message_str or ""))
        jobs = getattr(self, "_active_jobs", {})
        covers = getattr(self, "_active_covers", {})
        matched = [tk for tk in covers if (not kw or kw in tk)]
        if not matched:
            yield event.plain_result("没有可取消的翻唱任务~")
            return
        for tk in matched:
            self._cancelled.add(tk)
            job = jobs.get(tk)
            if job is not None:
                try:
                    job.cancel()
                except Exception:
                    pass
            covers.pop(tk, None)
            jobs.pop(tk, None)
        yield event.plain_result(f"已取消 {len(matched)} 个翻唱任务" + (f"（《{kw}》相关）" if kw else ""))

    @filter.command("我的翻唱")
    async def my_stats_cmd(self, event: AstrMessageEvent):
        """/我的翻唱 - 查看你累计点唱数量"""
        cnt = getattr(self, "_user_cover_count", {}).get(event.unified_msg_origin, 0)
        yield event.plain_result(f"你总共点过 {cnt} 首翻唱 🎤")

    @filter.command("唱歌缓存")
    async def cache_info_cmd(self, event: AstrMessageEvent):
        """/唱歌缓存 - 查看翻唱结果缓存占用"""
        try:
            info = await self._gw_predict("/cache_info")
        except Exception as e:
            yield event.plain_result(f"缓存信息查询失败: {e}")
            return
        yield event.plain_result(self._format_cache_info(info))

    @filter.command("清理唱歌缓存")
    async def clear_cache_cmd(self, event: AstrMessageEvent):
        """/清理唱歌缓存 - 清理已缓存的翻唱结果（管理员）"""
        try:
            await self._gw_predict("/clear_cache", "all")
        except Exception as e:
            yield event.plain_result(f"清理失败: {e}")
            return
        yield event.plain_result("翻唱缓存已清理 ✅")

    @filter.llm_tool(name="test_singing_network")
    async def test_singing_network(self, event: AstrMessageEvent) -> str:
        """当用户要求测试翻唱服务、检查唱歌网络或排查音频链路时调用此工具。"""
        return await self._test_services_network()

    async def terminate(self):
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
