"""固定的官方网页根域名登记表。

该模块只包含来源身份判断的无副作用规则，供搜索结果分级和读页排序
共同使用；它不依赖 safe_web，避免网络传输层与研究来源层互相导入。
"""

from __future__ import annotations


KNOWN_OFFICIAL_WEB_ROOTS = frozenset({
    "adobe.com",
    "android.com",
    "apache.org",
    "apple.com",
    "deepmind.google",
    "deepseek.com",
    "docker.com",
    "go.dev",
    "google.com",
    "google.dev",
    "kubernetes.io",
    "microsoft.com",
    "mozilla.org",
    "nodejs.org",
    "npmjs.com",
    "openai.com",
    "oracle.com",
    "python.org",
    "pytorch.org",
    "rust-lang.org",
    "tensorflow.org",
    # Maintained first-party roots for public institutions.  The matcher below
    # requires an exact root or a subdomain suffix, so lookalike domains such
    # as ``oecd.org.example`` and ``europa.eu.example`` do not qualify.
    "europa.eu",
    "oecd.org",
    "un.org",
})


def known_official_web_domain(domain: str) -> bool:
    """判断域名是否属于固定的官方根域名集合。"""
    value = str(domain or "").strip(".").lower()
    return any(
        value == root or value.endswith(f".{root}")
        for root in KNOWN_OFFICIAL_WEB_ROOTS
    )


__all__ = ["KNOWN_OFFICIAL_WEB_ROOTS", "known_official_web_domain"]
