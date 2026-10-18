#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
НИЖЕГОРОДСКАЯ ФОРМУЛА v8.0 — CLI и TUI.

Формулы, лексикон и обучение живут в core.py.
Этот файл остаётся терминальным интерфейсом, а не заглушкой.
Режим score_include_url в обходе ленты выключен: домен берётся только из текста,
как в исходном CLI. Тело статьи в разбор передаётся (фикс 1).
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import asdict

from core import (
    APP_NAME,
    APP_VER,
    FEEDS_DEFAULT,
    Article,
    Fetcher,
    Finder,
    Trainer,
    build_parser,
    configure_logging,
    date_filter,
    dedup,
    ensure_seed,
    extract_article,
    html_title,
    load_dataset,
    strip_html,
)

configure_logging()

TABS = ["Дашборд", "Лента", "Фильтры", "Разметка"]
VERDICT_COLOR = {5: "good", 4: "good", 3: "warn", 2: "bad", 1: "bad"}


def _init_colors():
    import curses
    curses.start_color()
    curses.use_default_colors()
    pairs = [
        (1, curses.COLOR_GREEN, -1), (2, curses.COLOR_YELLOW, -1),
        (3, curses.COLOR_RED, -1), (4, curses.COLOR_CYAN, -1),
        (5, curses.COLOR_MAGENTA, -1), (6, curses.COLOR_BLUE, -1),
    ]
    for i, f, b in pairs:
        try:
            curses.init_pair(i, f, b)
        except Exception:
            # терминал без палитры — остаёмся на атрибутах по умолчанию
            pass
    return {
        "good": curses.color_pair(1), "warn": curses.color_pair(2),
        "bad": curses.color_pair(3), "info": curses.color_pair(4),
        "accent": curses.color_pair(6), "dim": curses.A_DIM,
        "sel": curses.A_REVERSE | curses.A_BOLD, "bold": curses.A_BOLD,
    }


class App:
    def __init__(self, stdscr):
        import curses
        self.scr = stdscr
        self.C = _init_colors()
        self.tab = 0
        self.cursor = 0
        self.scroll = 0
        self.arts: list[Article] = []
        self.status = ""
        self.filters = {
            "kw": [], "all": [], "exc": [], "since": "", "until": "",
            "max": 5.0, "suspicious": False, "feeds": list(FEEDS_DEFAULT.keys()),
        }
        self.detail: Article | None = None
        self.train_log: list[str] = []
        self.input_field = 0
        self.pending_label: Article | None = None
        self.marked = {"pos": 0, "neg": 0}
        self.p, self.w = build_parser()
        curses.curs_set(0)

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addstr(y, x, s[:max(0, w - x - 1)], attr)
        except Exception:
            # край экрана: curses бросает, если символ не влезает
            pass

    def hline(self, y, ch="─", x0=0, x1=None):
        _h, w = self.scr.getmaxyx()
        x1 = x1 or w
        self.put(y, x0, ch * (x1 - x0), self.C["dim"])

    def draw_header(self):
        _h, w = self.scr.getmaxyx()
        self.put(0, 0, " " * w, self.C["accent"])
        self.put(0, 1, f" {APP_NAME} {APP_VER} ", self.C["accent"] | self.C["bold"])
        right = f"новостей: {len(self.arts)} ".rjust(max(0, w - 32))
        self.put(0, max(1, w - len(right) - 1), right, self.C["accent"])
        x = 2
        for i, t in enumerate(TABS):
            attr = self.C["sel"] if i == self.tab else self.C["dim"]
            self.put(1, x, f" {t} ", attr)
            x += len(t) + 3
        self.hline(2)

    def draw_footer(self):
        h, w = self.scr.getmaxyx()
        self.hline(h - 2)
        keys = ("q выход · ←→ вкладки · ↑↓ выбор · Enter детали · "
                "t обучить · r лента · s метка · d добросовестная")
        self.put(h - 1, 1, keys[:w - 2], self.C["dim"])
        if self.status:
            self.put(h - 2, 1, self.status[:w - 2], self.C["info"])

    def draw_dashboard(self):
        y = 4
        h, w = self.scr.getmaxyx()
        right = max(46, w // 2)
        self.put(y, 2, "СОСТОЯНИЕ МОДЕЛИ", self.C["bold"])
        y += 1
        data = load_dataset()
        if data:
            tr = Trainer(self.p, self.w)
            fmt = [self.p.analyze(d["text"], include_url=False) for d in data]
            gold = [d["label"] for d in data]
            preds = [tr._pred(a, self.w) for a in fmt]
            acc = sum(1 for a, b in zip(preds, gold) if a == b) / max(1, len(gold))
            f1 = tr.f1(preds, gold)
        else:
            acc = f1 = 0.0
        rows = [
            ("Примеров", f"{len(data)}"),
            ("Точность (вся выборка)", f"{acc:.3f}"),
            ("F1 (вся выборка)", f"{f1:.3f}"),
            ("Вес H_scale", f"{self.w.H_scale:.2f}"),
            ("Размечено вручную", f"+{self.marked['pos']} / −{self.marked['neg']}"),
        ]
        for k, v in rows:
            self.put(y, 4, f"{k:<24}")
            self.put(y, 30, v, self.C["bold"])
            y += 1
        y += 1
        self.put(y, 2, "ВЕСА СЕНСОРОВ", self.C["bold"])
        y += 1
        for k, v in sorted(self.w.sensor_weight.items(), key=lambda x: -x[1]):
            bar = "█" * int(round(min(v, 3) / 3 * 22))
            col = self.C["good"] if v >= 1.1 else self.C["warn"] if v >= 0.8 else self.C["bad"]
            self.put(y, 4, f"{k:<16}{v:>5.2f} ")
            self.put(y, 28, bar, col)
            y += 1
        x = right
        self.put(4, x, "ЖУРНАЛ", self.C["bold"])
        for i, line in enumerate(self.train_log[-max(1, h - 9):]):
            self.put(5 + i, x, line[:w - x - 2], self.C["dim"])

    def draw_feed(self):
        h, w = self.scr.getmaxyx()
        top, bottom = 4, h - 3
        self.put(3, 2, "ЛЕНТА НОВОСТЕЙ", self.C["bold"])
        if not self.arts:
            self.put(top + 1, 4, "Лента пуста. Нажми r — загрузить RSS, или u — ввести URL.", self.C["dim"])
            return
        visible = max(1, (bottom - top) // 2)
        self.cursor = max(0, min(self.cursor, len(self.arts) - 1))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1
        for i in range(self.scroll, min(len(self.arts), self.scroll + visible)):
            a = self.arts[i]
            y = top + (i - self.scroll) * 2
            sel = i == self.cursor
            col = self.C[VERDICT_COLOR.get(int(round(a.score)), "dim")]
            bar = "█" * int(round(a.score)) + "░" * (5 - int(round(a.score)))
            title = textwrap.shorten(a.title or "(без заголовка)", width=max(20, w - 44))
            line = f"{a.score:>4.1f} {bar} {title}"
            attr = (self.C["sel"] if sel else 0) | (col if not sel else self.C["sel"])
            self.put(y, 2, line.ljust(w - 4), attr)
            meta = f"      {a.source:<18} {a.date[:16]:<17} {a.verdict}"
            self.put(y + 1, 2, meta[:w - 4], self.C["dim"] if not sel else self.C["sel"])
        if self.detail:
            self.draw_detail()

    def draw_detail(self):
        h, w = self.scr.getmaxyx()
        a = self.detail
        w2 = min(90, w - 8)
        x0 = max(2, (w - w2) // 2)
        for yy in range(5, h - 4):
            self.put(yy, x0, " " * w2, self.C["accent"])
        y = 6
        self.put(y, x0 + 2, "КАРТОЧКА НОВОСТИ", self.C["accent"] | self.C["bold"])
        y += 2
        self.put(y, x0 + 2, textwrap.shorten(a.title or "-", width=w2 - 4), self.C["bold"])
        y += 1
        self.put(y, x0 + 2, f"{a.source} · {a.date} · {a.verdict} · {a.score}/5")
        y += 1
        self.put(y, x0 + 2, f"URL: {a.url[:w2 - 8]}", self.C["dim"])
        y += 2
        self.put(y, x0 + 2, "СИГНАЛЫ:", self.C["bold"])
        y += 1
        for k, v in sorted(a.flags.items(), key=lambda x: -x[1])[:7]:
            bar = "█" * int(round(v * 16))
            col = self.C["bad"] if v > .6 else self.C["warn"] if v > .35 else self.C["dim"]
            self.put(y, x0 + 4, f"{k:<16}{v:>5.2f} ")
            self.put(y, x0 + 28, bar, col)
            y += 1
        self.put(h - 5, x0 + 2, "[Esc] закрыть · [s] манипулятивная · [d] добросовестная", self.C["dim"])

    def fields(self):
        f = self.filters
        return [
            ("Ключевые слова (любое)", ", ".join(f["kw"]), "kw"),
            ("Все слова обязательны", ", ".join(f["all"]), "all"),
            ("Исключить слова", ", ".join(f["exc"]), "exc"),
            ("Дата с", f["since"], "since"),
            ("Дата по", f["until"], "until"),
            ("Макс. балл", f"{f['max']:.1f}", "max"),
            ("Только сомнительные", "да" if f["suspicious"] else "нет", "suspicious"),
            ("Фиды", ", ".join(f["feeds"]), "feeds"),
        ]

    def draw_filters(self):
        y = 5
        h, w = self.scr.getmaxyx()
        self.put(3, 2, "ФИЛЬТРЫ  (Enter — править · g — применить · R — сбросить)", self.C["bold"])
        for i, (label, val, _key) in enumerate(self.fields()):
            sel = i == self.input_field
            attr = self.C["sel"] if sel else 0
            self.put(y, 4, f"{label:<28}", attr or self.C["bold"])
            self.put(y, 34, textwrap.shorten(val or "(пусто)", width=max(10, w - 40)), attr or 0)
            y += 1

    def draw_marking(self):
        h, w = self.scr.getmaxyx()
        self.put(3, 2, "РАЗМЕТКА  (s — манипулятивная, d — добросовестная)", self.C["bold"])
        y = 5
        if self.pending_label:
            a = self.pending_label
            self.put(y, 4, "Текущая для разметки:", self.C["bold"])
            y += 1
            self.put(y, 6, textwrap.shorten(a.title or "-", width=w - 10))
            y += 1
            self.put(y, 6, f"{a.source} · {a.score}/5 · {a.verdict}", self.C["dim"])
            y += 1
        else:
            self.put(y, 4, "Выбери новость в «Ленте» и нажми s или d.", self.C["dim"])
            y += 1
        y += 1
        self.put(y, 4, f"манипулятивных (1): {self.marked['pos']}    добросовестных (0): {self.marked['neg']}")
        y += 2
        self.put(y, 4, "Последние записи:", self.C["bold"])
        y += 1
        for row in load_dataset()[-max(1, h - y - 3):]:
            lbl = "манип." if row.get("label") == 1 else "доброс."
            col = self.C["bad"] if row.get("label") == 1 else self.C["good"]
            self.put(y, 6, f"[{lbl}] ", col)
            self.put(y, 16, textwrap.shorten(row.get("text", ""), width=w - 20))
            y += 1

    def redraw(self):
        self.scr.erase()
        self.draw_header()
        if self.tab == 0:
            self.draw_dashboard()
        elif self.tab == 1:
            self.draw_feed()
        elif self.tab == 2:
            self.draw_filters()
        elif self.tab == 3:
            self.draw_marking()
        self.draw_footer()
        self.scr.refresh()

    def do_load_feeds(self):
        self.status = "Загрузка RSS-фидов..."
        self.redraw()
        finder = Finder(self.p, self.w)
        finder.log = lambda m: (setattr(self, "status", m), self.redraw())
        feeds = {k: v for k, v in FEEDS_DEFAULT.items() if k in self.filters["feeds"]}
        arts = finder.from_feeds(feeds, limit_per=12)
        arts = finder.score_all(arts, include_url=False)
        arts = self.apply_filters(arts)
        arts = dedup(arts)
        self.arts = sorted(arts, key=lambda a: a.score)
        self.cursor = self.scroll = 0
        self.status = f"Загружено {len(self.arts)} (после фильтров и дедупа)"

    def do_load_urls(self):
        urls = [u.strip() for u in self.read_input("URL-ы через запятую: ").split(",") if u.strip()]
        if not urls:
            return
        finder = Finder(self.p, self.w)
        finder.log = lambda m: (setattr(self, "status", m), self.redraw())
        arts = finder.score_all(finder.from_urls(urls), include_url=False)
        self.arts = dedup(arts + self.arts)
        self.status = f"Добавлено {len(arts)}"

    def apply_filters(self, arts):
        f = self.filters
        picked = Finder.keyword_filter(arts, f["kw"], f["all"], f["exc"])
        picked = date_filter(picked, f["since"], f["until"])
        if f["max"] < 5.0:
            picked = [x for x in picked if x.score <= f["max"]]
        if f["suspicious"]:
            picked = [x for x in picked if x.score < 3.5]
        return picked

    def do_train(self):
        ensure_seed()
        data = load_dataset()
        self.train_log = [f"данных: {len(data)}"]
        self.tab = 0
        self.redraw()
        tr = Trainer(self.p, self.w)
        tr.train(data, log=lambda m: (self.train_log.append(m), self.redraw()))
        self.p, self.w = build_parser()
        self.status = "Обучение завершено. Метрики в журнале — на тестовой части."
        self.redraw()

    def read_input(self, prompt: str) -> str:
        import curses
        h, w = self.scr.getmaxyx()
        curses.curs_set(1)
        buf = ""
        while True:
            self.put(h - 1, 1, " " * (w - 2))
            self.put(h - 1, 1, (prompt + buf)[:w - 2], self.C["info"])
            self.scr.refresh()
            ch = self.scr.get_wch()
            if ch in ("\n", "\r"):
                break
            if ch == "\x1b":
                buf = ""
                break
            if ch in ("\x7f", "\b", "KEY_BACKSPACE"):
                buf = buf[:-1]
                continue
            if isinstance(ch, str) and ch.isprintable():
                buf += ch
        curses.curs_set(0)
        return buf.strip()

    def mark(self, label: int):
        from core import append_dataset
        if self.tab == 1 and self.arts:
            self.pending_label = self.arts[self.cursor]
        a = self.pending_label
        if not a:
            self.status = "Нет выбранной новости"
            return
        append_dataset([{
            "text": (a.title + "\n" + a.body).strip(),
            "label": label,
            "url": a.url,
            "source": a.source,
        }])
        if label == 1:
            self.marked["pos"] += 1
        else:
            self.marked["neg"] += 1
        self.status = f"Размечено: {(a.title or '')[:40]} → {'манипулятивная' if label else 'добросовестная'}"

    def _apply_filter_input(self, key, val):
        f = self.filters
        if key in ("kw", "all", "exc", "feeds"):
            f[key] = [w.strip() for w in val.split(",") if w.strip()]
        elif key in ("since", "until"):
            f[key] = val
        elif key == "max":
            try:
                f["max"] = float(val)
            except Exception:
                # нечисловой порог не затирает предыдущий максимум осмысленным мусором
                f["max"] = 5.0
        elif key == "suspicious":
            f["suspicious"] = val.strip().lower() in ("да", "д", "y", "yes", "1", "true")

    def run(self):
        while True:
            self.redraw()
            ch = self.scr.get_wch()
            if ch in ("q", "Q"):
                break
            if ch == "KEY_RESIZE":
                continue
            if ch == "KEY_LEFT":
                self.tab = (self.tab - 1) % len(TABS)
                continue
            if ch == "KEY_RIGHT":
                self.tab = (self.tab + 1) % len(TABS)
                continue
            if self.tab == 1:
                if ch == "KEY_UP":
                    self.cursor -= 1
                elif ch == "KEY_DOWN":
                    self.cursor += 1
                elif ch == "KEY_NPAGE":
                    self.cursor += 10
                elif ch == "KEY_PPAGE":
                    self.cursor -= 10
                elif ch in ("\n", "\r"):
                    if self.arts:
                        self.detail = self.arts[self.cursor]
                elif ch == "\x1b":
                    self.detail = None
                elif ch == "s":
                    self.mark(1)
                elif ch == "d":
                    self.mark(0)
                elif ch == "r":
                    self.do_load_feeds()
                elif ch == "u":
                    self.do_load_urls()
            elif self.tab == 0:
                if ch == "t":
                    self.do_train()
                elif ch == "r":
                    self.do_load_feeds()
            elif self.tab == 2:
                fs = self.fields()
                if ch == "KEY_UP":
                    self.input_field = (self.input_field - 1) % len(fs)
                elif ch == "KEY_DOWN":
                    self.input_field = (self.input_field + 1) % len(fs)
                elif ch in ("\n", "\r"):
                    label, _val, key = fs[self.input_field]
                    new = self.read_input(f"{label}: ")
                    self._apply_filter_input(key, new)
                elif ch == "g" and self.arts:
                    self.arts = sorted(self.apply_filters(self.arts), key=lambda a: a.score)
                    self.cursor = self.scroll = 0
                    self.status = f"Применено: {len(self.arts)}"
                elif ch == "R":
                    self.filters = {
                        "kw": [], "all": [], "exc": [], "since": "", "until": "",
                        "max": 5.0, "suspicious": False, "feeds": list(FEEDS_DEFAULT.keys()),
                    }
                    self.status = "Фильтры сброшены"
            elif self.tab == 3:
                if ch == "s":
                    self.mark(1)
                elif ch == "d":
                    self.mark(0)
                elif ch == "t":
                    self.do_train()


def run_tui():
    import curses
    curses.wrapper(lambda s: App(s).run())


def out_article(a: Article) -> str:
    return (
        f"{a.score:>4.1f}/5  {a.verdict:<18} {a.title[:70]}\n"
        f"        {a.source:<20} {a.date[:16]:<17} {a.url[:60]}"
    )


def cmd_train(args):
    ensure_seed()
    data = load_dataset(args.file)
    print(f"  данных: {len(data)}")
    parser, w = build_parser()
    result = Trainer(parser, w).train(data, log=print)
    print(
        f"  test F1={result['f1']:.3f} P={result['precision']:.3f} "
        f"R={result['recall']:.3f} acc={result['accuracy']:.3f} "
        f"n_train={result['n_train']} n_test={result['n_test']}"
    )


def cmd_feed(args):
    ensure_seed()
    parser, w = build_parser()
    finder = Finder(parser, w)
    finder.log = print
    feeds = {k: v for k, v in FEEDS_DEFAULT.items() if (not args.feeds or k in args.feeds)}
    arts = finder.from_feeds(feeds, args.limit_per)
    arts = finder.score_all(arts, include_url=False)
    arts = finder.keyword_filter(arts, args.kw, args.all, args.exclude)
    arts = date_filter(arts, args.since, args.until)
    if args.max < 5:
        arts = [a for a in arts if a.score <= args.max]
    if args.suspicious:
        arts = [a for a in arts if a.score < 3.5]
    before = len(arts)
    arts = dedup(arts)
    arts.sort(key=lambda a: a.score)
    arts = arts[:args.limit]
    print(f"\n  после фильтра: {len(arts)} (дедуп убрал {before - len(arts)})\n")
    if finder.last_errors:
        for err in finder.last_errors:
            print(f"  ⚠ {err['error']}: {err['feed']}")
    if args.json:
        json.dump([asdict(a) for a in arts], sys.stdout, ensure_ascii=False, indent=2)
    else:
        for a in arts:
            print(out_article(a))


def cmd_fetch(args):
    parser, w = build_parser()
    finder = Finder(parser, w)
    finder.log = print
    arts = finder.from_urls(args.urls) if args.urls else []
    if args.feeds_url:
        arts += finder.from_feeds({u: u for u in args.feeds_url}, args.limit_per)
    arts = finder.score_all(arts, include_url=False)
    arts = finder.keyword_filter(arts, args.kw, args.all, args.exclude)
    arts = date_filter(arts, args.since, args.until)
    before = len(arts)
    arts = dedup(arts)
    arts.sort(key=lambda a: a.score)
    print(f"\n  результатов: {len(arts)} (дедуп убрал {before - len(arts)})\n")
    if args.json:
        json.dump([asdict(a) for a in arts], sys.stdout, ensure_ascii=False, indent=2)
    else:
        for a in arts[:args.limit]:
            print(out_article(a))


def cmd_extract(args):
    fetcher = Fetcher()
    for u in args.urls:
        html = fetcher.get(u, silent=False)
        if not html:
            continue
        ext = extract_article(html, title_hint=html_title(html))
        page = strip_html(html)
        print("=" * 74)
        print(f"URL    : {u}")
        print(f"метод  : {ext.method} | тело: {len(ext.text)} симв. | уверенность {ext.confidence:.2f}")
        print(f"узел   : <{ext.node_tag} class=\"{ext.node_class}\"> | кандидатов: {ext.candidates}")
        print(f"страница: {len(page)} симв.")
        print("-" * 74)
        print(ext.text[:800])


def cmd_report(_args):
    data = load_dataset()
    parser, w = build_parser()
    tr = Trainer(parser, w)
    fmt = [parser.analyze(d["text"], include_url=False) for d in data]
    gold = [d["label"] for d in data]
    preds = [tr._pred(a, w) for a in fmt]
    print(f"  примеров: {len(data)} | точность: {tr.accuracy(preds, gold):.3f} | F1: {tr.f1(preds, gold):.3f}")
    print(f"  precision: {tr.precision(preds, gold):.3f} | recall: {tr.recall(preds, gold):.3f}")
    print("  веса: " + ", ".join(f"{k}={v:.2f}" for k, v in sorted(w.sensor_weight.items(), key=lambda x: -x[1])))
    print("  F1 на всей выборке смещён вверх. Честные цифры — после train, на тестовой части.")


def main():
    ap = argparse.ArgumentParser(prog="nizhny", description=f"{APP_NAME} {APP_VER} — обучаемый детектор новостей")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ui", help="терминальный интерфейс (по умолчанию)")
    s_tr = sub.add_parser("train", help="обучить на dataset.jsonl")
    s_tr.add_argument("file", nargs="?", default=None)
    s_fd = sub.add_parser("feed", help="прогон RSS-фидов")
    s_fd.add_argument("--feeds", nargs="*", default=None)
    s_fd.add_argument("--limit-per", type=int, default=15)
    s_fd.add_argument("-k", "--kw", nargs="*", default=[])
    s_fd.add_argument("-a", "--all", nargs="*", default=[])
    s_fd.add_argument("-x", "--exclude", nargs="*", default=[])
    s_fd.add_argument("--since", default="")
    s_fd.add_argument("--until", default="")
    s_fd.add_argument("--max", type=float, default=5.0)
    s_fd.add_argument("--suspicious", action="store_true")
    s_fd.add_argument("--limit", type=int, default=25)
    s_fd.add_argument("--json", action="store_true")
    s_ft = sub.add_parser("fetch", help="прогон конкретных URL/RSS")
    s_ft.add_argument("--urls", nargs="*", default=[])
    s_ft.add_argument("--feeds-url", nargs="*", default=[])
    s_ft.add_argument("--limit-per", type=int, default=20)
    s_ft.add_argument("-k", "--kw", nargs="*", default=[])
    s_ft.add_argument("-a", "--all", nargs="*", default=[])
    s_ft.add_argument("-x", "--exclude", nargs="*", default=[])
    s_ft.add_argument("--since", default="")
    s_ft.add_argument("--until", default="")
    s_ft.add_argument("--limit", type=int, default=25)
    s_ft.add_argument("--json", action="store_true")
    s_ex = sub.add_parser("extract", help="проверить экстрактор")
    s_ex.add_argument("--urls", nargs="+", required=True)
    sub.add_parser("report", help="метрики")
    sub.add_parser("mark", help="подсказка по разметке")
    args = ap.parse_args()
    if args.cmd in (None, "ui"):
        if not sys.stdout.isatty():
            print("TUI требует терминала. Команды: train, feed, fetch, extract, report")
            print("Веб: uvicorn app:app --reload")
            return
        run_tui()
        return
    if args.cmd == "train":
        cmd_train(args)
    elif args.cmd == "feed":
        cmd_feed(args)
    elif args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "mark":
        print("Разметка: python3 nizhny.py, вкладка «Лента», [s] манипулятивная, [d] добросовестная.")
        print("В вебе обучение закрыто токеном администратора NIZHNY_ADMIN_TOKEN.")


if __name__ == "__main__":
    main()
