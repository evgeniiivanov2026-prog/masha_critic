#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НИЖЕГОРОДСКАЯ ФОРМУЛА v8.0 — движок без curses.

Формулы, пороги и коэффициенты перенесены из nizhny.py без изменений.
Печать в горячем пути запрещена: только logging.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import random
import re
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

APP_NAME, APP_VER = "Нижегородская формула", "v8.0"
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_HERE, "dataset.jsonl")
WEIGHTS_PATH = os.path.join(_HERE, "weights_v80.json")
CACHE_DIR = os.path.join(_HERE, ".nizhny_cache")

SPLIT_TEST_RATIO = 0.2
SPLIT_RANDOM_STATE = 42

logger = logging.getLogger("nizhny")

_state_lock = threading.Lock()
_weights_cache: "Weights | None" = None


def configure_logging() -> None:
    """Уровень из NIZHNY_LOG_LEVEL или LOG_LEVEL. Повторный вызов не плодит хендлеры."""
    level_name = os.environ.get("NIZHNY_LOG_LEVEL") or os.environ.get("LOG_LEVEL") or "INFO"
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger("nizhny")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)


# ═════════════════════════════════════════════════════════════════════
# ЛЕММАТИЗАЦИЯ: pymorphy2 → pymorphy3 → lower()
# ═════════════════════════════════════════════════════════════════════
_MORPH = None
_MORPH_BACKEND = "none"
try:
    import pymorphy2

    _MORPH = pymorphy2.MorphAnalyzer()
    _MORPH_BACKEND = "pymorphy2"
except Exception:
    # pymorphy2 не установлен или словари не читаются — пробуем pymorphy3
    try:
        import pymorphy3

        _MORPH = pymorphy3.MorphAnalyzer()
        _MORPH_BACKEND = "pymorphy3"
    except Exception:
        # нет ни одного анализатора: norm() остаётся lower(), программа живёт
        _MORPH = None
        _MORPH_BACKEND = "none"


def morph_available() -> bool:
    return _MORPH is not None


def norm(w: str) -> str:
    if _MORPH is None:
        return w.lower()
    try:
        return _MORPH.parse(w)[0].normal_form
    except Exception:
        # конкретное слово не разобралось — не роняем mislabel
        return w.lower()


# ═════════════════════════════════════════════════════════════════════
# 1. ЛЕКСИКОН
# ═════════════════════════════════════════════════════════════════════
@dataclass
class Sensor:
    key: str
    label: str
    patterns: list[str]
    weight: float = 1.0
    margin: float = 0.0


LEXICON = [
    Sensor("urgency", "срочность/давление", [
        r"\bсрочно\b", r"\bуспей\w*", r"\bпоследн\w+ шанс\b", r"\bпрямо\s+сейчас\b",
        r"\bне\s+пропусти\w*", r"\bbreaking\b", r"\burgent\b", r"\bact\s+now\b"]),
    Sensor("caps", "капслок/восклицания", [
        r"[A-ZА-ЯЁ]{5,}", r"!{2,}", r"[!?]{3,}", r"!{1}(?=\s|$)"]),
    Sensor("emotive", "эмоциональная накачка", [
        r"\bшок\w*", r"\bсенсац\w+", r"\bужас\w*", r"\bкатастроф\w+",
        r"\bскандал\w*", r"\bпотряс\w+", r"\bкошмар\w*"]),
    Sensor("hedging", "размытые кванторы", [
        r"\bэксперт\w*\s+(?:счита|говор|утвержда|предупрежда)\w*",
        r"\bучён\w+\s+(?:счита|говор|доказа|выясн)\w*",
        r"\bисточник\w*\s+(?:сообща|говор|утвержда)\w*",
        r"\bпо\s+некоторым\s+данным\b", r"\bякобы\b", r"\bпо\s+слухам\b"]),
    Sensor("pseudoscience", "псевдонаучный язык", [
        r"\bнаучно\s+доказан\w+", r"\bпрорывн\w+ технолог\w+",
        r"\bквантов\w+ (?:эффект|поле|скачок)", r"\bклинически\s+доказан\w+"]),
    Sensor("passive", "скрытый агент", [
        r"\bбыл[оаи]?\s+\w+(?:ан|ен|т)\w*\b", r"\bвыяснил\w*ся\b",
        r"\bоказал\w*ся\b", r"\bпринято\s+решение\b", r"\bустановлен\w+\b"]),
    Sensor("cta", "призыв к действию", [
        r"\bподели\w*", r"\bрепост\w*", r"\bподпишись\b", r"\bподписывайт\w+",
        r"\bоставь\w*\s+комментари\w+", r"\bshare\s+this\b"]),
]
PRIMARY_CUES = [r"\bсогласно\s+(?:отчёт|отчет|документ|данн)\w*",
                r"\bФЗ[- ]\d+", r"\bпо\s+итогам\s+замер\w+", r"\bпервичн\w+ данн\w+"]
RETRACT_CUES = [r"\bопроверга\w+", r"\bопровергнут\w*", r"\bотозва\w+"]
AGENCY_CUES = [r"\bReuters\b", r"\bAssociated\s+Press\b", r"\bТАСС\b",
               r"\bРИА\s+Новости\b", r"\bИнтерфакс\b", r"\bBloomberg\b", r"\bAFP\b"]
DOMAIN_CRED = {
    "reuters.com": .92, "apnews.com": .91, "bbc.com": .88, "bbc.co.uk": .88,
    "tass.ru": .82, "ria.ru": .78, "interfax.ru": .80, "nature.com": .95,
    "science.org": .95, "arxiv.org": .75, "who.int": .93, "nasa.gov": .94,
    "gov.ru": .85, "gov.uk": .90, "wikipedia.org": .65, "medium.com": .40,
    "t.me": .35, "telegram.me": .35, "vk.com": .45, "x.com": .40,
    "twitter.com": .40, "youtube.com": .50, "tiktok.com": .25,
}
DEFAULT_CRED = .55

_PATTERN_CACHE: dict[tuple[str, ...], list[re.Pattern[str]]] = {}


def compiled_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    key = tuple(patterns)
    cached = _PATTERN_CACHE.get(key)
    if cached is None:
        cached = [re.compile(p, re.I | re.U) for p in patterns]
        _PATTERN_CACHE[key] = cached
    return cached


_PRIMARY_RE = compiled_patterns(PRIMARY_CUES)
_RETRACT_RE = compiled_patterns(RETRACT_CUES)
_AGENCY_RE = compiled_patterns(AGENCY_CUES)
URL_HOST_RE = re.compile(r"https?://([^/\s\)\]\"']+)")
DATA_NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*%|\b\d{3,}\b")
DATA_TABLE_RE = re.compile(r"\bтаблиц|\bграфик|\bприложени", re.I)
QUAL_BANG_RE = re.compile(r"!{3,}|\?{4,}")
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{5,}")
for _sensor in LEXICON:
    compiled_patterns(_sensor.patterns)


def _sig(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        # exp вышел за диапазон float — сигмоида уже прижата к 0 или 1
        return 0.0 if x < 0 else 1.0


@dataclass
class Signal:
    key: str
    label: str
    count: int
    activation: float
    hits: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    title: str
    body: str
    signals: list[Signal]
    flags: dict
    mislabel: float
    primary: bool
    retracted: bool
    domains: list[str]
    credibility: float
    data_availability: float
    base_quality: float
    score: float = 0.0
    verdict: str = ""
    caps: list = field(default_factory=list)
    url_included: bool = False


class Parser:
    def __init__(self, lexicon=None, threshold: float = .5):
        self.lex = lexicon or LEXICON
        self.threshold = threshold

    @staticmethod
    def split(title: str, body: str, text: str = "") -> tuple[str, str]:
        if title or body:
            return title.strip(), body.strip()
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if not lines:
            return "", ""
        if len(lines[0]) <= 140 and not lines[0].endswith("."):
            return lines[0], "\n".join(lines[1:])
        return "", "\n".join(lines)

    def signals(self, text: str) -> list[Signal]:
        out = []
        for s in self.lex:
            hits: list[str] = []
            for cre in compiled_patterns(s.patterns):
                hits += [m.group(0) for m in cre.finditer(text)]
            density = len(hits) / max(1.0, len(text) / 500.0)
            raw = s.weight * math.log1p(density)
            act = _sig(6.0 * (raw - s.margin))
            out.append(Signal(s.key, s.label, len(hits), round(act, 4), hits[:6]))
        return out

    @staticmethod
    def mislabel(title: str, body: str) -> float:
        if not title or not body:
            return 0.0
        stop = {"котор", "поэт", "будет", "этот", "весь", "свой", "такой", "через", "между"}

        def keys(s: str) -> set[str]:
            return {norm(w) for w in WORD_RE.findall(s) if norm(w) not in stop}

        tk = keys(title)
        if len(tk) < 2:
            return 0.0
        return len(tk - keys(body)) / len(tk)

    def sources(self, text: str):
        primary = any(cre.search(text) for cre in _PRIMARY_RE)
        retracted = any(cre.search(text) for cre in _RETRACT_RE)
        ags = [m.group(0) for cre in _AGENCY_RE for m in cre.finditer(text)]
        # lstrip("www.") — поведение исходника (набор символов, не префикс).
        doms = sorted({d.lower().lstrip("www.") for d in URL_HOST_RE.findall(text)})
        if not doms and ags:
            doms = [f"agency:{a.lower()}" for a in sorted(set(ags))]
        if not doms:
            doms = ["(none)"]
        cred = [DOMAIN_CRED.get(d, DEFAULT_CRED) for d in doms]
        return primary, retracted, doms, min(1.0, max(cred) + 0.08 * min(3, len(doms) - 1))

    @staticmethod
    def data_avail(text: str, primary: bool, n: int) -> float:
        da = 0.30 + (0.35 if primary else 0) + (0.15 if n >= 2 else 0)
        if DATA_NUM_RE.search(text):
            da += 0.10
        if DATA_TABLE_RE.search(text):
            da += 0.10
        return max(0.0, min(1.0, da))

    @staticmethod
    def base_qual(text: str) -> float:
        q, n = 0.50, len(text)
        q += 0.15 if n > 700 else 0
        q += 0.10 if n > 1500 else 0
        q -= 0.25 if n < 200 else 0
        q += 0.05 if text.count("\n") >= 4 else 0
        caps = sum(1 for c in text if c.isupper()) / max(1, n)
        q -= 0.20 if caps > 0.30 else 0
        q -= 0.10 if QUAL_BANG_RE.search(text) else 0
        return max(0.0, min(1.0, q))

    def analyze(
        self,
        text: str,
        title: str = "",
        body: str = "",
        url: str = "",
        include_url: bool = True,
    ) -> Analysis:
        """Разбор. include_url — режим score_include_url (по умолчанию включён).

        ФИКС 1. Вызов analyze(text, title=заголовок) раньше попадал в split(),
        который при непустом title и пустом body возвращал тело "". Если тело
        передано аргументом или лежит хвостом в text — оно остаётся в разборе.
        """
        raw = text or ""
        title, body = self.split(title, body, raw)
        if title and not body and raw.strip():
            stripped = raw.strip()
            head = title.strip()
            if head and stripped.startswith(head):
                rest = stripped[len(head):].lstrip("\n").strip()
            else:
                rest = "" if stripped == head else stripped
            if rest:
                body = rest
        if title:
            full = title + "\n" + body
        elif raw:
            full = raw
        else:
            full = body
        url_included = False
        # ФИКС 2. OFF — URL статьи не подмешивается, как в CLI.
        if include_url and url and url.strip():
            full = full + "\n" + url.strip()
            url_included = True
        sigs = self.signals(full)
        mis = self.mislabel(title, body)
        prim, retr, doms, cred = self.sources(full)
        da = self.data_avail(full, prim, len(doms))
        bq = self.base_qual(full)
        return Analysis(
            title, body, sigs, {s.key: s.activation for s in sigs},
            mis, prim, retr, doms, cred, da, bq, url_included=url_included,
        )

    def score(self, a: Analysis, weights: dict | None = None, H_scale: float = 1.0) -> Analysis:
        fw = weights or {s.key: 1.0 for s in self.lex}
        H = sum(a.flags.get(k, 0.0) * fw.get(k, 1.0) for k in fw)
        H = H * H_scale / (1.0 + 0.6 * H) + a.mislabel * 0.5
        S = 3.0 + (a.credibility - 0.55) * 3.2 + (a.data_availability - 0.55) * 2.0 \
            + (a.base_quality - 0.55) * 2.4 - H * 2.6 \
            + (0.4 if a.primary else -0.5) - (1.3 if a.retracted else 0.0)
        caps: list[str] = []
        if len(a.domains) == 1:
            S = min(S, 3.0)
            caps.append("одинокий_источник≤3")
        if not a.primary:
            S = min(S, 3.4)
            caps.append("нет_первичного≤3.4")
        if a.retracted:
            S = min(S, 1.4)
            caps.append("опровергнуто≤1.4")
        if H > 2.0:
            S = min(S, 2.0)
            caps.append("шум_подачи≤2")
        a.score = max(1.0, min(5.0, round(S, 2)))
        a.verdict = (
            "добросовестная" if a.score >= 4 else "спорная" if a.score >= 3
            else "слабодостоверная" if a.score >= 2 else "манипулятивная"
        )
        a.caps = caps
        return a


TAG_RE = re.compile(r"<(script|style|noscript|svg|nav|footer|aside)[^>]*>.*?</\1>", re.I | re.S)
BLOCK_RE = re.compile(r"</?(p|div|br|li|h[1-6]|article|section|tr|blockquote)[^>]*>", re.I)
META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']'
    r'(og:title|og:description|twitter:title|description|title|'
    r'article:published_time)["\'][^>]+content=["\']([^"\']*)["\']',
    re.I,
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
NEXTDATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]{4,30})"', re.I)
TIME_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)', re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
SENT_RE = re.compile(r"[.!?。！？](\s|$)")
AMP_FIX_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+);)")


def strip_html(h: str) -> str:
    s = TAG_RE.sub(" ", h)
    s = BLOCK_RE.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&lt;", "<"),
        ("&gt;", ">"), ("&#8217;", "'"), ("&laquo;", "«"), ("&raquo;", "»"),
        ("&mdash;", "—"),
    ]:
        s = s.replace(a, b)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def html_title(h: str) -> str:
    for m in META_RE.finditer(h):
        if m.group(1).lower() in ("og:title", "twitter:title", "title"):
            return strip_html(m.group(2))
    m = TITLE_RE.search(h)
    return strip_html(m.group(1)) if m else ""


def html_date(h: str) -> str:
    for m in META_RE.finditer(h):
        if m.group(1).lower() == "article:published_time":
            return m.group(2)[:10]
    m = DATE_PUBLISHED_RE.search(h)
    if m:
        return m.group(1)[:10]
    m = TIME_RE.search(h)
    return m.group(1)[:10] if m else ""


def _walk_json(obj) -> list[dict]:
    """Собрать все dict-узлы рекурсивно."""
    found = []
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            found.append(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return found


def extract_jsonld(html: str) -> tuple[str, str, str]:
    """Тело/заголовок/дата из JSON-LD."""
    for m in JSONLD_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            # битый JSON-LD: пропускаем скрипт и ищем следующий
            continue
        for node in _walk_json(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(x in ("NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle")
                   for x in types if x):
                body = node.get("articleBody") or node.get("text") or ""
                title = node.get("headline") or node.get("name") or ""
                date = (node.get("datePublished") or node.get("dateModified") or "")[:10]
                if body and len(body) > 200:
                    return strip_html(body), strip_html(title), date
    return "", "", ""


def extract_nextdata(html: str) -> tuple[str, str, str]:
    """Тело из Next.js __NEXT_DATA__ (если есть)."""
    m = NEXTDATA_RE.search(html)
    if not m:
        return "", "", ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        # __NEXT_DATA__ не JSON — этот метод просто неприменим
        return "", "", ""
    best_body = best_title = best_date = ""
    for node in _walk_json(data):
        for k in ("articleBody", "content", "body", "text", "description"):
            v = node.get(k)
            if isinstance(v, str) and len(v) > 300 and len(v) > len(best_body):
                best_body = v
        for k in ("headline", "title", "name"):
            v = node.get(k)
            if isinstance(v, str) and 10 < len(v) < 200 and not best_title:
                best_title = v
        for k in ("datePublished", "publishedAt", "date"):
            v = node.get(k)
            if isinstance(v, str) and len(v) >= 10 and not best_date:
                best_date = v[:10]
    if best_body:
        best_body = strip_html(best_body)
    return best_body, strip_html(best_title), best_date


KILL_TAGS = {"script", "style", "noscript", "svg", "iframe", "form", "button",
             "nav", "aside", "footer", "header", "menu", "figure", "figcaption"}
KILL_ROLES = re.compile(r"(nav|banner|complementary|contentinfo|search|menu)", re.I)
POS_RE = re.compile(
    r"(article|content|post|entry|story|text|body|main|"
    r"paragraph|содержан|стать|текст|новост)",
    re.I,
)
NEG_RE = re.compile(
    r"(nav|menu|sidebar|footer|header|comment|related|"
    r"share|social|promo|advert|banner|widget|cookie|"
    r"popup|modal|subscribe|newsletter|breadcrumb|"
    r"меню|подвал|реклам|коммент|похож)",
    re.I,
)
STRONG_TAGS = {"article", "main"}
SEMI_TAGS = {"section", "div"}


@dataclass
class Block:
    tag: str
    attrs: dict
    text: str = ""
    link_text: str = ""
    score: float = 0.0

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def link_density(self) -> float:
        return len(self.link_text) / max(1, len(self.text))


class BlockExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.stack: list[Block] = []
        self._kill = 0
        self._in_link = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        cls = a.get("class", "") + " " + a.get("id", "") + " " + a.get("role", "")
        if tag in KILL_TAGS or KILL_ROLES.search(cls):
            self._kill += 1
            if self.stack:
                self.stack[-1].score -= 25
            return
        if tag == "a":
            self._in_link += 1
        if tag in STRONG_TAGS | SEMI_TAGS | {"p", "pre", "blockquote", "li"}:
            b = Block(tag, a)
            self.blocks.append(b)
            self.stack.append(b)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self._in_link:
            self._in_link -= 1
            return
        if tag in KILL_TAGS:
            self._kill = max(0, self._kill - 1)
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                node = self.stack[i]
                for p in self.stack[i + 1:]:
                    p.text += node.text
                    p.link_text += node.link_text
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._kill or not self.stack or not data.strip():
            return
        self.stack[-1].text += data + " "
        if self._in_link:
            self.stack[-1].link_text += data + " "


def _class_bonus(b: Block) -> float:
    cls = b.attrs.get("class", "") + " " + b.attrs.get("id", "")
    s = 0.0
    if POS_RE.search(cls):
        s += 30
    if NEG_RE.search(cls):
        s -= 35
    if b.tag in STRONG_TAGS:
        s += 40
    elif b.tag == "p":
        s += 8
    elif b.tag == "blockquote":
        s += 4
    return s


def _text_bonus(b: Block) -> float:
    t = b.text
    if len(t) < 40:
        return -20.0
    s = min(60.0, len(t) / 25.0)
    s += min(25.0, (t.count(",") + t.count("，")) * 2.0)
    s += min(25.0, len(SENT_RE.findall(t)) * 3.0)
    ld = b.link_density
    if ld > 0.5:
        s -= 70
    elif ld > 0.3:
        s -= 35
    elif ld > 0.15:
        s -= 12
    return s


@dataclass
class Extraction:
    text: str
    title: str = ""
    node_tag: str = ""
    node_class: str = ""
    confidence: float = 0.0
    candidates: int = 0
    method: str = ""


def _fallback_paragraphs(html: str) -> str:
    paras = [strip_html(p) for p in PARA_RE.findall(html)]
    paras = [p for p in paras if len(p) >= 80]
    out: list[str] = []
    for p in paras:
        if not any(p in o for o in out):
            out.append(p)
    return "\n\n".join(out)


def extract_article(html: str, title_hint: str = "") -> Extraction:
    b, t, _d = extract_jsonld(html)
    if b:
        return Extraction(b, t or title_hint, "application/ld+json", "", 0.95, 0, "jsonld")
    b, t, _d = extract_nextdata(html)
    if b:
        return Extraction(b, t or title_hint, "__NEXT_DATA__", "", 0.85, 0, "nextdata")
    clean = TAG_RE.sub(" ", COMMENT_RE.sub(" ", html))
    ex = BlockExtractor()
    try:
        ex.feed(clean)
    except Exception:
        # сломанная вёрстка: ниже сработает fallback по абзацам
        pass
    if not ex.blocks:
        return Extraction(
            _fallback_paragraphs(html) or strip_html(html),
            title_hint, "fallback", "", 0.1, 0, "fallback",
        )
    for block in ex.blocks:
        block.score += _class_bonus(block) + _text_bonus(block)
    scored = sorted(ex.blocks, key=lambda x: -x.score)
    best = scored[0]
    parts = [
        x.text.strip() for x in ex.blocks
        if x.score >= 12 and x.tag in ("p", "blockquote", "pre", "li", "article", "div", "section")
    ]
    parts = sorted({p for p in parts if len(p) > 40}, key=len, reverse=True)
    deduped: list[str] = []
    for p in parts:
        if not any(p in d for d in deduped):
            deduped.append(p)
    body = re.sub(r"[ \t]{2,}", " ", "\n\n".join(deduped)).strip()
    conf = min(1.0, len(body) / max(200.0, len(strip_html(html)) * 0.7))
    if len(body) < 150:
        fb = _fallback_paragraphs(html)
        if len(fb) > len(body):
            return Extraction(fb, title_hint, "fallback", "", 0.25, len(ex.blocks), "fallback")
        conf = min(conf, 0.3)
    return Extraction(
        body, title_hint, best.tag, best.attrs.get("class", "")[:40],
        round(conf, 3), len(ex.blocks), "block",
    )


UA = "Mozilla/5.0 (compatible; NizhnyBot/8.0; +https://example.org/bot)"


def _net_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"отдал {exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "не ответил вовремя"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        text = str(reason)
        if isinstance(reason, socket.gaierror) or "Name or service not known" in text or "Temporary failure in name resolution" in text:
            return "не открылся: адрес не найден"
        return f"не открылся: {reason}"
    return "не открылся"


def feed_error_text(reason: str | None) -> str:
    """Человеческая причина для ленты, без стектрейса."""
    if not reason:
        return "фид не ответил"
    if reason.startswith("отдал "):
        return "фид " + reason
    if reason.startswith("фид "):
        return reason
    return "фид " + reason


def page_error_text(reason: str | None) -> str:
    if not reason:
        return "страница не ответила"
    if reason.startswith("отдал "):
        return "страница отдала " + reason[len("отдал "):]
    if reason.startswith("не открылся"):
        return "страница не открылась" + reason[len("не открылся"):]
    if reason.startswith("не ответил"):
        return "страница не ответила" + reason[len("не ответил"):]
    return "страница " + reason


class Fetcher:
    def __init__(self, timeout=15, retries=2, cache_dir=CACHE_DIR, polite=1.0):
        self.timeout, self.retries, self.polite = timeout, retries, polite
        self.cache_dir = cache_dir
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cp(self, url: str) -> str | None:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest()[:20] + ".html")

    def fetch(self, url: str, max_age: int = 1800, silent: bool = True) -> tuple[str | None, str | None]:
        """Текст страницы и человеческая причина сбоя (если текста нет)."""
        cp = self._cp(url)
        if cp and os.path.exists(cp) and time.time() - os.path.getmtime(cp) < max_age:
            try:
                with open(cp, encoding="utf-8", errors="ignore") as fh:
                    return fh.read(), None
            except OSError:
                # битый файл кэша не мешает живому запросу
                logger.debug("cache read failed for %s", url)
        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            wait = self.polite - (time.time() - self._last.get(host, 0))
            self._last[host] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Encoding": "gzip",
            "Accept-Language": "ru,en;q=0.7",
        })
        last_err = "не ответил"
        for i in range(self.retries + 1):
            try:
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    enc = "utf-8"
                    m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""))
                    if m:
                        enc = m.group(1)
                    txt = raw.decode(enc, errors="ignore")
                if cp:
                    with self._lock:
                        try:
                            with open(cp, "w", encoding="utf-8") as fh:
                                fh.write(txt)
                        except OSError:
                            # кэш — оптимизация, отказ записи не отменяет ответ
                            logger.debug("cache write failed for %s", url)
                return txt, None
            except Exception as e:
                # сетевой отказ повторяем; наружу уходит короткая причина, не traceback
                last_err = _net_reason(e)
                if i == self.retries:
                    if not silent:
                        logger.warning("%s: %s", url, last_err)
                    return None, last_err
                time.sleep(1.5 * (i + 1))
        return None, last_err

    def get(self, url: str, max_age: int = 1800, silent: bool = True) -> str | None:
        text, _err = self.fetch(url, max_age=max_age, silent=silent)
        return text


_fetcher: Fetcher | None = None


def get_fetcher() -> Fetcher:
    global _fetcher
    with _state_lock:
        if _fetcher is None:
            _fetcher = Fetcher()
        return _fetcher


NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
FEEDS_DEFAULT = {
    "reuters.com": "https://feeds.reuters.com/reuters/topNews",
    "apnews.com": "https://apnews.com/hub/ap-top-news.rss",
    "bbc.co.uk": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "tass.ru": "https://tass.ru/rss/v2.xml",
    "interfax.ru": "https://www.interfax.ru/rss.asp",
    "lenta.ru": "https://lenta.ru/rss/news",
    "rbc.ru": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "nature.com": "https://www.nature.com/nature.rss",
    "habr.com": "https://habr.com/ru/rss/all/all/",
}


@dataclass
class Article:
    title: str = ""
    body: str = ""
    url: str = ""
    date: str = ""
    source: str = ""
    score: float = 0.0
    verdict: str = ""
    domains: list = field(default_factory=list)
    flags: dict = field(default_factory=dict)
    method: str = ""
    confidence: float = 0.0
    dup_of: str = ""
    ts: float = 0.0
    primary: bool = False
    caps: list = field(default_factory=list)


def parse_feed(xml_text: str, feed_url: str, limit: int = 30) -> list[Article]:
    out: list[Article] = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "ignore"))
    except ET.ParseError:
        # часть фидов отдаёт голые амперсанды — одна попытка экранирования
        try:
            fixed = AMP_FIX_RE.sub("&amp;", xml_text)
            root = ET.fromstring(fixed.encode("utf-8", "ignore"))
        except Exception:
            # после починки XML всё ещё битый: пустая лента, не исключение
            return out
    src = urllib.parse.urlparse(feed_url).netloc

    def txt(node, *paths: str) -> str:
        for p in paths:
            el = node.find(p, NS)
            if el is not None:
                if el.text and el.text.strip():
                    return strip_html(el.text)
                if el.get("href"):
                    return el.get("href") or ""
        return ""

    for it in root.findall(".//item"):
        out.append(Article(
            title=txt(it, "title"),
            body=txt(it, "description", "content:encoded"),
            url=txt(it, "link"),
            date=(txt(it, "pubDate") or txt(it, "dc:date"))[:25],
            source=src,
        ))
        if len(out) >= limit:
            return out
    for it in root.findall(".//atom:entry", NS):
        out.append(Article(
            title=txt(it, "atom:title"),
            body=txt(it, "atom:summary", "atom:content"),
            url=txt(it, "atom:link"),
            date=txt(it, "atom:updated", "atom:published")[:10],
            source=src,
        ))
        if len(out) >= limit:
            return out
    return out


_SHINGLE_RE = re.compile(r"[a-zа-яё0-9]+")
_TITLE_KEY_RE = re.compile(r"\W+")


def _shingles(text: str, k: int = 3) -> set[str]:
    words = _SHINGLE_RE.findall(text.lower())[:400]
    return {" ".join(words[i:i + k]) for i in range(max(1, len(words) - k + 1))}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_key(t: str) -> str:
    return _TITLE_KEY_RE.sub("", t.lower())[:60]


def dedup(arts: list[Article], sim: float = 0.55) -> list[Article]:
    """Убирает дубли по сходству заголовков и шинглов тела."""
    uniq: list[Article] = []
    seen_titles: dict[str, str] = {}
    for a in arts:
        tk = title_key(a.title)
        if tk and tk in seen_titles:
            a.dup_of = seen_titles[tk]
            continue
        dupe = None
        if len(a.body) > 200:
            sh = _shingles(a.body)
            for u in uniq:
                if len(u.body) > 200 and jaccard(sh, _shingles(u.body)) >= sim:
                    dupe = u
                    break
        if dupe:
            a.dup_of = dupe.url or dupe.title
            continue
        if tk:
            seen_titles[tk] = a.url or a.title
        uniq.append(a)
    return uniq


def parse_date(s: str) -> float:
    """Разные форматы -> unix. Неузнанное -> 0."""
    if not s:
        return 0.0
    s = s.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z", "%d.%m.%Y", "%d.%m.%Y %H:%M",
    ):
        try:
            return datetime.strptime(s[:len(s)], fmt).timestamp()
        except Exception:
            # формат не совпал — пробуем усечённую строку и следующий шаблон
            try:
                return datetime.strptime(s[:19], fmt).timestamp()
            except Exception:
                pass
    return 0.0


def date_filter(arts: list[Article], since: str = "", until: str = "") -> list[Article]:
    s_ts, u_ts = parse_date(since), parse_date(until)
    if not s_ts and not u_ts:
        return arts
    out = []
    for a in arts:
        ts = parse_date(a.date) or a.ts
        if s_ts and ts and ts < s_ts:
            continue
        if u_ts and ts and ts > u_ts:
            continue
        if not ts:
            a.date = (a.date or "") + " (дата неясна)"
        out.append(a)
    return out


@dataclass
class Weights:
    sensor_weight: dict = field(default_factory=dict)
    sensor_margin: dict = field(default_factory=dict)
    H_scale: float = 1.0
    lr: float = 0.05
    epochs: int = 60

    @staticmethod
    def fresh() -> "Weights":
        return Weights({s.key: 1.0 for s in LEXICON}, {s.key: 0.0 for s in LEXICON})

    def dump(self, p: str = WEIGHTS_PATH) -> None:
        """Атомарная запись: tempfile в том же каталоге и os.replace."""
        directory = os.path.dirname(os.path.abspath(p)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".weights_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except Exception:
            # replace не случился — временный файл не оставляем
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def load(p: str = WEIGHTS_PATH) -> "Weights":
        if not os.path.exists(p):
            return Weights.fresh()
        try:
            with open(p, encoding="utf-8") as fh:
                return Weights(**json.load(fh))
        except Exception:
            # повреждённый файл весов не должен останавливать разбор
            logger.warning("weights file unreadable, using fresh weights")
            return Weights.fresh()

    def apply(self, lex) -> None:
        for s in lex:
            s.weight = self.sensor_weight.get(s.key, 1.0)
            s.margin = self.sensor_margin.get(s.key, 0.0)


def weights_on_disk() -> bool:
    return os.path.exists(WEIGHTS_PATH)


def current_weights() -> Weights:
    global _weights_cache
    with _state_lock:
        if _weights_cache is None:
            _weights_cache = Weights.load(WEIGHTS_PATH)
        return _weights_cache


def set_weights(w: Weights) -> None:
    global _weights_cache
    with _state_lock:
        w.dump(WEIGHTS_PATH)
        _weights_cache = w


class Trainer:
    def __init__(self, parser: Parser, w: Weights):
        self.p, self.w = parser, w

    @staticmethod
    def f1(preds, gold) -> float:
        # Формула исходника: при отсутствии tp результат строго 0.0.
        tp = sum(1 for p, g in zip(preds, gold) if p == 1 and g == 1)
        fp = sum(1 for p, g in zip(preds, gold) if p == 1 and g == 0)
        fn = sum(1 for p, g in zip(preds, gold) if p == 0 and g == 1)
        if not tp:
            return 0.0
        pr, rc = tp / (tp + fp), tp / (tp + fn)
        return 2 * pr * rc / (pr + rc)

    @staticmethod
    def precision(preds, gold) -> float:
        tp = sum(1 for p, g in zip(preds, gold) if p == 1 and g == 1)
        fp = sum(1 for p, g in zip(preds, gold) if p == 1 and g == 0)
        if tp + fp == 0:
            return 0.0
        return tp / (tp + fp)

    @staticmethod
    def recall(preds, gold) -> float:
        tp = sum(1 for p, g in zip(preds, gold) if p == 1 and g == 1)
        fn = sum(1 for p, g in zip(preds, gold) if p == 0 and g == 1)
        if tp + fn == 0:
            return 0.0
        return tp / (tp + fn)

    @staticmethod
    def accuracy(preds, gold) -> float:
        if not gold:
            return 0.0
        return sum(1 for p, g in zip(preds, gold) if p == g) / len(gold)

    def _pred(self, a: Analysis, w: Weights, thr: float = .5) -> int:
        # 1 = шум/недостоверная. Метки SEED не инвертируются.
        H = sum(a.flags.get(k, 0.0) * w.sensor_weight.get(k, 1.0) for k in w.sensor_weight)
        H = H * w.H_scale / (1.0 + 0.6 * H) + a.mislabel * 0.5
        return int(H >= thr or a.credibility < 0.45)

    def _metrics(self, rows: list[dict]) -> dict[str, float]:
        if not rows:
            return {"f1": 0.0, "precision": 0.0, "recall": 0.0, "accuracy": 0.0}
        fmt = [self.p.analyze(d["text"], include_url=False) for d in rows]
        gold = [int(d["label"]) for d in rows]
        preds = [self._pred(a, self.w) for a in fmt]
        return {
            "f1": self.f1(preds, gold),
            "precision": self.precision(preds, gold),
            "recall": self.recall(preds, gold),
            "accuracy": self.accuracy(preds, gold),
        }

    def train(self, data, log=None, save: str = WEIGHTS_PATH) -> dict:
        """Подбор весов на обучающей части, метрики — на тестовой.

        Разбиение стратифицировано по метке, random_state фиксирован.
        Алгоритм шага (lr / −lr, ранний стоп) и формула F1 не менялись.
        """
        if log is None:
            log = logger.info
        lines: list[str] = []

        def emit(message: str) -> None:
            lines.append(message)
            log(message)

        train_rows, test_rows = stratified_split(data)
        fmt = [self.p.analyze(d["text"], include_url=False) for d in train_rows]
        gold = [int(d["label"]) for d in train_rows]
        self.w.apply(self.p.lex)
        best = self.f1([self._pred(a, self.w) for a in fmt], gold)
        emit(f"  baseline F1 (train) = {best:.3f}")
        for ep in range(1, self.w.epochs + 1):
            improved = False
            for key in list(self.w.sensor_weight):
                for delta in (self.w.lr, -self.w.lr):
                    old = self.w.sensor_weight[key]
                    self.w.sensor_weight[key] = max(0.0, old + delta)
                    self.w.apply(self.p.lex)
                    fmt = [self.p.analyze(d["text"], include_url=False) for d in train_rows]
                    f = self.f1([self._pred(a, self.w) for a in fmt], gold)
                    if f > best + 1e-6:
                        best = f
                        improved = True
                        break
                    self.w.sensor_weight[key] = old
            if not improved:
                break
            if ep % 10 == 0:
                emit(f"  epoch {ep:>3}: F1 = {best:.3f}")
        self.w.apply(self.p.lex)
        self.w.dump(save)
        test_m = self._metrics(test_rows)
        emit(
            f"  ✔ test F1 = {test_m['f1']:.3f}  P = {test_m['precision']:.3f}"
            f"  R = {test_m['recall']:.3f}  acc = {test_m['accuracy']:.3f}"
            f"  n_train = {len(train_rows)}  n_test = {len(test_rows)} → {save}"
        )
        return {
            "f1": test_m["f1"],
            "precision": test_m["precision"],
            "recall": test_m["recall"],
            "accuracy": test_m["accuracy"],
            "n_train": len(train_rows),
            "n_test": len(test_rows),
            "weights": dict(self.w.sensor_weight),
            "log": lines,
        }


def stratified_split(
    rows: list[dict],
    test_ratio: float = SPLIT_TEST_RATIO,
    random_state: int = SPLIT_RANDOM_STATE,
) -> tuple[list[dict], list[dict]]:
    """80/20 внутри каждой метки. Метки не перемешиваются между классами."""
    rng = random.Random(random_state)
    groups: dict[int, list[dict]] = {}
    for row in rows:
        groups.setdefault(int(row.get("label", 0)), []).append(row)
    train: list[dict] = []
    test: list[dict] = []
    for items in groups.values():
        bucket = items[:]
        rng.shuffle(bucket)
        if len(bucket) < 2:
            train.extend(bucket)
            continue
        n_test = int(round(len(bucket) * test_ratio))
        n_test = max(1, min(n_test, len(bucket) - 1))
        test.extend(bucket[:n_test])
        train.extend(bucket[n_test:])
    return train, test


def normalize_row(row: dict) -> dict:
    item = dict(row)
    if "text" not in item and ("title" in item or "body" in item):
        item["text"] = (str(item.get("title", "")) + "\n" + str(item.get("body", ""))).strip()
    if "label" in item:
        item["label"] = int(item["label"])
    return item


def load_dataset(extra: str | None = None) -> list[dict]:
    rows: list[dict] = []
    paths = [DATA_PATH] + ([extra] if extra else [])
    for p in paths:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(normalize_row(json.loads(line)))
                    except Exception:
                        # одна битая строка не отменяет остальной датасет
                        logger.debug("skip bad dataset line in %s", p)
    return rows


def append_dataset(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(DATA_PATH) or ".", exist_ok=True)
    with open(DATA_PATH, "a", encoding="utf-8") as fh:
        for raw in rows:
            r = normalize_row(raw)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


SEED = [
    {"label": 1, "text": "СРОЧНО!!! Шок! Учёные доказали — эксперты в ужасе!\nПо некоторым данным, якобы установлено. Поделись! Подпишись.\nhttps://t.me/chan"},
    {"label": 1, "text": "Breaking! ВСЕМ СРОЧНО\nМногие эксперты утверждают, клинически доказано. Репост!\nhttps://t.me/other"},
    {"label": 1, "text": "Возмутительно! Кошмарная правда\nПо слухам, принято решение, выяснилось якобы. Поделись.\nhttps://vk.com/group"},
    {"label": 1, "text": "Потрясающее открытие!!! Сенсация!\nУчёные выяснили, научно доказанный метод. Расскажи друзьям! Подписывайтесь.\nhttps://tiktok.com/x"},
    {"label": 1, "text": "НЕВЕРОЯТНО!!! Это изменит всё\nКлинически доказано, ужасающие последствия. Поделись.\nhttps://x.com/i/status"},
    {"label": 1, "text": "КАТАСТРОФА! СРОЧНО ЧИТАЙ\nЯкобы специалисты установили. Скандал. Поделись.\nhttps://t.me/news1"},
    {"label": 1, "text": "ШОК КОНТЕНТ!!! Внимание!\nБыло принято решение, прорывная технология. Репост!\nhttps://facebook.com/p"},
    {"label": 1, "text": "Срочно! Последний шанс узнать правду\nМногие считают, невероятное открытие. Поделись. Сенсация!\nhttps://t.me/hot"},
    {"label": 1, "text": "ПОТРЯСАЮЩАЯ новость!\nПо слухам, квантовое поле исцеляет. Ошеломительный результат. Репост!\nhttps://youtube.com/w"},
    {"label": 1, "text": "Ужас! Сенсация века!\nЯкобы эксперты предупреждают. Катастрофические последствия. Поделись срочно!\nhttps://t.me/alarm"},
    {"label": 0, "text": "Совет директоров утвердил бюджет на 2027 год\nСогласно официальному отчёту от 14 октября, расходы вырастут на 6,2% — до 48 млрд рублей. Документ доступен на сайте.\nhttps://gov.ru/report"},
    {"label": 0, "text": "ЦБ сохранил ключевую ставку\nПо итогам заседания 25 октября регулятор оставил ставку на 16%. Согласно пресс-релизу, инфляция замедлилась до 7,1%.\nhttps://cbr.ru/press"},
    {"label": 0, "text": "Учёные опубликовали данные о качестве воды\nПервичные данные замеров за III квартал: 92,4% проб соответствуют нормам. Таблица результатов приложена.\nhttps://nature.com/a"},
    {"label": 0, "text": "Открытие нового корпуса больницы\nВвод в эксплуатацию состоялся 3 сентября. Финансирование — 1,8 млрд рублей. Согласно проектной документации, 240 мест.\nhttps://gov.ru/news"},
    {"label": 0, "text": "Reuters: экспорт зерна вырос на 11%\nСогласно данным Минсельхоза, за 9 месяцев отгружено 47,3 млн тонн. Аналитики отмечают сезонный фактор.\nhttps://reuters.com/m"},
    {"label": 0, "text": "Результаты переписи населения\nРосстат опубликовал предварительные итоги: 146,1 млн человек. Таблицы по регионам приложены.\nhttps://rosstat.gov.ru/i"},
    {"label": 0, "text": "Изменения в правилах дорожного движения\nСогласно постановлению №1421, с 1 ноября вводится новый знак. Текст документа опубликован. Поправки вступают в силу.\nhttps://pravo.gov.ru/d"},
    {"label": 0, "text": "Экспедиция вернулась из Арктики\nИсследователи собрали 340 проб льда. Согласно предварительному отчёту, толщина покрова снизилась на 4% за десятилетие.\nhttps://science.org/a"},
    {"label": 0, "text": "ТАСС: подписано соглашение о поставках\nПо итогам встречи 12 октября подписан контракт на 5 лет, сумма — 2,3 млрд рублей. Копия направлена в министерство.\nhttps://tass.ru/e"},
    {"label": 0, "text": "Проверка выявила нарушения у подрядчика\nСогласно акту проверки от 8 октября, работы выполнены на 78%. Отчёт направлен в контрольное управление.\nhttps://gov.ru/control"},
    {"label": 0, "text": "Минфин разместил облигации\nПо итогам аукциона 15 октября спрос превысил предложение в 2,4 раза. Согласно данным ведомства, средневзвешенная доходность — 13,8%.\nhttps://minfin.gov.ru/o"},
    {"label": 0, "text": "Университет открыл новую лабораторию\nСогласно приказу ректора от 1 октября, создано 12 рабочих мест. Финансирование — 84 млн рублей из гранта.\nhttps://uni.ru/lab"},
]


def ensure_seed() -> None:
    if not os.path.exists(DATA_PATH):
        append_dataset(SEED)


def build_parser() -> tuple[Parser, Weights]:
    w = current_weights()
    lex = [Sensor(s.key, s.label, list(s.patterns), s.weight, s.margin) for s in LEXICON]
    p = Parser(lex)
    w.apply(p.lex)
    return p, w


def _lex_from_weights(sensor_weight: dict, sensor_margin: dict) -> list[Sensor]:
    return [
        Sensor(
            s.key, s.label, list(s.patterns),
            float(sensor_weight.get(s.key, s.weight)),
            float(sensor_margin.get(s.key, s.margin)),
        )
        for s in LEXICON
    ]


def evaluate(
    text: str = "",
    title: str = "",
    body: str = "",
    url: str = "",
    weights: dict | None = None,
    H_scale: float | None = None,
    include_url: bool = True,
) -> Analysis:
    """Один разбор с текущими весами. include_url по умолчанию включён."""
    w = current_weights()
    sensor_w = dict(w.sensor_weight)
    margins = dict(w.sensor_margin)
    scale = w.H_scale if H_scale is None else float(H_scale)
    if weights:
        for key, value in weights.items():
            sensor_w[str(key)] = float(value)
    parser = Parser(_lex_from_weights(sensor_w, margins))
    analysis = parser.analyze(text, title=title or "", body=body or "", url=url or "", include_url=include_url)
    parser.score(analysis, sensor_w, scale)
    return analysis


def analysis_dict(a: Analysis) -> dict:
    return {
        "title": a.title,
        "body": a.body,
        "score": a.score,
        "verdict": a.verdict,
        "caps": list(a.caps),
        "signals": [
            {"key": s.key, "label": s.label, "count": s.count, "activation": s.activation, "hits": list(s.hits)}
            for s in a.signals
        ],
        "flags": dict(a.flags),
        "mislabel": a.mislabel,
        "primary": a.primary,
        "retracted": a.retracted,
        "domains": list(a.domains),
        "credibility": a.credibility,
        "data_availability": a.data_availability,
        "base_quality": a.base_quality,
        "url_included": bool(a.url_included),
    }


def resolve_feeds(requested: list[str] | None) -> dict[str, str]:
    if not requested:
        return dict(FEEDS_DEFAULT)
    out: dict[str, str] = {}
    for item in requested:
        item = item.strip()
        if not item:
            continue
        if item in FEEDS_DEFAULT:
            out[item] = FEEDS_DEFAULT[item]
        else:
            out[item] = item
    return out


class Finder:
    def __init__(self, parser: Parser, w: Weights, fetcher: Fetcher | None = None):
        self.p, self.w = parser, w
        self.f = fetcher or get_fetcher()
        self.log = lambda *a: None
        self.last_errors: list[dict] = []

    def from_feeds(self, feeds: dict, limit_per: int = 15) -> list[Article]:
        arts: list[Article] = []
        self.last_errors = []
        for name, url in feeds.items():
            self.log(f"→ {name}")
            xml, err = self.f.fetch(url)
            if not xml:
                message = feed_error_text(err)
                self.last_errors.append({"feed": url, "error": message})
                self.log(f"  ⚠ {message}")
                continue
            try:
                got = parse_feed(xml, url, limit_per)
            except Exception as e:
                # неожиданный разбор не останавливает остальные фиды
                logger.debug("feed parse failed: %s", e)
                self.last_errors.append({"feed": url, "error": "фид не разобран"})
                self.log("  ⚠ фид не разобран")
                continue
            self.log(f"  {len(got)} записей")
            arts += got
        return arts

    def from_urls(self, urls: list) -> list[Article]:
        arts: list[Article] = []
        for u in urls:
            self.log(f"→ {u}")
            html, err = self.f.fetch(u)
            if not html:
                self.log(f"  ⚠ {page_error_text(err)}")
                continue
            title = html_title(html)
            ext = extract_article(html, title_hint=title)
            self.log(f"  {len(ext.text)} симв., уверенность {ext.confidence:.2f}, метод {ext.method}")
            arts.append(Article(
                title=title or ext.title, body=ext.text, url=u,
                date=html_date(html), source=urllib.parse.urlparse(u).netloc,
                method=ext.method, confidence=ext.confidence,
            ))
        return arts

    def score_all(self, arts: list[Article], include_url: bool = False) -> list[Article]:
        """CLI по умолчанию include_url=False: домен берётся только из текста.

        ФИКС 1: в analyze уходят и заголовок, и тело.
        """
        for a in arts:
            if not a.ts:
                a.ts = parse_date(a.date)
            text = (a.title + "\n" + a.body).strip()
            if len(text) < 80:
                a.score, a.verdict = 1.0, "нет текста"
                a.caps = []
                continue
            an = self.p.analyze("", title=a.title, body=a.body, url=a.url, include_url=include_url)
            self.p.score(an, self.w.sensor_weight, self.w.H_scale)
            a.score, a.verdict = an.score, an.verdict
            a.domains, a.flags = an.domains, an.flags
            a.primary = an.primary
            a.caps = list(an.caps)
        return arts

    @staticmethod
    def keyword_filter(arts, inc, all_, exc):
        def rx(ws):
            return [re.compile(r"\b" + re.escape(w) + r"\w*", re.I | re.U) for w in ws]

        INC, ALL, EXC = rx(inc), rx(all_), rx(exc)
        out = []
        for a in arts:
            t = (a.title + " " + a.body).lower()
            if INC and not any(p.search(t) for p in INC):
                continue
            if ALL and not all(p.search(t) for p in ALL):
                continue
            if EXC and any(p.search(t) for p in EXC):
                continue
            out.append(a)
        return out


def fetch_page(url: str, include_url: bool = True) -> dict:
    """Одна страница: сеть не бросает исключение наружу, а возвращает ok=false."""
    html, err = get_fetcher().fetch(url)
    if not html:
        return {
            "url": url, "ok": False, "title": "", "date": "", "method": "",
            "confidence": 0.0, "candidates": 0, "chars": 0, "analysis": None,
            "error": page_error_text(err),
        }
    title = html_title(html)
    ext = extract_article(html, title_hint=title)
    page_title = title or ext.title
    analysis = evaluate("", title=page_title, body=ext.text, url=url, include_url=include_url)
    return {
        "url": url, "ok": True, "title": page_title, "date": html_date(html),
        "method": ext.method, "confidence": ext.confidence, "candidates": ext.candidates,
        "chars": len(ext.text), "analysis": analysis_dict(analysis),
    }


def run_feeds(feeds: list[str] | None, limit: int = 25, since: str = "", include_url: bool = True) -> dict:
    parser, w = build_parser()
    finder = Finder(parser, w)
    selected = resolve_feeds(feeds)
    per = max(1, min(int(limit or 25), 40))
    arts = finder.from_feeds(selected, limit_per=per)
    arts = finder.score_all(arts, include_url=include_url)
    arts = date_filter(arts, since or "", "")
    arts = dedup(arts)
    arts.sort(key=lambda a: a.score)
    if limit:
        arts = arts[: int(limit)]
    items = [
        {
            "title": a.title, "url": a.url, "source": a.source, "date": a.date,
            "score": a.score, "verdict": a.verdict, "primary": a.primary, "caps": list(a.caps),
        }
        for a in arts
    ]
    return {"items": items, "errors": list(finder.last_errors), "count": len(items)}


def train_model(epochs: int | None = None, lr: float | None = None, seeds: list | None = None) -> dict:
    """Обучение под замком. Метки не инвертируются: 1 = шум, 0 = достоверная."""
    global _weights_cache
    with _state_lock:
        ensure_seed()
        if seeds:
            data = []
            for raw in seeds:
                row = normalize_row(raw)
                if "text" not in row or "label" not in row:
                    raise ValueError("В каждом примере нужны text (или title/body) и label")
                if row["label"] not in (0, 1):
                    raise ValueError("label может быть только 0 или 1")
                data.append(row)
            append_dataset(data)
        else:
            data = load_dataset()
        if len(data) < 2:
            raise ValueError("Для разбиения train/test нужно хотя бы два примера")
        w = Weights.load(WEIGHTS_PATH)
        if epochs is not None:
            w.epochs = max(1, int(epochs))
        if lr is not None:
            w.lr = float(lr)
        lex = [Sensor(s.key, s.label, list(s.patterns), s.weight, s.margin) for s in LEXICON]
        result = Trainer(Parser(lex), w).train(data, log=logger.info, save=WEIGHTS_PATH)
        _weights_cache = w
        result["weights"] = dict(w.sensor_weight)
        return result


def save_weights_payload(payload: dict) -> dict:
    w = current_weights()
    fresh = Weights(dict(w.sensor_weight), dict(w.sensor_margin), w.H_scale, w.lr, w.epochs)
    if any(k in payload for k in ("sensor_weight", "sensor_margin", "H_scale", "lr", "epochs")):
        if "sensor_weight" in payload:
            fresh.sensor_weight.update({str(k): float(v) for k, v in payload["sensor_weight"].items()})
        if "sensor_margin" in payload:
            fresh.sensor_margin.update({str(k): float(v) for k, v in payload["sensor_margin"].items()})
        if payload.get("H_scale") is not None:
            fresh.H_scale = float(payload["H_scale"])
        if payload.get("lr") is not None:
            fresh.lr = float(payload["lr"])
        if payload.get("epochs") is not None:
            fresh.epochs = int(payload["epochs"])
    else:
        numeric = {
            str(k): float(v)
            for k, v in payload.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if not numeric:
            raise ValueError("Нет числовых весов для сохранения")
        fresh.sensor_weight.update(numeric)
    set_weights(fresh)
    return asdict(fresh)


def lexicon_public() -> list[dict]:
    w = current_weights()
    return [
        {
            "key": s.key,
            "label": s.label,
            "patterns": list(s.patterns),
            "weight": w.sensor_weight.get(s.key, s.weight),
            "margin": w.sensor_margin.get(s.key, s.margin),
        }
        for s in LEXICON
    ]
