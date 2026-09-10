"""Curated public website destinations and conservative alias resolution."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


WEB_DESTINATIONS: tuple[dict[str, Any], ...] = (
    # Chinese video, community, shopping, and media services.
    {"id": "bilibili", "title": "哔哩哔哩", "url": "https://www.bilibili.com/", "aliases": ("bilibili", "哔哩哔哩", "哔哩哔哩弹幕网", "b站", "小破站")},
    {"id": "acfun", "title": "AcFun", "url": "https://www.acfun.cn/", "aliases": ("acfun", "a站")},
    {"id": "zhihu", "title": "知乎", "url": "https://www.zhihu.com/", "aliases": ("知乎", "zhihu")},
    {"id": "weibo", "title": "微博", "url": "https://weibo.com/", "aliases": ("微博", "新浪微博", "weibo")},
    {"id": "baidu", "title": "百度", "url": "https://www.baidu.com/", "aliases": ("百度", "百度一下", "baidu")},
    {"id": "douban", "title": "豆瓣", "url": "https://www.douban.com/", "aliases": ("豆瓣", "douban")},
    {"id": "tieba", "title": "百度贴吧", "url": "https://tieba.baidu.com/", "aliases": ("百度贴吧", "贴吧", "tieba")},
    {"id": "xiaohongshu", "title": "小红书", "url": "https://www.xiaohongshu.com/", "aliases": ("小红书", "xiaohongshu", "rednote")},
    {"id": "douyin", "title": "抖音", "url": "https://www.douyin.com/", "aliases": ("抖音", "douyin")},
    {"id": "kuaishou", "title": "快手", "url": "https://www.kuaishou.com/", "aliases": ("快手", "kuaishou")},
    {"id": "iqiyi", "title": "爱奇艺", "url": "https://www.iqiyi.com/", "aliases": ("爱奇艺", "iqiyi")},
    {"id": "youku", "title": "优酷", "url": "https://www.youku.com/", "aliases": ("优酷", "youku")},
    {"id": "tencent_video", "title": "腾讯视频", "url": "https://v.qq.com/", "aliases": ("腾讯视频", "腾讯视频网页版", "vqq")},
    {"id": "netease_music", "title": "网易云音乐", "url": "https://music.163.com/", "aliases": ("网易云音乐", "网易云", "云村", "music163")},
    {"id": "qq_music", "title": "QQ音乐", "url": "https://y.qq.com/", "aliases": ("qq音乐", "qqmusic")},
    {"id": "taobao", "title": "淘宝", "url": "https://www.taobao.com/", "aliases": ("淘宝", "淘宝网", "taobao")},
    {"id": "tmall", "title": "天猫", "url": "https://www.tmall.com/", "aliases": ("天猫", "天猫商城", "tmall")},
    {"id": "jd", "title": "京东", "url": "https://www.jd.com/", "aliases": ("京东", "京东商城", "京东网", "jd")},
    {"id": "pinduoduo", "title": "拼多多", "url": "https://www.pinduoduo.com/", "aliases": ("拼多多", "pdd", "pinduoduo")},
    {"id": "meituan", "title": "美团", "url": "https://www.meituan.com/", "aliases": ("美团", "meituan")},
    {"id": "moegirl", "title": "萌娘百科", "url": "https://moegirl.uk/", "aliases": ("萌娘百科", "萌百", "moegirl")},
    {"id": "bangumi", "title": "Bangumi 番组计划", "url": "https://bgm.tv/", "aliases": ("bangumi", "番组计划", "bgm")},
    {"id": "csdn", "title": "CSDN", "url": "https://www.csdn.net/", "aliases": ("csdn",)},
    {"id": "cnblogs", "title": "博客园", "url": "https://www.cnblogs.com/", "aliases": ("博客园", "cnblogs")},
    {"id": "gitee", "title": "Gitee", "url": "https://gitee.com/", "aliases": ("gitee", "码云")},
    {"id": "leetcode_cn", "title": "力扣", "url": "https://leetcode.cn/", "aliases": ("力扣", "leetcode中国", "leetcode中文")},
    {"id": "v2ex", "title": "V2EX", "url": "https://www.v2ex.com/", "aliases": ("v2ex",)},

    # Research, development, and AI services.
    {"id": "github", "title": "GitHub", "url": "https://github.com/", "aliases": ("github", "github官网")},
    {"id": "gitlab", "title": "GitLab", "url": "https://gitlab.com/", "aliases": ("gitlab",)},
    {"id": "stackoverflow", "title": "Stack Overflow", "url": "https://stackoverflow.com/", "aliases": ("stackoverflow", "stack overflow")},
    {"id": "arxiv", "title": "arXiv", "url": "https://arxiv.org/", "aliases": ("arxiv", "arxiv官网")},
    {"id": "openreview", "title": "OpenReview", "url": "https://openreview.net/", "aliases": ("openreview",)},
    {"id": "huggingface", "title": "Hugging Face", "url": "https://huggingface.co/", "aliases": ("huggingface", "hugging face", "hf社区", "抱抱脸")},
    {"id": "kaggle", "title": "Kaggle", "url": "https://www.kaggle.com/", "aliases": ("kaggle",)},
    {"id": "openai", "title": "OpenAI", "url": "https://openai.com/", "aliases": ("openai", "openai官网")},
    {"id": "chatgpt", "title": "ChatGPT", "url": "https://chatgpt.com/", "aliases": ("chatgpt", "chat gpt", "gpt网页版")},
    {"id": "deepseek", "title": "DeepSeek", "url": "https://www.deepseek.com/", "aliases": ("deepseek", "deep seek", "深度求索")},
    {"id": "claude", "title": "Claude", "url": "https://claude.ai/", "aliases": ("claude", "claude网页版")},
    {"id": "gemini", "title": "Gemini", "url": "https://gemini.google.com/", "aliases": ("gemini", "谷歌gemini")},

    # International search, social, video, game, and reference services.
    {"id": "google", "title": "Google", "url": "https://www.google.com/", "aliases": ("google", "谷歌", "谷歌搜索")},
    {"id": "bing", "title": "Bing", "url": "https://www.bing.com/", "aliases": ("bing", "必应", "必应搜索")},
    {"id": "duckduckgo", "title": "DuckDuckGo", "url": "https://duckduckgo.com/", "aliases": ("duckduckgo", "duck duck go", "ddg")},
    {"id": "wikipedia", "title": "Wikipedia", "url": "https://www.wikipedia.org/", "aliases": ("wikipedia", "维基百科", "维基")},
    {"id": "youtube", "title": "YouTube", "url": "https://www.youtube.com/", "aliases": ("youtube", "油管", "yt")},
    {"id": "twitter", "title": "X / Twitter", "url": "https://x.com/", "aliases": ("twitter", "推特", "x网站", "x.com")},
    {"id": "reddit", "title": "Reddit", "url": "https://www.reddit.com/", "aliases": ("reddit",)},
    {"id": "instagram", "title": "Instagram", "url": "https://www.instagram.com/", "aliases": ("instagram", "ins", "ig")},
    {"id": "facebook", "title": "Facebook", "url": "https://www.facebook.com/", "aliases": ("facebook", "脸书", "fb")},
    {"id": "linkedin", "title": "LinkedIn", "url": "https://www.linkedin.com/", "aliases": ("linkedin", "领英")},
    {"id": "tiktok", "title": "TikTok", "url": "https://www.tiktok.com/", "aliases": ("tiktok", "tik tok")},
    {"id": "twitch", "title": "Twitch", "url": "https://www.twitch.tv/", "aliases": ("twitch",)},
    {"id": "niconico", "title": "Niconico", "url": "https://www.nicovideo.jp/", "aliases": ("niconico", "nico nico", "n站")},
    {"id": "pixiv", "title": "Pixiv", "url": "https://www.pixiv.net/", "aliases": ("pixiv", "p站")},
    {"id": "steam", "title": "Steam", "url": "https://store.steampowered.com/", "aliases": ("steam", "steam商店")},
    {"id": "discord", "title": "Discord", "url": "https://discord.com/", "aliases": ("discord",)},
)


def normalize_destination_alias(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s_\-—·.,，。!?！？:：/\\'\"“”‘’（）()]+", "", text)


_ALIAS_INDEX: dict[str, dict[str, Any]] = {}
for _destination in WEB_DESTINATIONS:
    for _alias in _destination.get("aliases", ()):
        _key = normalize_destination_alias(str(_alias))
        if _key and _key not in _ALIAS_INDEX:
            _ALIAS_INDEX[_key] = _destination


def resolve_web_destination_alias(value: str) -> dict[str, str] | None:
    key = normalize_destination_alias(value)
    destination = _ALIAS_INDEX.get(key)
    if not destination:
        return None
    matched_alias = next(
        (
            str(alias)
            for alias in destination.get("aliases", ())
            if normalize_destination_alias(str(alias)) == key
        ),
        "",
    )
    return {
        "id": str(destination.get("id") or ""),
        "title": str(destination.get("title") or ""),
        "url": str(destination.get("url") or ""),
        "matchedAlias": matched_alias,
    }


def public_destination_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "aliases": [str(alias) for alias in item.get("aliases", ())],
        }
        for item in WEB_DESTINATIONS
    ]
