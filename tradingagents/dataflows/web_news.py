from __future__ import annotations

import json
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from parsel import Selector

_DEFAULT_TIMEOUT = 10
_MIN_TITLE_LENGTH = 10
_USER_AGENT = "TradingAgents/1.0 (+https://github.com/TauricResearch/TradingAgents)"
_ARTICLE_TYPES = {"NewsArticle", "Article", "Report", "LiveBlogPosting", "BlogPosting"}


def _fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def _parse_datetime(value: str | None) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _walk_json(data) -> Iterable[dict]:
    if isinstance(data, dict):
        yield data
        for item in data.values():
            yield from _walk_json(item)
    elif isinstance(data, list):
        for item in data:
            yield from _walk_json(item)


def _normalize_url(base_url: str, url: str | None) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    full_url = urljoin(base_url, url)
    parsed = urlparse(full_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return full_url


def _build_article(
    node: dict,
    *,
    base_url: str,
    source: str,
    allowed_domains: Iterable[str],
    allowed_paths: Iterable[str],
) -> Optional[dict]:
    title = node.get("headline") or node.get("name")
    url = (
        node.get("url")
        or node.get("@id")
        or node.get("mainEntityOfPage")
        or node.get("contentUrl")
    )
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url")
    url = _normalize_url(base_url, url)
    if not title or not url:
        return None
    if not _is_allowed_url(url, allowed_domains, allowed_paths):
        return None
    summary = node.get("description") or node.get("abstract") or ""
    published = _parse_datetime(
        node.get("datePublished") or node.get("dateCreated") or node.get("dateModified")
    )
    return {
        "title": str(title).strip(),
        "summary": str(summary).strip(),
        "url": url,
        "published": published,
        "source": source,
    }


def _is_allowed_url(url: str, domains: Iterable[str], path_fragments: Iterable[str]) -> bool:
    parsed = urlparse(url)
    if not any(parsed.netloc.endswith(domain) for domain in domains):
        return False
    if not path_fragments:
        return True
    return any(fragment in parsed.path for fragment in path_fragments)


def _extract_json_ld_articles(
    selector: Selector,
    *,
    base_url: str,
    source: str,
    allowed_domains: Iterable[str],
    allowed_paths: Iterable[str],
) -> list[dict]:
    articles: list[dict] = []
    scripts = selector.css('script[type="application/ld+json"]::text').getall()
    for script in scripts:
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        for node in _walk_json(data):
            node_type = node.get("@type")
            node_types = {node_type} if isinstance(node_type, str) else set(node_type or [])
            if node_types & _ARTICLE_TYPES:
                article = _build_article(
                    node,
                    base_url=base_url,
                    source=source,
                    allowed_domains=allowed_domains,
                    allowed_paths=allowed_paths,
                )
                if article:
                    articles.append(article)
            if node_types == {"ListItem"} and isinstance(node.get("item"), str):
                article = _build_article(
                    {"name": node.get("name"), "url": node.get("item")},
                    base_url=base_url,
                    source=source,
                    allowed_domains=allowed_domains,
                    allowed_paths=allowed_paths,
                )
                if article:
                    articles.append(article)
            if not node_types and node.get("url") and (node.get("headline") or node.get("name")):
                article = _build_article(
                    node,
                    base_url=base_url,
                    source=source,
                    allowed_domains=allowed_domains,
                    allowed_paths=allowed_paths,
                )
                if article:
                    articles.append(article)
    return articles


def _extract_link_articles(
    selector: Selector,
    *,
    base_url: str,
    source: str,
    allowed_domains: Iterable[str],
    allowed_paths: Iterable[str],
) -> list[dict]:
    articles: list[dict] = []
    for link in selector.css("article a"):
        href = link.attrib.get("href")
        url = _normalize_url(base_url, href)
        if not url or not _is_allowed_url(url, allowed_domains, allowed_paths):
            continue
        title = " ".join(text.strip() for text in link.css("::text").getall()).strip()
        if len(title) < _MIN_TITLE_LENGTH:
            continue
        articles.append(
            {
                "title": title,
                "summary": "",
                "url": url,
                "published": None,
                "source": source,
            }
        )
    return articles


def _collect_source_articles(
    *,
    source: str,
    url: str,
    allowed_domains: Iterable[str],
    allowed_paths: Iterable[str],
) -> list[dict]:
    html = _fetch_html(url)
    selector = Selector(text=html)
    articles = _extract_json_ld_articles(
        selector,
        base_url=url,
        source=source,
        allowed_domains=allowed_domains,
        allowed_paths=allowed_paths,
    )
    if articles:
        return articles
    return _extract_link_articles(
        selector,
        base_url=url,
        source=source,
        allowed_domains=allowed_domains,
        allowed_paths=allowed_paths,
    )


def _format_articles(articles: list[dict], start_date: str, curr_date: str) -> str:
    if not articles:
        return f"No global news found between {start_date} and {curr_date}"
    lines = [f"## Global Market News, from {start_date} to {curr_date}:\n"]
    for article in articles:
        date_label = ""
        if article.get("published"):
            date_label = f", {article['published'].date()}"
        lines.append(
            f"### {article['title']} (source: {article['source']}{date_label})"
        )
        if article.get("summary"):
            lines.append(article["summary"])
        lines.append(f"Link: {article['url']}\n")
    return "\n".join(lines)


def get_global_news_web(
    curr_date: str,
    look_back_days: int = 5,
    limit: int = 5,
) -> str:
    """Aggregate global market news from Reuters Finance and Bloomberg Markets."""
    try:
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - timedelta(days=look_back_days)
    except ValueError as exc:
        return f"Invalid date format: {exc}"

    sources = [
        {
            "source": "Reuters",
            "url": "https://www.reuters.com/business/finance/",
            "domains": ("reuters.com",),
            "paths": ("/business/finance/", "/markets/"),
        },
        {
            "source": "Bloomberg",
            "url": "https://www.bloomberg.com/markets",
            "domains": ("bloomberg.com",),
            "paths": ("/news/", "/markets/"),
        },
    ]

    articles: list[dict] = []
    errors: list[str] = []
    for source in sources:
        try:
            articles.extend(
                _collect_source_articles(
                    source=source["source"],
                    url=source["url"],
                    allowed_domains=source["domains"],
                    allowed_paths=source["paths"],
                )
            )
        except Exception as exc:
            errors.append(f"{source['source']}: {exc}")

    unique: dict[str, dict] = {}
    for article in articles:
        key = article["url"]
        if key not in unique:
            unique[key] = article

    filtered = []
    for article in unique.values():
        published = article.get("published")
        if isinstance(published, datetime):
            published_naive = published.replace(tzinfo=None)
            if not (start_dt <= published_naive <= curr_dt):
                continue
        filtered.append(article)

    filtered.sort(
        key=lambda item: item.get("published") or datetime.min, reverse=True
    )
    filtered = filtered[:limit]
    result = _format_articles(
        filtered,
        start_dt.strftime("%Y-%m-%d"),
        curr_dt.strftime("%Y-%m-%d"),
    )
    if errors and not filtered:
        return f"{result}\n\nErrors: " + "; ".join(errors)
    return result
