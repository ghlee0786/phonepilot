"""YouTube Data API v3 read-only research client and channel analysis (PRD 16.3).

Uses only public read endpoints; nothing here likes, subscribes, comments or uploads. The API key
is revealed only while a single request is being sent, and errors never echo the request URL.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from phonepilot.errors import PhonePilotError
from phonepilot.security.secrets import Secret

BASE_URL = "https://www.googleapis.com/youtube/v3"
HOST = "www.googleapis.com"
KST = timezone(timedelta(hours=9), "KST")  # Korea has no DST

QUOTA_COST = {"search": 100, "channels": 1, "playlistItems": 1, "videos": 1, "videoCategories": 1}
DEFAULT_DAILY_QUOTA = 10_000
MAX_SEARCH_CALLS = 5  # per research run (PRD 16.3)
SHORTS_MAX_S = 180
OUTLIER_FACTOR = 3


def _chunks(items: list[str], n: int = 50) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class YouTubeClient:
    """Async read-only client with an estimated quota counter."""

    def __init__(
        self,
        api_key: Secret,
        client: httpx.AsyncClient | None = None,
        daily_quota: int = DEFAULT_DAILY_QUOTA,
        max_search_calls: int = MAX_SEARCH_CALLS,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self.daily_quota = daily_quota
        self.max_search_calls = max_search_calls
        self.quota_used = 0
        self.search_calls = 0

    def __repr__(self) -> str:
        return f"YouTubeClient(api_key={self._api_key!r}, quota_used={self.quota_used})"

    __str__ = __repr__

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> YouTubeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @staticmethod
    def estimate(resource: str, calls: int = 1) -> int:
        return QUOTA_COST.get(resource, 1) * calls

    def reset_research(self) -> None:
        """Start a new research run (resets the per-run search.list cap)."""
        self.search_calls = 0

    async def _get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        if HOST not in [h.lower() for h in self._api_key.meta.allowed_hosts]:
            raise PhonePilotError("SECRET_SCOPE_DENIED", f"{self._api_key.name} is not allowed for host {HOST}")
        cost = self.estimate(resource)
        if self.quota_used + cost > self.daily_quota:
            raise PhonePilotError("LIMIT_EXCEEDED", f"estimated YouTube quota would exceed {self.daily_quota}")
        if resource == "search":
            if self.search_calls >= self.max_search_calls:
                raise PhonePilotError("LIMIT_EXCEEDED", f"search.list is capped at {self.max_search_calls} per research run")
            self.search_calls += 1
        self.quota_used += cost
        try:
            with self._api_key.reveal() as buf:
                resp = await self._client.get(f"{BASE_URL}/{resource}", params={**params, "key": buf.decode("utf-8")})
        except httpx.HTTPError as e:
            raise PhonePilotError("INTEGRATION_ERROR", f"YouTube {resource} network error: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise self._error(resource, resp)
        return resp.json()

    def _error(self, resource: str, resp: httpx.Response) -> PhonePilotError:
        reason, message = "", ""
        try:
            err = resp.json().get("error", {})
            message = str(err.get("message", ""))
            reason = str((err.get("errors") or [{}])[0].get("reason", ""))
        except (ValueError, AttributeError, IndexError):
            pass
        if self._api_key.contained_in(message):
            message = "(redacted)"
        text = f"YouTube {resource} HTTP {resp.status_code} {reason}: {message[:200]}"
        if reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            return PhonePilotError("RATE_LIMITED", text)
        if resp.status_code in (401, 403) or reason in {"keyInvalid", "keyExpired", "accessNotConfigured"}:
            return PhonePilotError("INTEGRATION_AUTH_FAILED", text)
        return PhonePilotError("INTEGRATION_ERROR", text)

    async def search_channels(self, q: str, region: str = "KR", max_results: int = 10) -> list[dict[str, Any]]:
        data = await self._get(
            "search",
            {"part": "snippet", "type": "channel", "q": q, "regionCode": region, "maxResults": min(max_results, 50)},
        )
        return data.get("items", [])

    async def channels(self, ids: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for chunk in _chunks(ids):
            data = await self._get("channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)})
            items.extend(data.get("items", []))
        return items

    async def recent_videos(self, uploads_playlist_id: str, max_results: int = 50) -> list[dict[str, Any]]:
        data = await self._get(
            "playlistItems",
            {"part": "snippet,contentDetails", "playlistId": uploads_playlist_id, "maxResults": min(max_results, 50)},
        )
        return data.get("items", [])

    async def videos(self, ids: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for chunk in _chunks(ids):
            data = await self._get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)})
            items.extend(data.get("items", []))
        return items

    async def categories(self, region: str = "KR") -> list[dict[str, Any]]:
        data = await self._get("videoCategories", {"part": "snippet", "regionCode": region})
        return data.get("items", [])


# --------------------------------------------------------------------------- analysis

_DURATION_RE = re.compile(
    r"^P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def parse_iso8601_duration(value: str | None) -> float | None:
    """'PT1M5S' -> 65.0. Returns None if unparseable."""
    if not value:
        return None
    m = _DURATION_RE.match(value.strip())
    if not m or value.strip() in ("P", "PT"):
        return None
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return g["w"] * 604800 + g["d"] * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _field(v: dict[str, Any], section: str, key: str, flat: str) -> Any:
    """Read a field from a videos.list item, falling back to a flat dict key."""
    sec = v.get(section)
    if isinstance(sec, dict) and key in sec:
        return sec[key]
    return v.get(flat)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def analyze_channel(videos: list[dict[str, Any]], subscribers: int | None = None) -> dict[str, Any]:
    """Compute PRD 16.3 metrics from videos.list items (or flat dicts with the same data)."""
    rows = []
    for v in videos:
        rows.append(
            {
                "id": v.get("id") if isinstance(v.get("id"), str) else v.get("video_id"),
                "title": str(_field(v, "snippet", "title", "title") or ""),
                "published": _parse_time(_field(v, "snippet", "publishedAt", "published_at")),
                "duration_s": parse_iso8601_duration(_field(v, "contentDetails", "duration", "duration")),
                "views": _int(_field(v, "statistics", "viewCount", "views")),
            }
        )

    # Upload interval (median days between consecutive posts)
    times = sorted((r["published"] for r in rows if r["published"]), reverse=True)
    gaps = [(a - b).total_seconds() / 86400 for a, b in zip(times, times[1:])]
    interval = round(statistics.median(gaps), 2) if gaps else None

    # Weekday x hour heatmap in KST (weekday 0 = Monday)
    heat = [[0] * 24 for _ in range(7)]
    for t in times:
        k = t.astimezone(KST)
        heat[k.weekday()][k.hour] += 1

    # Durations
    durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
    n_dur = len(durations)
    shorts = sum(1 for d in durations if d <= SHORTS_MAX_S)
    shorts60 = sum(1 for d in durations if d <= 60)

    # Views and outliers
    views = [r["views"] for r in rows if r["views"] is not None]
    median_views = statistics.median(views) if views else None
    outliers = []
    if median_views:
        for r in rows:
            if r["views"] is not None and r["views"] >= OUTLIER_FACTOR * median_views:
                outliers.append(
                    {"id": r["id"], "title": r["title"], "views": r["views"], "ratio": round(r["views"] / median_views, 2)}
                )
        outliers.sort(key=lambda o: o["views"], reverse=True)

    views_per_sub = None
    if subscribers:
        recent = [r["views"] for r in sorted(rows, key=lambda r: r["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True) if r["views"] is not None][:10]
        if recent:
            views_per_sub = round(statistics.median(recent) / subscribers, 4)

    # Titles
    titles = [r["title"] for r in rows if r["title"]]
    n_t = len(titles)
    words: Counter[str] = Counter()
    for t in titles:
        for w in _WORD_RE.findall(t.lower()):
            w = w.strip("_")
            if len(w) >= 2 and not w.isdigit():
                words[w] += 1
    top_words = sorted(words.items(), key=lambda kv: (-kv[1], kv[0]))[:20]

    return {
        "video_count": len(rows),
        "upload_interval_median_days": interval,
        "weekday_hour_kst": heat,
        "shorts_ratio": round(shorts / n_dur, 4) if n_dur else None,
        "shorts60_ratio": round(shorts60 / n_dur, 4) if n_dur else None,
        "longform_ratio": round((n_dur - shorts) / n_dur, 4) if n_dur else None,
        "median_duration_s": statistics.median(durations) if durations else None,
        "median_views": median_views,
        "views_per_subscriber": views_per_sub,
        "outliers": outliers,
        "title_avg_length": round(sum(len(t) for t in titles) / n_t, 2) if n_t else None,
        "title_digit_ratio": round(sum(1 for t in titles if any(c.isdigit() for c in t)) / n_t, 4) if n_t else None,
        "title_question_ratio": round(sum(1 for t in titles if t.rstrip().endswith(("?", "？"))) / n_t, 4) if n_t else None,
        "top_words": [list(kv) for kv in top_words],
    }
