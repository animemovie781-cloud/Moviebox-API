"""Safe stream metadata normalization and filtering helpers.

These helpers classify URLs returned by the upstream provider. They do not
manufacture manifests, alter signed URLs, spoof entitlements, or bypass region
and CDN access controls.
"""
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


def classify_stream(stream: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(stream or {})
    url = str(item.get("url") or "")
    path = urlsplit(url).path.lower() if url else ""
    declared = str(item.get("format") or "").lower()
    codec = str(item.get("codec") or item.get("codecName") or "").lower()

    if path.endswith((".mp4", ".m4v", ".mov")) or declared in {"mp4", "m4v"}:
        family = "mp4"
    elif path.endswith(".m3u8") or "hls" in declared:
        family = "hls"
    elif path.endswith(".mpd") or "dash" in declared:
        family = "dash"
    else:
        family = "unknown"

    if "265" in codec or "hevc" in codec or "h265" in codec:
        codec_family = "h265"
    elif "264" in codec or "avc" in codec or "h264" in codec:
        codec_family = "h264"
    else:
        codec_family = "unknown"

    item["format_family"] = family
    item["codec_family"] = codec_family
    item["direct_playback"] = family == "mp4"
    item["requires_manifest_player"] = family in {"hls", "dash"}
    item["has_request_headers"] = bool(item.get("headers"))
    return item


def filter_streams(
    streams: List[Dict[str, Any]],
    media_format: str = "all",
    quality: Optional[int] = None,
) -> List[Dict[str, Any]]:
    allowed = {"all", "mp4", "hls", "dash"}
    if media_format not in allowed:
        raise ValueError("format must be one of: all, mp4, hls, dash")

    result = [
        dict(item)
        for item in streams
        if media_format == "all" or item.get("format_family") == media_format
    ]
    if quality is not None:
        result = [
            item for item in result
            if int(item.get("quality_value") or 0) == quality
        ]
    result.sort(
        key=lambda item: (
            1 if item.get("direct_playback") else 0,
            1 if item.get("codec_family") == "h264" else 0,
            int(item.get("quality_value") or 0),
        ),
        reverse=True,
    )
    return result
