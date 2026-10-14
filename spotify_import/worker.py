"""Unattended operation: Telegram messages, daily reports, automatic resends, halts.

A halt is persistent: once the worker stops for review, restarting the process
(or the container) sends nothing until someone runs `resume`.
"""

import json
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .api import APIError, TransportError
from .engine import DAY, backoff, status
from .history import resend

# Failures Last.fm describes as temporary; the worker waits them out.
TRANSIENT = (8, 11, 16, 29)
# Automatic resends per event before it is left for a human.
MAX_AUTO_RESENDS = 2
MISSING_MIN_AGE = 3600
# A sending session is over after this long without a new batch.
SESSION_QUIET = 900


class Notifier:
    """Sends Telegram messages; never raises and never prints the bot token."""

    def __init__(self, values):
        self.token = values.get("TELEGRAM_BOT_TOKEN") or ""
        self.chat = values.get("TELEGRAM_CHAT_ID") or ""

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def call(self, method, params):
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=urlencode(params).encode(),
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read(1_000_000))

    def send(self, text):
        print(text, flush=True)
        if not self.enabled:
            return False
        try:
            return bool(
                self.call(
                    "sendMessage",
                    {
                        "chat_id": self.chat,
                        "text": text,
                        "disable_web_page_preview": "true",
                    },
                ).get("ok")
            )
        except Exception as error:  # A notification must never stop the import.
            print(f"Telegram message failed ({type(error).__name__})", flush=True)
            return False

    def chats(self):
        """Chats that recently wrote to the bot, to find TELEGRAM_CHAT_ID."""
        updates = self.call("getUpdates", {}).get("result", [])
        found = {}
        for update in updates:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                found[chat["id"]] = chat.get("username") or chat.get("title") or ""
        return [{"chat_id": k, "name": v} for k, v in found.items()]


def halt(store, notifier, reason, now):
    with store.conn:
        store.set("halted", {"reason": reason, "at": now})
    notifier.send(
        "⛔ Импорт Last.fm остановлен и ждёт решения.\n"
        f"Причина: {reason}\n\n"
        "Ничего не отправляется. Разобраться, затем продолжить:\n"
        'spotify-import resume --note "что проверено"'
    )


def resume(store, note, now=None):
    now = int(time.time()) if now is None else now
    halted = store.get("halted")
    if not halted:
        raise ValueError("The worker is not halted")
    if not note.strip():
        raise ValueError("Note required")
    with store.conn:
        store.set(
            "halt_history",
            (store.get("halt_history") or [])
            + [{**halted, "resumed_at": now, "note": note}],
        )
        store.set("halted", None)
    return halted


def auto_resend(store, api, cfg, now):
    """Requeue accepted-but-absent scrobbles at most MAX_AUTO_RESENDS times each."""
    tries = {}
    for row in store.rows(
        "SELECT event_id, count(*) n FROM decisions WHERE action='resend' GROUP BY event_id"
    ):
        tries[row["event_id"]] = row["n"]
    missing = store.rows(
        """SELECT id, missing_at FROM events WHERE status='confirmed'
        AND verified_at IS NULL AND missing_at IS NOT NULL"""
    )
    due = [
        r["id"]
        for r in missing
        if now - r["missing_at"] >= MISSING_MIN_AGE
        and tries.get(r["id"], 0) < MAX_AUTO_RESENDS
    ]
    if not due:
        return {"requeued": 0, "found_on_recheck": 0}
    return resend(store, api, cfg, due, "automatic: still absent after re-check", now)


def needs_review(store):
    """Missing scrobbles that used up their automatic resends."""
    return store.rows(
        f"""SELECT e.id, e.artist, e.track FROM events e
        WHERE e.status='confirmed' AND e.verified_at IS NULL AND e.missing_at IS NOT NULL
        AND (SELECT count(*) FROM decisions d WHERE d.event_id=e.id AND d.action='resend')
            >= {MAX_AUTO_RESENDS}"""
    )


def summary(store, cfg, now, since=None, title="📊 Импорт Last.fm, сводка за сутки"):
    since = now - DAY if since is None else since
    sent = store.rows("SELECT count(*) n FROM attempts WHERE sent_at>?", (since,))[0][
        "n"
    ]
    stored = store.rows(
        """SELECT count(*) n FROM attempts a JOIN events e ON e.id=a.event_id
        WHERE a.sent_at>? AND a.outcome='confirmed' AND e.verified_at IS NOT NULL""",
        (since,),
    )[0]["n"]
    resent = store.rows(
        "SELECT count(*) n FROM decisions WHERE action='resend' AND time>?", (since,)
    )[0]["n"]
    state = status(store, cfg, now)
    total = state["confirmed_verified"]
    left = state["pending"]
    review = needs_review(store)
    lines = [
        title,
        f"Отправлено: {sent}, сохранено: {stored}",
        f"Выброшено Last.fm сейчас: {state['confirmed_missing']}, переотправлено: {resent}",
        f"Неясных: {state['uncertain']}, на ручной разбор: {len(review)}",
        f"В профиле: {total:,} из {total + left:,} ({total / max(1, total + left):.1%})".replace(
            ",", " "
        ),
        f"Осталось: {left:,}".replace(",", " ")
        + f", финиш ≈ {state['estimated_completion_utc']}",
    ]
    if store.get("halted"):
        lines.append(f"⛔ Остановлен: {store.get('halted')['reason']}")
    return "\n".join(lines)


def maybe_report(store, cfg, notifier, now):
    """Report each finished sending session; otherwise a daily sign of life.

    A session counts as finished once nothing was sent for SESSION_QUIET seconds,
    so the numbers are fresh instead of a day old.
    """
    last = store.get("last_report_at", 0)
    latest = store.rows("SELECT max(sent_at) t FROM attempts")[0]["t"] or 0
    local = datetime.fromtimestamp(now, ZoneInfo(cfg.account_timezone))
    if latest > last and now - latest >= SESSION_QUIET:
        text = summary(
            store,
            cfg,
            now,
            since=last or latest - DAY,
            title="✅ Импорт Last.fm: отправка завершена",
        )
    elif (
        local.hour >= cfg.report_hour
        and store.get("last_report_day") != local.date().isoformat()
        and now - latest >= DAY
    ):
        # Nothing sent for a day: say so daily, so silence never hides a stall.
        text = summary(store, cfg, now)
    else:
        return False
    notifier.send(text)
    with store.conn:
        store.set("last_report_at", now)
        store.set("last_report_day", local.date().isoformat())
    return True


def tick(store, api, cfg, notifier, now):
    """Housekeeping between batches; transient read failures are only postponed."""
    maybe_report(store, cfg, notifier, now)
    if now - store.get("last_auto_resend", 0) < cfg.poll_seconds:
        return
    try:
        result = auto_resend(store, api, cfg, now)
    except TransportError:
        return
    except APIError as error:
        if error.code not in TRANSIENT:
            raise
        return
    with store.conn:
        store.set("last_auto_resend", now)
    if result["requeued"]:
        print(f"Requeued {result['requeued']} dropped scrobbles.", flush=True)
    review = needs_review(store)
    reported = set(store.get("review_reported") or [])
    fresh = [r for r in review if r["id"] not in reported]
    if fresh:
        notifier.send(
            "⚠️ Last.fm не сохраняет эти скробблы даже после повторов; импорт идёт дальше:\n"
            + "\n".join(f"• {r['artist']} — {r['track']}" for r in fresh)
        )
        with store.conn:
            store.set("review_reported", sorted(reported | {r["id"] for r in fresh}))


def serve(store, api, cfg, args, notifier, run, sleep=time.sleep, clock=time.time):
    """Run continuously; halt on anything that needs a human, idle once finished."""
    notifier.send(
        "▶️ Импорт Last.fm запущен.\n"
        + summary(store, cfg, int(clock())).split("\n", 1)[1]
    )
    while True:
        now = int(clock())
        if store.get("halted"):
            maybe_report(store, cfg, notifier, now)
            sleep(60)
            continue
        if store.get("finished"):
            maybe_report(store, cfg, notifier, now)
            sleep(3600)
            continue
        try:
            run(
                store,
                api,
                cfg,
                args,
                tick=lambda now: tick(store, api, cfg, notifier, now),
            )
        except TransportError:
            with store.conn:
                backoff(store, now)
            sleep(60)
            continue
        except APIError as error:
            if error.code in TRANSIENT:
                with store.conn:
                    backoff(store, now, error.retry_after)
                sleep(60)
                continue
            halt(store, notifier, f"Last.fm API error {error.code}", now)
            continue
        except ValueError as error:
            halt(store, notifier, str(error), now)
            continue
        # run() returns only when nothing is left to send.
        if store.rows("SELECT 1 FROM events WHERE status IN ('pending','uncertain')"):
            halt(
                store,
                notifier,
                "Очередь пуста для автоматического режима, но остались неясные или "
                "заблокированные записи",
                now,
            )
            continue
        with store.conn:
            store.set("finished", now)
        notifier.send("✅ Импорт Last.fm завершён: всё отправлено и подтверждено.")
