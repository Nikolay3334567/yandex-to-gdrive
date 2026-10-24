"""Локальный веб-интерфейс (только 127.0.0.1) и JSON API."""
import html
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, winsys
from .http_util import ApiError

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Лицензии и README: в exe они вложены в папку legal, в исходниках лежат в корне репозитория
LEGAL_DIR = os.path.join(sys._MEIPASS, "legal") if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") \
    else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGAL = {"license": "LICENSE", "license-ru": "LICENSE.ru.txt", "third-party": "THIRD_PARTY_LICENSES.txt",
         "readme": "README.txt"}


class Handler(BaseHTTPRequestHandler):
    manager = None                       # profiles.Manager: все профили; запрос работает с выбранным в окне
    server_version = "ya2gd"

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _pid(self, qs):
        """Профиль, с которым работает окно: заголовок X-Ya2gd-Profile или ?profile= (для ссылок)."""
        pid = self.headers.get("X-Ya2gd-Profile") or qs.get("profile") or ""
        return pid if self.manager.has(pid) else "main"

    def _base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        m = self.manager
        pid = self._pid(qs)
        e = m.get(pid)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                st = e.status()
                st["version"] = __version__
                st["data_dir"] = e.dir
                st["profile"] = {"id": pid, "name": m.name(pid)}
                st["deferred"] = e.deferred_info()
                pl = m.listing()
                st["profiles"] = [{k: p[k] for k in ("id", "name", "state", "running", "waiting")}
                                  for p in pl["profiles"]]
                st["queue"] = pl["queue"]
                return self._send(200, st)
            if u.path == "/api/profiles":
                return self._send(200, m.listing())
            if u.path == "/api/export/map":
                tmp = os.path.join(m.root, f"export-{os.getpid()}-{time.time_ns()}.sqlite3")
                try:
                    e.export_map(tmp)
                    with open(tmp, "rb") as f:
                        data = f.read()
                finally:
                    for x in (tmp, tmp + "-journal"):
                        try:
                            os.remove(x)
                        except OSError:
                            pass
                name = f"ya2gd-map-{pid}-{time.strftime('%Y%m%d-%H%M')}.sqlite3"
                return self._send(200, data, "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path == "/api/drives":
                return self._send(200, winsys.list_drives())
            if u.path == "/api/settings":
                return self._send(200, e.settings.public())
            if u.path == "/api/log":
                return self._send(200, e.log.since(int(qs.get("after", 0))))
            if u.path == "/api/files":
                total, rows = e.db.list_files(qs.get("status") or None, qs.get("q") or None,
                                              int(qs.get("offset", 0)), min(500, int(qs.get("limit", 100))))
                return self._send(200, {"total": total, "items": rows})
            if u.path == "/api/report.csv":
                only = qs.get("status") or None
                name = f"ya2gd-{only}.csv" if only else "ya2gd-report.csv"
                return self._send(200, e.report_csv(only), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path.startswith("/legal/") and u.path[7:] in LEGAL:
                try:
                    with open(os.path.join(LEGAL_DIR, LEGAL[u.path[7:]]), encoding="utf-8-sig") as f:
                        return self._send(200, f.read(), "text/plain; charset=utf-8")
                except OSError:
                    return self._send(404, "Файл не найден. Тексты лицензий есть в репозитории: "
                                           "https://github.com/Nikolay3334567/yandex-to-gdrive",
                                      "text/plain; charset=utf-8")
            if u.path == "/oauth/google/start":
                auth = e.google_auth()
                if not auth.client_id:
                    return self._send(400, "Сначала сохраните Client ID и Client secret", "text/plain; charset=utf-8")
                return self._redirect(auth.auth_url(self._base() + "/oauth/google/callback", e.google_state))
            if u.path == "/oauth/google/callback":
                # профиль — по state: его выдал тот профиль, из окна которого нажали «Войти в Google»
                e = next((x for x in m.engines.values() if x.google_state == qs.get("state")), e)
                return self._google_callback(e, qs)
            return self._send(404, {"error": "not found"})
        except Exception as ex:  # noqa
            return self._error(ex)

    def _google_callback(self, e, qs):
        if qs.get("error"):
            return self._send(400, _page(f"Google вернул ошибку: {qs['error']}"), "text/html; charset=utf-8")
        if qs.get("state") != e.google_state:
            return self._send(400, _page("Неверный state — начните вход заново из программы"),
                              "text/html; charset=utf-8")
        auth = e.google_auth()
        rt = auth.exchange_code(qs.get("code", ""), self._base() + "/oauth/google/callback")
        e.settings.update(google_refresh_token=rt)
        try:
            info = e.test_google()
            e.settings.update(google_email=info.get("email") or "")
            self.manager.accounts.label_if_default("google", e.settings["google_account"], info.get("email"))
            msg = f"Google подключён: {info.get('email')}. Запись проверена. Окно можно закрыть."
        except Exception as ex:  # noqa
            msg = f"Токен получен, но проверка не прошла: {ex}"
        e.log(msg)
        return self._send(200, _page(msg, back=True), "text/html; charset=utf-8")

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        # Защита от запросов с чужих сайтов: браузер не даст им выставить этот заголовок без CORS.
        if self.headers.get("X-Ya2gd") != "1":
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        p = u.path
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        m = self.manager
        pid = self._pid(qs)
        e = m.get(pid)
        try:
            if p == "/api/import/upload":                 # файл карты прямо из окна (тело — сам файл)
                n = int(self.headers.get("Content-Length") or 0)
                if not n or n > 2 * 1024 ** 3:
                    raise ApiError("Пустой или слишком большой файл")
                d = os.path.join(m.root, "imports")
                os.makedirs(d, exist_ok=True)
                dst = os.path.join(d, f"upload-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
                left = n
                with open(dst, "wb") as f:
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                return self._send(200, m.inspect_import(dst))
            b = self._body()
            if p == "/api/settings":
                clean = {k: v for k, v in b.items() if not (k.endswith("_set"))}
                # маскированные секреты не перезаписываем
                for k in ("yandex_token", "yandex_cookies", "google_client_secret", "google_refresh_token"):
                    if k in clean and (not clean[k] or "…" in str(clean[k])):
                        clean.pop(k)
                if "google_client_id" in clean and clean["google_client_id"] != e.settings["google_client_id"]:
                    clean.setdefault("google_refresh_token", "")
                if clean.get("deferred_auto") and clean["deferred_auto"] != e.settings["deferred_auto"]:
                    e.db.set("deferred.auto_since", time.time())    # первый повтор — через выбранный срок
                e.settings.update(**clean)
                e.sync_sections()
                if "buffer_dir" in clean and e.transferring():
                    e.apply_buffer()            # буфер — сразу, без «Пауза → Старт»
                if "win_autorun" in clean:
                    winsys.set_autorun(e.settings["win_autorun"])
                return self._send(200, e.settings.public())
            if p == "/api/pick_folder":
                args = [b.get("initial") or ""] + ([b["title"]] if b.get("title") else [])
                return self._send(200, {"path": winsys.pick_folder(*args)})
            if p == "/api/buffer/check":
                return self._send(200, _check_buffer(b.get("path") or ""))
            if p == "/api/actions/previews":
                e.rescue_previews()
                return self._send(200, {"ok": True})
            if p == "/api/quit":
                threading.Thread(target=_quit, args=(m,), daemon=True).start()
                return self._send(200, {"ok": True})
            if p == "/api/yandex/test":
                r = e.test_yandex()
                m.accounts.label_if_default("yandex", e.settings["yandex_account"], r.get("user"))
                return self._send(200, r)
            if p == "/api/yandex/web_test":
                return self._send(200, e.test_yandex_web())
            if p == "/api/google/test":
                r = e.test_google()
                m.accounts.label_if_default("google", e.settings["google_account"], r.get("email"))
                return self._send(200, r)
            if p == "/api/google/logout":
                e.settings.update(google_refresh_token="", google_email="")
                return self._send(200, {"ok": True})
            if p == "/api/scan/start":
                ok = e.start_scan(bool(b.get("fresh")))
                return self._send(200, {"ok": ok})
            if p == "/api/scan/stop":
                e.stop_scanning()
                return self._send(200, {"ok": True})
            if p == "/api/transfer/start":
                return self._send(200, m.start(pid))
            if p == "/api/transfer/pause":
                e.pause_transfer()
                return self._send(200, {"ok": True})
            if p == "/api/actions/retry_errors":
                return self._send(200, {"ok": True, "n": e.retry_errors()})
            if p == "/api/actions/requeue_all":
                return self._send(200, {"ok": True, "n": e.requeue_all()})
            if p == "/api/actions/verify_drive":
                e.verify_drive()
                return self._send(200, {"ok": True})
            if p == "/api/actions/reset_db":
                e.reset_db()
                return self._send(200, {"ok": True})
            if p == "/api/probe/sample":
                e.speed_sample()
                return self._send(200, {"ok": True})
            if p == "/api/probe/mark":
                e.mark_all(bool(b.get("fresh")))
                return self._send(200, {"ok": True})
            if p == "/api/probe/defer":
                return self._send(200, {"ok": True, "n": e.defer_marked()})
            if p == "/api/task/stop":
                e.stop_task()
                return self._send(200, {"ok": True})
            if p == "/api/deferred/retry":            # «Повторить отложенные»: в очередь и сразу перенос
                n = e.retry_errors()
                r = m.start(pid) if n and not e.transferring() else {"ok": e.transferring(), "queued": False}
                return self._send(200, dict(r, n=n))
            # ---------- профили и аккаунты
            if p == "/api/profiles/create":
                return self._send(200, {"id": m.create(b.get("name", ""), b.get("yandex_account", ""),
                                                       b.get("google_account", ""), b.get("copy_from") or pid)})
            if p == "/api/profiles/rename":
                m.rename(b["id"], b.get("name", ""))
                return self._send(200, {"ok": True})
            if p == "/api/profiles/delete":
                m.delete(b["id"])
                return self._send(200, {"ok": True})
            if p == "/api/profiles/move":
                m.move(b["id"], b.get("delta", 0))
                return self._send(200, {"ok": True})
            if p == "/api/profiles/queue_flag":
                m.set_queue(b["id"], b.get("on"))
                return self._send(200, {"ok": True})
            if p == "/api/queue/start":
                m.queue_start()
                return self._send(200, {"ok": True})
            if p == "/api/queue/stop":
                m.queue_stop()
                return self._send(200, {"ok": True})
            if p == "/api/profile/accounts":              # выбрать аккаунт из библиотеки для этого профиля
                upd = {k: b[k] for k in ("yandex_account", "google_account")
                       if k in b and (not b[k] or m.accounts.exists(k[:-8], b[k]))}
                e.settings.update(**upd)
                return self._send(200, e.settings.public())
            if p == "/api/accounts/create":               # новый пустой аккаунт — сразу в этот профиль
                kind = b.get("kind")
                if kind not in ("yandex", "google"):
                    raise ApiError("Неизвестный вид аккаунта")
                creds = {}
                if kind == "google":                       # тот же проект Google Cloud — другой вход
                    src = b.get("client_from") or ""
                    if m.accounts.exists("google", src):
                        creds = {k: m.accounts.get("google", src, k) for k in ("google_client_id", "google_client_secret")}
                    elif b.get("copy_client", True):
                        creds = {k: e.settings[k] for k in ("google_client_id", "google_client_secret")}
                aid = m.accounts.create(kind, label=b.get("label", ""), **creds)
                e.settings.update(**{kind + "_account": aid})
                return self._send(200, {"id": aid})
            if p == "/api/accounts/rename":
                m.accounts.rename(b["kind"], b["id"], b.get("label", ""))
                return self._send(200, {"ok": True})
            if p == "/api/accounts/delete":
                m.delete_account(b["kind"], b["id"])
                return self._send(200, {"ok": True})
            # ---------- перенос из прошлой версии
            if p == "/api/import/inspect":
                return self._send(200, m.inspect_import(b.get("path", "")))
            if p == "/api/import/apply":
                return self._send(200, m.import_old(b.get("path", ""), pid, b.get("target", "auto"),
                                                    b.get("name", ""), b.get("with_accounts", True)))
            if p == "/api/files/action":
                e.file_action(int(b["id"]), b["action"])
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as ex:  # noqa
            return self._error(ex)

    def _error(self, ex):
        if not isinstance(ex, ApiError):
            self.manager.main.log(traceback.format_exc(), "debug")
        return self._send(400 if isinstance(ex, ApiError) else 500, {"error": str(ex)})


def _page(msg, back=False):
    link = '<p><a href="/">← Вернуться в программу</a></p>' if back else ""
    return (f'<!doctype html><meta charset="utf-8"><title>ya2gd</title>'
            f'<body style="font-family:sans-serif;padding:40px;background:#f4f5f7">'
            f'<h3>{html.escape(str(msg))}</h3>{link}</body>')


def _check_buffer(path):
    """Проверка папки буфера: можно ли писать, сколько места."""
    path = (path or "").strip().strip('"')
    if not path:
        return {"ok": True, "msg": "Буфер на диске выключен: маленькие файлы ждут в памяти, большие идут напрямую"}
    if not os.path.isabs(path):
        raise ApiError("Укажите полный путь, например D:\\YandexBuffer")
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".ya2gd-probe")
        with open(probe, "wb") as f:
            f.write(b"x" * 4096)
        os.remove(probe)
        du = shutil.disk_usage(path)
    except OSError as ex:
        raise ApiError(f"В папку «{path}» нельзя писать: {ex}")
    return {"ok": True, "free": du.free, "total": du.total,
            "msg": f"Папка доступна, свободно {du.free / 2**30:.1f} ГБ из {du.total / 2**30:.1f} ГБ"}


def _quit(manager):
    manager.main.log("Выход по команде из окна программы: останавливаю перенос…")
    manager.close()
    for engine in manager.engines.values():
        engine.stop_scanning()
        engine.stop_task()
        engine.pause_transfer("выход из программы", by_user=False)
    t0 = time.time()
    while any(x.transferring() for x in manager.engines.values()) and time.time() - t0 < 60:
        time.sleep(0.5)
    manager.main.log("Программа закрыта. Прогресс сохранён — при следующем запуске всё продолжится.")
    winsys.allow_sleep()
    time.sleep(0.5)
    os._exit(0)


class _Server(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второй программе занять тот же порт — запрещаем
    allow_reuse_address = not winsys.WIN
    daemon_threads = True


def serve(manager, port):
    Handler.manager = manager
    return _Server(("127.0.0.1", port), Handler)
