import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from playback import classify_stream, filter_streams

try:
    from curl_cffi import requests as cr
except ImportError:
    cr = None
    import httpx

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger("moviebox.upstream")

app = FastAPI(
    title="MovieBox 1080p API",
    description="REST API for MovieBox with 1080p Stream & Subtitle Extraction",
    version="2.6.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY_DEFAULT_B64 = "NzZpUmwwN3MweFNOOWpxbUVXQXQ3OUVCSlp1bElRSXNWNjRGWnIyTw=="
SECRET_KEY_ALT_B64 = "WHFuMm5uTzQxL0w5Mm8xaXVYaFNMSFRiWHZZNFo1Wlo2Mm04bVNMQQ=="
MODERN_USER_AGENT = "com.community.mbox.in/50020130 (Linux; U; Android 14; en_IN; Pixel 8; Build/UD1A.230803.041; Cronet/145.0.7582.0)"
WEB_USER_AGENT = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
API_BASE = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
NATIVE_API_BASE = "https://api3.aoneroom.com"
BOOTSTRAP_HOST = "https://apig.inmoviebox.com"
BOOTSTRAP_URL = f"{BOOTSTRAP_HOST}/wefeed-mobile-bff/tab-operating?host=api.inmoviebox.com&page=1&pageSize=24&tabId=1"

WEB_DOMAINS = [
    "https://123moviesfree.club",
    "https://movie-box.co",
    "https://movieboxonline.net",
    "https://sflix.film",
    "https://h5-api.aoneroom.com",
    "https://netnaija.film",
    "https://movieboxhd.net",
]

def _b64decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)

def get_secret_bytes(use_alt_key: bool = False) -> bytes:
    outer = SECRET_KEY_ALT_B64 if use_alt_key else SECRET_KEY_DEFAULT_B64
    inner = _b64decode(outer).decode("utf-8")
    return _b64decode(inner)

def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def generate_device_id() -> str:
    return secrets.token_hex(16)

def generate_x_client_token(timestamp: int) -> str:
    ts_str = str(timestamp)
    reversed_ts = ts_str[::-1].encode("utf-8")
    return f"{ts_str},{md5_hex(reversed_ts)}"

def generate_client_info(device_id: str) -> str:
    # Match the Android LocalMovieBoxServer client metadata from Anime-.
    return json.dumps(
        {
            "package_name": "com.community.oneroom",
            "version_name": "4.0.02",
            "version_code": 50020126,
            "os": "android",
            "os_version": "14",
            "install_ch": "ps",
            "device_id": device_id,
            "install_store": "ps",
            "gaid": "",
            "brand": "Google",
            "model": "Pixel 6",
            "system_language": "en",
            "net": "wifi",
            "region": "IN",
            "timezone": "Asia/Kolkata",
            "sp_code": "404",
        },
        separators=(",", ":"),
    )

def build_canonical_string(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        pairs = []
        for segment in parsed.query.split("&"):
            if not segment:
                continue
            pieces = segment.split("=", 1)
            pairs.append((pieces[0], pieces[1] if len(pieces) > 1 else ""))
        pairs.sort(key=lambda kv: kv[0])
        canonical_query = "&".join(f"{k}={v}" for k, v in pairs)
        canonical_url = f"{path}?{canonical_query}"
    else:
        canonical_url = path

    body_bytes = body.encode("utf-8") if body is not None else None
    if body_bytes:
        body_length = str(len(body_bytes))
        body_hash = md5_hex(body_bytes[:102400])
    else:
        body_length = ""
        body_hash = ""

    return "\n".join(
        [
            method.upper(),
            accept or "",
            content_type or "",
            body_length,
            str(timestamp),
            body_hash,
            canonical_url,
        ]
    )

def generate_x_tr_signature(method: str, accept: str | None, content_type: str | None, url: str, body: str | None, timestamp: int, use_alt_key: bool = False) -> str:
    canonical = build_canonical_string(method, accept, content_type, url, body, timestamp)
    key = get_secret_bytes(use_alt_key)
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.md5).digest()
    sig_b64 = base64.b64encode(digest).decode("ascii")
    return f"{timestamp}|2|{sig_b64}"

def build_auth_headers(method: str, url: str, body: str | None = None, token: str | None = None, device_id: str | None = None, use_alt_key: bool = False, content_type: str = "application/json", accept: str = "application/json") -> Dict[str, str]:
    ts = int(time.time() * 1000)
    dev_id = device_id or generate_device_id()
    headers = {
        "user-agent": "MovieBox/4.0.02 (Android 14; Pixel 6)",
        "accept": accept,
        "content-type": content_type,
        "connection": "keep-alive",
        "x-client-token": generate_x_client_token(ts),
        "x-tr-signature": generate_x_tr_signature(method, accept, content_type, url, body, ts, use_alt_key),
        "x-client-info": generate_client_info(dev_id),
        "x-client-status": "0" if token else "1",
        "x-sign-version": "2.0",
        "appid": "4U01pxRu278GqCZKY9",
        "region": "IN",
        "lang": "en",
        "os": "android",
        "x-timestamp": str(ts),
        "referer": "https://api.inmoviebox.com/",
        "x-play-mode": "2",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers

def extract_policy_resource(sign_cookie: str | None) -> str | None:
    if not sign_cookie:
        return None
    m_edge = re.search(r"Edge-Cache-Cookie=urlprefix=([^:;\s]+)", sign_cookie)
    if m_edge:
        try:
            decoded = _b64decode(m_edge.group(1)).decode("utf-8").rstrip("/")
            return decoded + "/index.mpd"
        except Exception:
            pass
    m_cf = re.search(r"CloudFront-Policy=([^;]+)", sign_cookie)
    if m_cf:
        raw = m_cf.group(1)
        for t in ("cf", "std"):
            try:
                b = raw.replace("-", "+").replace("_", "=") if t == "cf" else raw
                b += "=" * (-len(b) % 4)
                data = json.loads(base64.b64decode(b).decode("utf-8"))
                for s in data.get("Statement", []):
                    res = s.get("Resource")
                    if res:
                        return res.rstrip("*").rstrip("/") + "/index.mpd"
            except Exception:
                pass
    return None

class MovieBoxEngine:
    _instance = None

    def __init__(self):
        self.device_id = generate_device_id()
        self.bearer_token: Optional[str] = None
        self.api_base = NATIVE_API_BASE.rstrip("/")
        self.bootstrap_host = BOOTSTRAP_HOST.rstrip("/")
        if cr:
            self.session = cr.Session(impersonate="chrome120")
        else:
            self.session = httpx.Client(timeout=25, follow_redirects=True)
        self.resolve_api_domain()

    @classmethod
    def get_instance(cls) -> "MovieBoxEngine":
        if cls._instance is None:
            cls._instance = MovieBoxEngine()
        return cls._instance

    def resolve_api_domain(self) -> None:
        """Ask MovieBox's official GSLB for the current API host mapping."""
        package_name = "com.community.oneroom"
        device_hash = hashlib.sha256(self.device_id.encode("utf-8")).hexdigest()
        payload = {
            "appId": package_name,
            "key": device_hash,
            "oldKey": "",
            "domains": ["apig.inmoviebox.com", "api.inmoviebox.com"],
            "mcc": "404",
            "locale": "IN",
            "language": "en",
            "model": "Pixel 8",
        }
        body = json.dumps(payload, separators=(",", ":"))
        gslb_signature = hashlib.sha256(
            (package_name + device_hash).encode("utf-8")
        ).hexdigest()
        try:
            response = self.session.post(
                "https://gslb.shalltry.com/gslb/domain/convert",
                headers={
                    "accept": "*/*",
                    "Content-Type": "application/json",
                    "X-Gslb-Sign": gslb_signature,                    "User-Agent": "okhttp/4.11.0",
                },
                data=body.encode("utf-8"),
                timeout=12,
            )
            if response.status_code != 200:
                return
            payload_out = response.json()
            mapping = payload_out.get("data", {}) if isinstance(payload_out, dict) else {}
            mapped_host = mapping.get("apig.inmoviebox.com") or mapping.get("api.inmoviebox.com")
            if isinstance(mapped_host, str) and mapped_host.strip():
                mapped_host = re.sub(r"^https?://", "", mapped_host.strip()).strip("/")
                if "/" not in mapped_host and " " not in mapped_host:
                    self.api_base = f"https://{mapped_host}"
                    self.bootstrap_host = self.api_base
        except Exception:
            # Keep the configured official hosts; GSLB is an optional discovery step.
            return

    def bootstrap_token(self, force: bool = False) -> str:
        if self.bearer_token and not force:
            return self.bearer_token

        # First make an anonymous app request. MovieBox may issue a guest JWT
        # in the x-user response header; this is the handshake used by its client.
        url = (
            f"{self.bootstrap_host}/wefeed-mobile-bff/tab-operating"
            "?host=api.inmoviebox.com&page=1&pageSize=24&tabId=1"
        )
        headers = build_auth_headers(
            "GET", url, device_id=self.device_id, content_type="application/json;charset=UTF-8"
        )
        headers["x-client-status"] = "1"
        headers.pop("authorization", None)
        try:
            resp = self.session.get(url, headers=headers, timeout=25)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MovieBox guest bootstrap network error: {type(exc).__name__}"
            ) from exc

        x_user = next(
            (value for key, value in resp.headers.items() if key.lower() == "x-user"),
            None,
        )
        if x_user:
            try:
                user_data = json.loads(x_user)
                token = user_data.get("token") if isinstance(user_data, dict) else None
                if token:
                    self.bearer_token = token
                    return token
            except (TypeError, json.JSONDecodeError):
                pass

        # Some API versions return the guest token in visitor-login JSON.
        login_url = f"{self.api_base}/wefeed-mobile-bff/user-api/visitor-login?host=api.inmoviebox.com"
        body = json.dumps(
            {"device_id": self.device_id, "deviceId": self.device_id},
            separators=(",", ":"),
        )
        login_headers = build_auth_headers(
            "POST", login_url, body=body, device_id=self.device_id,
            content_type="application/json;charset=UTF-8"
        )
        login_headers["x-client-status"] = "1"
        login_headers.pop("authorization", None)
        try:
            login_resp = self.session.post(
                login_url, headers=login_headers, data=body.encode("utf-8"), timeout=25
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MovieBox visitor-login network error: {type(exc).__name__}"
            ) from exc

        if login_resp.status_code < 200 or login_resp.status_code >= 300:
            try:
                error_payload = login_resp.json()
            except Exception:
                error_payload = {}
            upstream_message = (
                error_payload.get("message") or error_payload.get("msg")
                if isinstance(error_payload, dict) else None
            )
            if login_resp.status_code == 403 and (
                "region" in (login_resp.text or "").lower()
                or "current region" in (login_resp.text or "").lower()
            ):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "MovieBox is unavailable from the current hosting region",
                        "upstream_status": 403,
                        "upstream_message": upstream_message or "Service not available in current region",
                        "api_host": urlsplit(self.api_base).netloc,
                    },
                )
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "MovieBox visitor-login rejected the request",
                    "upstream_status": login_resp.status_code,
                    "upstream_message": upstream_message,
                    "api_host": urlsplit(self.api_base).netloc,
                },
            )

        try:
            payload = login_resp.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="MovieBox visitor-login returned non-JSON data") from exc

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            x_user = next(
                (value for key, value in login_resp.headers.items() if key.lower() == "x-user"),
                None,
            )
            if x_user:
                try:
                    user_data = json.loads(x_user)
                    token = user_data.get("token") if isinstance(user_data, dict) else None
                except (TypeError, json.JSONDecodeError):
                    token = None
        if not token:
            raise HTTPException(
                status_code=502,
                detail="MovieBox guest bootstrap completed but no token was returned",
            )
        self.bearer_token = token
        return token

    def api_request(self, method: str, path_or_url: str, body_dict: Optional[Dict[str, Any]] = None, use_token: bool = True, retry_auth: bool = True) -> Dict[str, Any]:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            full_url = path_or_url
        else:
            full_url = f"{self.api_base}{path_or_url}"
        if use_token and not self.bearer_token:
            self.bootstrap_token()
        body_str = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else None
        # Android app adds the host query parameter before signing every native API call.
        parsed = urlsplit(full_url)
        if "host=" not in (parsed.query or ""):
            separator = "&" if parsed.query else "?"
            full_url = f"{full_url}{separator}host=api.inmoviebox.com"
        headers = build_auth_headers(
            method=method,
            url=full_url,
            body=body_str,
            token=self.bearer_token if use_token else None,
            device_id=self.device_id,
            content_type="application/json;charset=UTF-8",
        )
        data_bytes = body_str.encode("utf-8") if body_str is not None else None
        if method.upper() == "POST":
            resp = self.session.post(full_url, headers=headers, data=data_bytes, timeout=25)
        else:
            resp = self.session.get(full_url, headers=headers, timeout=25)
        if resp.status_code in (401, 441) and retry_auth:
            self.bootstrap_token(force=True)
            return self.api_request(method, path_or_url, body_dict, use_token, retry_auth=False)
        if resp.status_code != 200:
            # Keep upstream failures distinguishable from errors generated by this API.
            # Never log Authorization, cookies, signatures, or full request headers.
            upstream_host = urlsplit(full_url).netloc
            upstream_path = urlsplit(full_url).path
            raw_message = (resp.text or "").strip()
            safe_message = re.sub(
                r"(?i)(bearer\\s+)[^\\s,;]+|((?:token|authorization|cookie|signature|secret)[\"'=:\\s]+)[^&\"'\\s,;]+",
                r"\\1[REDACTED]",
                raw_message,
            )[:400]
            logger.warning(
                "MovieBox upstream request rejected: method=%s host=%s path=%s status=%s content_type=%s request_id=%s body=%s",
                method.upper(),
                upstream_host,
                upstream_path,
                resp.status_code,
                resp.headers.get("content-type", ""),
                resp.headers.get("x-request-id") or resp.headers.get("trace-id") or "",
                safe_message,
            )
            region_blocked = resp.status_code == 403 and any(
                term in raw_message.lower()
                for term in ("region", "current country", "not available in your location", "geo")
            )
            detail = {
                "message": "Upstream MovieBox request was forbidden" if resp.status_code == 403 else "Upstream MovieBox request failed",
                "upstream_status": resp.status_code,
                "upstream_host": upstream_host,
                "upstream_path": upstream_path,
                "upstream_message": safe_message or None,
                "request_id": resp.headers.get("x-request-id") or resp.headers.get("trace-id"),
            }
            raise HTTPException(status_code=503 if region_blocked else 502, detail=detail)
        return resp.json()

    def search(self, query: str, page: int = 1, per_page: int = 20) -> List[Dict[str, Any]]:
        payload = {"keyword": query, "page": page, "perPage": per_page}
        data = {}
        last_error = None
        for url in (
            "/wefeed-mobile-bff/subject-api/search/v2",
            "/wefeed-mobile-bff/subject-api/search",
        ):
            try:
                response_data = self.api_request("POST", url, body_dict=payload)
                data = response_data.get("data", {}) if isinstance(response_data, dict) else {}
                raw_items = data.get("items", []) or data.get("list", []) if isinstance(data, dict) else []
                if raw_items:
                    break
            except HTTPException as exc:
                last_error = exc
                continue
        else:
            if last_error:
                raise last_error
            raw_items = []
        raw_items = raw_items or (data.get("items", []) if isinstance(data, dict) else [])
        results = []
        for item in raw_items:
            sub = item.get("subject", item)
            results.append({
                "subject_id": str(sub.get("subjectId")) if sub.get("subjectId") is not None else None,
                "title": sub.get("title"),
                "type": "Movie" if sub.get("subjectType") == 1 else "Series",
                "subject_type": sub.get("subjectType"),
                "release_date": sub.get("releaseDate"),
                "imdb_rating": sub.get("imdbRatingValue"),
                "genre": sub.get("genre"),
                "cover": sub.get("cover", {}).get("url") if isinstance(sub.get("cover"), dict) else None,
                "detail_path": sub.get("detailPath"),
                "language": sub.get("language")
            })
        return [item for item in results if item.get("subject_id")]

    def get_details(self, subject_id: str) -> Dict[str, Any]:
        url = f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}"
        data = self.api_request("GET", url).get("data", {})
        detail_url_str = data.get("detailUrl", "")
        detail_path = detail_url_str.split("/")[-1] if "/" in detail_url_str else detail_url_str
        return {
            "subject_id": str(data.get("subjectId")),
            "title": data.get("title"),
            "subject_type": data.get("subjectType"),
            "type": "Movie" if data.get("subjectType") == 1 else "Series",
            "description": data.get("description"),
            "release_date": data.get("releaseDate"),
            "duration": data.get("duration"),
            "imdb_rating": data.get("imdbRatingValue"),
            "genre": data.get("genre"),
            "country": data.get("countryName"),
            "language": data.get("language"),
            "cover": data.get("cover", {}).get("url") if isinstance(data.get("cover"), dict) else None,
            "detail_path": detail_path,
            "dubs": [
                {
                    "subject_id": str(d.get("subjectId")),
                    "lang_name": d.get("lanName"),
                    "language": d.get("language"),
                }
                for d in data.get("dubs", [])
                if isinstance(d, dict)
            ]
        }

    def get_episodes(self, subject_id: str) -> List[Dict[str, Any]]:
        url = f"/wefeed-mobile-bff/subject-api/season-info?subjectId={subject_id}"
        res = self.api_request("GET", url)
        seasons = res.get("data", {}).get("seasons", [])
        season_list = []
        for s in seasons:
            season_list.append({
                "season": s.get("se", 1),
                "episode_count": s.get("maxEp", 1),
                "resolutions": s.get("resolutions", [])
            })
        return season_list
    def get_streams(self, subject_id: str, season: int = 0, episode: int = 0, detail_path: Optional[str] = None) -> List[Dict[str, Any]]:
        if not detail_path:
            try:
                details = self.get_details(subject_id)
                detail_path = details.get("detail_path", "")
            except Exception:
                detail_path = ""
        token = self.bootstrap_token()
        streams: List[Dict[str, Any]] = []
        seen_urls = set()

        for domain in WEB_DOMAINS:
            play_url = (
                f"{domain}/wefeed-h5api-bff/subject/play?"
                f"subjectId={subject_id}&se={season}&ep={episode}&detailPath={detail_path}&"
                f"streamSignType=1&supportCodecs%5Bhevc%5D=1&supportCodecs%5Bh264%5D=1"
            )
            referer = f"{domain}/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe=&detailEp=&lang=en"
            headers = {
                "User-Agent": WEB_USER_AGENT,
                "Referer": referer,
                "Accept": "application/json",
                "x-client-info": '{"timezone":"Asia/Calcutta"}',
                "x-request-lang": "en",
                "x-vip-restrict": "0",
                "x-no-high-risk-restrict": "0",
                "x-source": "moviebox",
                "Authorization": f"Bearer {token}",
            }
            try:
                resp = self.session.get(play_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json().get("data", {})
                raw_streams = data.get("streams", [])
                dash_items = data.get("dash", [])

                if raw_streams or dash_items:
                    for st in raw_streams:
                        url = st.get("url")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        res_str = str(st.get("resolutions", "unknown"))
                        size_bytes = int(st.get("size") or 0)
                        size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                        quality_val = 0
                        for q in (2160, 1440, 1080, 720, 480, 360, 240):
                            if str(q) in res_str:
                                quality_val = q
                                break
                        streams.append({
                            "source": f"Web CDN ({domain})",
                            "format": st.get("format", "MP4"),
                            "quality": f"{quality_val}p" if quality_val else res_str,
                            "quality_value": quality_val,
                            "resolution_tag": res_str,
                            "url": url,
                            "size_mb": round(size_mb, 1),
                            "codec": st.get("codecName"),
                            "duration_sec": st.get("duration"),
                            "headers": {
                                "User-Agent": "Mozilla/5.0",
                                "Referer": f"{domain}/",
                                "Origin": domain,
                            }
                        })

                    for d in dash_items:
                        url = d.get("url")
                        cookie = d.get("signCookie")
                        extracted_mpd = extract_policy_resource(cookie) if cookie else None
                        final_url = extracted_mpd or url
                        if final_url and final_url not in seen_urls:
                            seen_urls.add(final_url)
                            res_tag = str(d.get("resolutions", "adaptive"))
                            streams.append({
                                "source": f"Web DASH ({domain})",
                                "format": "DASH",
                                "quality": "1080p (Adaptive)" if "1080" in res_tag else "Adaptive",
                                "quality_value": 1080 if "1080" in res_tag else 999,
                                "resolution_tag": res_tag,
                                "url": final_url,
                                "size_mb": 0,
                                "codec": "hevc/h264",
                                "duration_sec": None,
                                "cookie": cookie,
                                "headers": {
                                    "User-Agent": "Mozilla/5.0",
                                    "Referer": f"{domain}/",
                                    "Origin": domain,
                                }
                            })
                    break
            except Exception:
                continue

        try:
            # Match Anime-'s current Android client: play-info/v2, 1-based
            # season/episode values, and streamList (with streams fallback).
            native_url = (
                f"/wefeed-mobile-bff/subject-api/play-info/v2"
                f"?subjectId={subject_id}&se={max(1, season)}&ep={max(1, episode)}"
                f"&host=api6.aoneroom.com"
            )
            native_response = self.api_request("GET", native_url)
            native_data = native_response.get("data", {}) if isinstance(native_response, dict) else {}
            raw_native_streams = (
                native_data.get("streamList")
                or native_data.get("streams")
                or []
            )
            global_cookie = (
                native_response.get("signCookie")
                or native_data.get("signCookie")
                or ""
            ) if isinstance(native_response, dict) else ""
            for st in raw_native_streams:
                if not isinstance(st, dict):
                    continue
                raw_url = st.get("url")
                if not raw_url or any(x in str(raw_url) for x in (
                    "b164fbfb4347792950bdfbfb563d39d9",
                    "please_update", "update_app"
                )):
                    continue
                cookie = st.get("signCookie") or global_cookie
                policy_url = extract_policy_resource(cookie) if cookie else None
                final_url = policy_url or raw_url
                if final_url in seen_urls:
                    continue
                seen_urls.add(final_url)
                res_str = str(st.get("resolutions") or st.get("resolution") or "")
                quality_val = 0
                for q in (2160, 1440, 1080, 720, 480, 360, 240):
                    if str(q) in res_str:
                        quality_val = q
                        break
                size_bytes = int(st.get("size") or 0)
                size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                streams.append({
                    "source": "Mobile Native CDN",
                    "format": "DASH" if (policy_url or ".mpd" in str(final_url)) else st.get("format", "MP4"),
                    "quality": f"{quality_val}p" if quality_val else (res_str or "Default"),
                    "quality_value": quality_val,
                    "resolution_tag": res_str,
                    "url": final_url,
                    "size_mb": round(size_mb, 1),
                    "codec": st.get("codecName") or st.get("codec"),
                    "duration_sec": st.get("duration"),
                    "headers": {
                        "User-Agent": MODERN_USER_AGENT,
                        "Referer": "https://api3.aoneroom.com/",
                    }
                })
        except Exception:
            pass

        streams.sort(key=lambda s: s["quality_value"], reverse=True)
        return streams

    def get_subtitles(self, subject_id: str, stream_id: Optional[str] = None, season: int = 0, episode: int = 0) -> List[Dict[str, Any]]:
        subs: List[Dict[str, Any]] = []
        seen_urls = set()

        if not stream_id:
            try:
                native_url = f"/wefeed-mobile-bff/subject-api/play-info?subjectId={subject_id}&se={season}&ep={episode}"
                raw_streams = self.api_request("GET", native_url).get("data", {}).get("streams", [])
                if raw_streams and raw_streams[0].get("id"):
                    stream_id = str(raw_streams[0]["id"])
            except Exception:
                pass

        def _add_sub(item: Dict[str, Any]):
            u = item.get("url")
            if u and u not in seen_urls:
                seen_urls.add(u)
                subs.append({
                    "id": str(item.get("id", "")),
                    "language": item.get("lanName") or item.get("language") or "English",
                    "code": item.get("lan", ""),
                    "url": u,
                    "size": item.get("size")
                })

        if stream_id:
            try:
                url = f"/wefeed-mobile-bff/subject-api/get-stream-captions?subjectId={subject_id}&streamId={stream_id}"
                data = self.api_request("GET", url).get("data", {})
                captions = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
                for s in captions:
                    if isinstance(s, dict):
                        _add_sub(s)
            except Exception:
                pass

        try:
            res_id = stream_id or "0"
            url = f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={res_id}&episode={episode}"
            data = self.api_request("GET", url).get("data", {})
            ext = data if isinstance(data, list) else data.get("extCaptions") or data.get("captions") or []
            for s in ext:
                if isinstance(s, dict):
                    _add_sub(s)
        except Exception:
            pass

        return subs

engine = MovieBoxEngine.get_instance()

@app.get("/", response_class=HTMLResponse)
def root():
    """Serve the lightweight MovieBox streaming web app."""
    from pathlib import Path
    from fastapi.responses import FileResponse

    page = Path(__file__).parent / "web" / "index.html"
    if not page.is_file():
        return HTMLResponse("<h1>MovieBox frontend not found</h1>", status_code=500)
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/search")
def search(q: str = Query(..., min_length=1), page: int = 1, per_page: int = 20):
    return {"query": q, "page": page, "results": engine.search(q, page=page, per_page=per_page)}

@app.get("/search/suggest")
def search_suggest(q: str = Query(..., min_length=1)):
    # Use the authenticated catalog search so suggestions contain real subject IDs.
    results = engine.search(q, page=1, per_page=10)
    suggestions = [
        {
            "title": item.get("title"),
            "slug": item.get("detail_path"),
            "subject_id": item.get("subject_id"),
        }
        for item in results
        if item.get("subject_id") and item.get("subject_id") != "None"
    ]
    return {"suggestions": suggestions}

@app.get("/detail/{subject_id}")
def get_detail(subject_id: str):
    return engine.get_details(subject_id)

@app.get("/episodes/{subject_id}")
def get_episodes(subject_id: str):    return {"subject_id": subject_id, "seasons": engine.get_episodes(subject_id)}

@app.get("/api/stream/{subject_id}")
def get_streams(subject_id: str, se: int = 0, ep: int = 0, detail_path: Optional[str] = None):
    streams = engine.get_streams(subject_id, season=se, episode=ep, detail_path=detail_path)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(streams),
        "streams": streams
    }

@app.get("/api/stream/{subject_id}/captions")
def get_captions(subject_id: str, se: int = 0, ep: int = 0, stream_id: Optional[str] = None):
    subs = engine.get_subtitles(subject_id, stream_id=stream_id, season=se, episode=ep)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(subs),
        "captions": subs
    }


@app.get("/api/stream/{subject_id}/sources")
def get_stream_sources(
    subject_id: str,
    se: int = 0,
    ep: int = 0,
    format: str = Query("all", pattern="^(all|mp4|hls|dash)$"),
    quality: Optional[int] = Query(None, ge=144, le=4320),
):
    """Return normalized playback sources without rewriting protected CDN URLs."""
    streams = engine.get_streams(subject_id, season=se, episode=ep)
    normalized = [classify_stream(item) for item in streams]
    selected = filter_streams(normalized, media_format=format, quality=quality)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "format_filter": format,
        "quality_filter": quality,
        "count": len(selected),
        "recommended": next(
            (item for item in selected if item.get("direct_playback") and item.get("codec_family") == "h264"),
            selected[0] if selected else None,
        ),
        "sources": selected,
        "playback_notes": {
            "mp4": "For VLC or a basic HTML5 player, prefer a direct MP4 source. The source may still require the provider's allowed request headers.",
            "hls": "Use an HLS-capable player such as hls.js; the CDN must permit CORS.",
            "dash": "Use a DASH-capable player such as dash.js. A .mpd manifest is not a single MP4 file.",
            "codec": "H.264 has broader browser compatibility than H.265/HEVC. Browser support also depends on device, browser, and CDN CORS policy.",
        },
    }


@app.get("/api/stream/{subject_id}/vlc")
def get_vlc_sources(subject_id: str, se: int = 0, ep: int = 0):
    """List direct MP4 candidates for external players; does not proxy or bypass access controls."""
    streams = engine.get_streams(subject_id, season=se, episode=ep)
    normalized = [classify_stream(item) for item in streams]
    mp4_sources = filter_streams(normalized, media_format="mp4")
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "count": len(mp4_sources),
        "sources": mp4_sources,
        "note": "Choose a direct MP4 source. DASH .mpd manifests can contain multiple representations and are not equivalent to a single media file. If a provider requires authorization headers or denies the current region, this API cannot remove that restriction.",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "MovieBox API",
        "version": "2.6.0",
        "features": ["search", "metadata", "episodes", "normalized-stream-sources", "direct-mp4-list", "captions"],
    }


@app.get("/movies")
def get_movies(page: int = 1, per_page: int = 24, sort: str = "RECOMMEND"):
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 2, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    return {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }

@app.get("/tv-series")
def get_tv_series(page: int = 1, per_page: int = 24, sort: str = "RECOMMEND"):
    url = f"{API_BASE}/subject/filter"
    payload = {"tabId": 5, "filter": {"sort": sort, "genre": "ALL", "country": "ALL", "year": "ALL", "language": "ALL"}, "page": page, "perPage": per_page}
    resp = engine.session.post(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, json=payload, timeout=20)
    data = resp.json().get("data", {})
    raw = data.get("items") or data.get("subjects") or []
    return {
        "page": page,
        "per_page": per_page,
        "total": data.get("pager", {}).get("totalCount") or len(raw),
        "items": [
            {
                "title": s.get("title"),
                "subject_id": str(s.get("subjectId")),
                "detail_path": s.get("detailPath"),
                "cover": s.get("cover", {}).get("url") if isinstance(s.get("cover"), dict) else None,
                "rating": s.get("imdbRatingValue"),
                "year": s.get("releaseDate", "")[:4] if s.get("releaseDate") else None
            }
            for s in raw
        ]
    }

@app.get("/home")
def get_home():
    url = f"{API_BASE}/home?host=moviebox.ph"
    resp = engine.session.get(url, headers={"User-Agent": WEB_USER_AGENT, "Referer": "https://moviebox.ph/"}, timeout=20)
    return resp.json()


@app.get("/api/stream/{subject_id}/diagnostics")
def diagnose_streams(subject_id: str, se: int = 0, ep: int = 0):
    """Check provider-returned stream URLs with a one-byte Range request.
    This is diagnostic only: it does not proxy media or bypass provider controls.
    """
    from urllib.parse import urlsplit

    results = []
    candidates = engine.get_streams(subject_id, season=se, episode=ep)
    for index, stream in enumerate(candidates[:12]):
        raw_url = str(stream.get("url") or "")
        parsed = urlsplit(raw_url)
        item = {
            "index": index,
            "quality": stream.get("quality"),
            "format": stream.get("format"),
            "codec": stream.get("codec"),
            "host": parsed.hostname,
            "status": None,
            "content_type": None,
            "accept_ranges": None,
            "range_supported": False,
            "cors": None,
            "playable_candidate": False,
            "error": None,
        }
        if parsed.scheme != "https" or not parsed.hostname:
            item["error"] = "Rejected non-HTTPS or invalid provider URL"
            results.append(item)
            continue
        try:
            request_headers = dict(stream.get("headers") or {})
            request_headers["Range"] = "bytes=0-0"
            request_headers["Accept-Encoding"] = "identity"
            response = engine.session.get(
                raw_url,
                headers=request_headers,
                timeout=12,
                stream=True,
            )
            item["status"] = int(response.status_code)
            item["content_type"] = response.headers.get("content-type")
            item["accept_ranges"] = response.headers.get("accept-ranges")
            item["cors"] = response.headers.get("access-control-allow-origin")
            item["range_supported"] = (
                response.status_code == 206
                or "bytes" in str(item["accept_ranges"] or "").lower()
            )
            item["playable_candidate"] = (
                response.status_code in (200, 206)
                and (
                    "video/" in str(item["content_type"] or "").lower()
                    or "application/octet-stream" in str(item["content_type"] or "").lower()
                    or "dash+xml" in str(item["content_type"] or "").lower()
                    or "mpegurl" in str(item["content_type"] or "").lower()
                )
            )
            response.close()
        except Exception as exc:
            item["error"] = type(exc).__name__ + ": " + str(exc)[:180]
        results.append(item)
    return {
        "subject_id": subject_id,
        "season": se,
        "episode": ep,
        "checked": len(results),
        "results": results,
        "note": "A successful server-side probe does not guarantee browser playback; the CDN must also allow browser CORS and required codecs.",
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)