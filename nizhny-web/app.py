#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP-слой «Нижегородской формулы». Скоринг живёт в core.py."""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import core

core.configure_logging()
logger = logging.getLogger("nizhny.http")

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
ADMIN_ENV = "NIZHNY_ADMIN_TOKEN"

app = FastAPI(title=core.APP_NAME, version=core.APP_VER)
# Плагин открывается с chrome-extension:// и moz-extension://, происхождение заранее неизвестно.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class AnalyzeIn(BaseModel):
    text: str = ""
    title: str | None = None
    body: str | None = None
    url: str | None = None
    weights: dict | None = None
    H_scale: float | None = None
    include_url: bool | None = None


class FetchIn(BaseModel):
    url: str
    include_url: bool | None = None


class FeedIn(BaseModel):
    feeds: list[str] | None = None
    limit: int | None = None
    since: str | None = None
    include_url: bool | None = None


class TrainIn(BaseModel):
    epochs: int | None = None
    lr: float | None = None
    seeds: list[dict] | None = None
    admin_token: str | None = None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _admin_token() -> str:
    return os.environ.get(ADMIN_ENV, "").strip()


def _token_matches(got: str) -> bool:
    expected = _admin_token()
    if not expected or not got:
        return False
    a, b = got.encode(), expected.encode()
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def _require_admin(request: Request, body_token: str | None = None) -> JSONResponse | None:
    """Обучение и запись весов — только администратор. Токен берётся из окружения."""
    if not _admin_token():
        return _error(403, "Обучение выключено: администратор не задал переменную NIZHNY_ADMIN_TOKEN")
    got = (request.headers.get("x-admin-token") or body_token or "").strip()
    if not _token_matches(got):
        return _error(403, "Обучение доступно только администратору")
    return None


def _has_text(payload: AnalyzeIn) -> bool:
    return bool((payload.text or "").strip() or (payload.title or "").strip() or (payload.body or "").strip())


def _run(payload: AnalyzeIn, include_url: bool) -> dict:
    return core.analysis_dict(core.evaluate(
        text=payload.text or "",
        title=payload.title or "",
        body=payload.body or "",
        url=payload.url or "",
        weights=payload.weights,
        H_scale=payload.H_scale,
        include_url=include_url,
    ))


@app.exception_handler(RequestValidationError)
async def _bad_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Схема не совпала — человеку нужен короткий JSON, не трассировка pydantic.
    logger.debug("validation failed on %s: %s", request.url.path, exc)
    return _error(400, "Некорректное тело запроса")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Клиенту не отдаём стектрейс: причина пишется в журнал сервера.
    logger.exception("unhandled error on %s", request.url.path)
    return _error(500, "Внутренняя ошибка сервера")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX, media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": core.APP_VER, "morph": core.morph_available(), "weights": core.weights_on_disk()}


@app.post("/api/analyze")
def analyze(payload: AnalyzeIn):
    if not _has_text(payload):
        return _error(400, "Нужен текст, заголовок или тело статьи")
    include = True if payload.include_url is None else bool(payload.include_url)
    try:
        return _run(payload, include)
    except ValueError as exc:
        return _error(400, str(exc))


@app.post("/api/fetch")
def fetch(payload: FetchIn):
    url = (payload.url or "").strip()
    if not url:
        return _error(400, "Нужен адрес страницы")
    if not (url.startswith("http://") or url.startswith("https://")):
        return {
            "url": url, "ok": False, "title": "", "date": "", "method": "",
            "confidence": 0.0, "candidates": 0, "chars": 0, "analysis": None,
            "error": "Нужен адрес, который начинается с http:// или https://",
        }
    include = True if payload.include_url is None else bool(payload.include_url)
    return core.fetch_page(url, include_url=include)


@app.post("/api/feed")
def feed(payload: FeedIn):
    limit = payload.limit if payload.limit is not None else 25
    if limit < 1 or limit > 100:
        return _error(400, "Лимит должен быть от 1 до 100")
    include = True if payload.include_url is None else bool(payload.include_url)
    try:
        return core.run_feeds(payload.feeds, limit=limit, since=payload.since or "", include_url=include)
    except Exception as exc:
        # Один сломанный обход не должен становиться 500 со стектрейсом.
        logger.warning("feed failed: %s", exc)
        return {"items": [], "errors": [{"feed": "", "error": "лента не собрана"}], "count": 0}


@app.post("/api/compare")
def compare(payload: AnalyzeIn):
    if not _has_text(payload):
        return _error(400, "Нужен текст для сравнения режимов")
    try:
        with_url = _run(payload, True)
        without_url = _run(payload, False)
    except ValueError as exc:
        return _error(400, str(exc))
    return {"with_url": with_url, "without_url": without_url, "delta_score": round(with_url["score"] - without_url["score"], 2)}


@app.post("/api/train")
async def train(payload: TrainIn, request: Request):
    denied = _require_admin(request, payload.admin_token)
    if denied is not None:
        return denied
    try:
        return core.train_model(epochs=payload.epochs, lr=payload.lr, seeds=payload.seeds)
    except ValueError as exc:
        return _error(400, str(exc))


@app.get("/api/weights")
def weights_get() -> dict:
    return core.asdict(core.current_weights())


@app.post("/api/weights")
async def weights_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        # тело не JSON — это ошибка запроса, не сбой формулы
        return _error(400, "Нужен JSON с весами")
    if not isinstance(payload, dict):
        return _error(400, "Нужен JSON-объект с весами")
    denied = _require_admin(request, payload.get("admin_token"))
    if denied is not None:
        return denied
    clean = {k: v for k, v in payload.items() if k != "admin_token"}
    try:
        return core.save_weights_payload(clean)
    except ValueError as exc:
        return _error(400, str(exc))


@app.get("/api/lexicon")
def lexicon() -> list[dict]:
    return core.lexicon_public()
