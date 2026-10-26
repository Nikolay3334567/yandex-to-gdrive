"""Профили: несколько пар «аккаунт Яндекса → аккаунт Google», у каждой своя карта файлов и свой прогресс.

- «Основной» профиль — сама папка data, как в прошлых версиях: всё начатое продолжается без изменений.
  Остальные — data/profiles/<id>/ со своими settings.json, state.sqlite3 и журналом.
- Аккаунты Яндекса и Google — в общей библиотеке data/accounts.json (core.AccountStore). Профиль на них
  только ссылается: один аккаунт можно выбрать в нескольких профилях, новые куки сразу видны всем.
- Переносит одновременно только один профиль: у профилей общий интернет и общий лимит запросов к Яндексу.
  «Старт» другого профиля в это время ставит его в ожидание — он начнётся сам, когда первый закончит.
- Очередь: отмеченные профили переносятся друг за другом. Профиль, у которого ещё нет списка файлов,
  сначала получает список.
- Расписание: «Повторять отложенные раз в день / неделю / месяц» — та же кнопка «Повторить», только сама.
- Импорт из прошлой версии: карта файлов (state.sqlite3) и аккаунты из её settings.json — без нового входа
  и без нового получения списка.
"""
import json
import os
import shutil
import sqlite3
import threading
import time

from .core import DEFAULTS, ROUTE, SHARED_KEYS, AccountStore, _migrate, _upgrade_v3
from .engine import Buffer, Engine
from .http_util import ApiError

MAIN = "main"
TICK = float(os.environ.get("YA2GD_PROFILES_TICK", "3"))
NOT_COPIED = set(ROUTE) | set(SHARED_KEYS) | {"yandex_account", "google_account", "settings_v"}
AUTO_NAMES = {"day": "раз в день", "week": "раз в неделю", "month": "раз в месяц"}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def find_source(path):
    """Где карта файлов прошлой версии: файл .sqlite3, папка data или папка с YandexToGoogle.exe.
    (путь к базе, путь к settings.json или None)"""
    p = os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))
    if os.path.isfile(p) and p.lower().endswith(".exe"):
        p = os.path.dirname(p)
    if os.path.isfile(p):
        st = os.path.join(os.path.dirname(p), "settings.json")
        return p, st if os.path.isfile(st) else None
    for d in (p, os.path.join(p, "data")):
        db = os.path.join(d, "state.sqlite3")
        if os.path.isfile(db):
            st = os.path.join(d, "settings.json")
            return db, st if os.path.isfile(st) else None
    raise ApiError("Здесь нет данных программы. Укажите папку, где лежал YandexToGoogle.exe (рядом с ним — папка "
                   "data), саму папку data или файл карты (.sqlite3).")


def read_source_settings(path):
    """settings.json прошлой версии — только чтение, с теми же переходами между версиями настроек."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return {}
    _migrate(stored)
    _upgrade_v3(stored)
    return stored


class Manager:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.accounts = AccountStore(os.path.join(root, "accounts.json"))
        self.meta_path = os.path.join(root, "profiles.json")
        self.meta = {"seq": 1, "profiles": [{"id": MAIN, "name": "Основной", "queue": True}],
                     "queue": {"active": False, "pending": [], "current": None, "done": []}}
        if os.path.exists(self.meta_path):
            with open(self.meta_path, encoding="utf-8") as f:
                stored = json.load(f)
            self.meta.update({k: v for k, v in stored.items() if k in self.meta})
            if not any(p["id"] == MAIN for p in self.meta["profiles"]):
                self.meta["profiles"].insert(0, {"id": MAIN, "name": "Основной", "queue": True})
        self.main = Engine(root, accounts=self.accounts, profile_id=MAIN)
        self.engines = {MAIN: self.main}
        for p in self.meta["profiles"]:
            if p["id"] != MAIN:
                self._open(p)
        self.waiting = []                 # «Старт», пока переносил другой профиль: начнутся по очереди
        self._launched = set()            # профили, запущенные в этом запуске программы
        self._stop = threading.Event()
        self.runner = None

    # ------------------------------------------------------------------ профили
    def _save(self):
        _write_json(self.meta_path, self.meta)

    def _dir(self, pid):
        return self.root if pid == MAIN else os.path.join(self.root, "profiles", pid)

    def _open(self, p):
        e = Engine(self._dir(p["id"]), accounts=self.accounts, shared=self.main.settings, profile_id=p["id"],
                   prefix=f"[{p['name']}] ")
        self.engines[p["id"]] = e
        return e

    def get(self, pid=None):
        return self.engines.get(pid or MAIN) or self.main

    def has(self, pid):
        return pid in self.engines

    def profile(self, pid):
        return next((p for p in self.meta["profiles"] if p["id"] == pid), None)

    def name(self, pid):
        p = self.profile(pid)
        return p["name"] if p else pid

    def create(self, name="", yandex_account="", google_account="", copy_from=MAIN):
        """Новый профиль. Настройки (папки на Drive, буфер, потоки) — как у профиля copy_from,
        аккаунты — выбранные из библиотеки (или пусто: тогда их задают в блоках 1–3)."""
        with self.lock:
            self.meta["seq"] = int(self.meta.get("seq") or 1) + 1
            pid = f"p{self.meta['seq']}"
            d = self._dir(pid)
            os.makedirs(d, exist_ok=True)
            src = self.get(copy_from).settings
            vals = {k: v for k, v in src.values.items() if k in DEFAULTS and k not in NOT_COPIED}
            vals["yandex_account"] = yandex_account if self.accounts.exists("yandex", yandex_account) else ""
            vals["google_account"] = google_account if self.accounts.exists("google", google_account) else ""
            vals["settings_v"] = DEFAULTS["settings_v"]
            _write_json(os.path.join(d, "settings.json"), vals)
            n = len(self.meta["profiles"]) + 1
            p = {"id": pid, "name": (name or "").strip()[:60] or f"Профиль {n}", "queue": True, "created": time.time()}
            self.meta["profiles"].append(p)
            self._save()
            self._open(p)
        self.main.log(f"Создан профиль «{p['name']}»")
        return pid

    def rename(self, pid, name):
        with self.lock:
            p = self.profile(pid)
            if p and (name or "").strip():
                p["name"] = name.strip()[:60]
                self.engines[pid].log.prefix = "" if pid == MAIN else f"[{p['name']}] "
                self._save()

    def move(self, pid, delta):
        with self.lock:
            ps = self.meta["profiles"]
            i = next((k for k, p in enumerate(ps) if p["id"] == pid), None)
            j = None if i is None else i + int(delta)
            if i is not None and 0 <= j < len(ps):
                ps[i], ps[j] = ps[j], ps[i]
                self._save()

    def set_queue(self, pid, on):
        with self.lock:
            p = self.profile(pid)
            if p:
                p["queue"] = bool(on)
                self._save()

    def delete(self, pid):
        """Удалить профиль: его карту файлов, прогресс и журнал на этом компьютере. Файлы на Яндексе и Drive
        и аккаунты в библиотеке не трогаются."""
        if pid == MAIN:
            raise ApiError("Основной профиль удалить нельзя")
        with self.lock:
            e = self.engines.get(pid)
            if e is None:
                return
            if e.busy() or e.scanning():
                raise ApiError("Сначала остановите перенос, получение списка или замер этого профиля")
            name = self.name(pid)
            self.meta["profiles"] = [p for p in self.meta["profiles"] if p["id"] != pid]
            q = self.meta["queue"]
            q["pending"] = [x for x in q.get("pending", []) if x != pid]
            if q.get("current") == pid:
                q["current"] = None
            self.waiting = [x for x in self.waiting if x != pid]
            self._save()
            self.engines.pop(pid)
            bdir = e.settings["buffer_dir"]
            e.db.close()
            e.log.close()
            if bdir:
                shutil.rmtree(Buffer(bdir, e.buffer_sub).dir, ignore_errors=True)
            shutil.rmtree(self._dir(pid), ignore_errors=True)
        self.main.log(f"Профиль «{name}» удалён (файлы на Яндекс Диске и Google Drive не тронуты)")

    def accounts_in_use(self, kind, aid):
        return [self.name(pid) for pid, e in self.engines.items() if e.settings[kind + "_account"] == aid]

    def delete_account(self, kind, aid):
        used = self.accounts_in_use(kind, aid)
        if used:
            raise ApiError("Аккаунт выбран в профилях: " + ", ".join(f"«{x}»" for x in used) +
                           ". Сначала выберите в них другой аккаунт.")
        self.accounts.delete(kind, aid)

    # ------------------------------------------------------------------ один перенос за раз, очередь
    def running(self):
        for pid, e in self.engines.items():
            if e.transferring():
                return pid
        return None

    def start(self, pid):
        """«Старт» профиля. Если сейчас переносит другой профиль — этот встанет в ожидание и начнётся сам."""
        with self.lock:
            other = self.running()
            if other and other != pid:
                if pid not in self.waiting:
                    self.waiting.append(pid)
                self.get(pid).log(f"Сейчас переносит профиль «{self.name(other)}» — этот начнётся сразу после него")
                return {"ok": False, "queued": True, "after": self.name(other)}
            ok = self._launch(pid)
            return {"ok": ok, "queued": False}

    def _launch(self, pid, scan_if_needed=False):
        e = self.get(pid)
        if scan_if_needed and e.db.get("scan.state") != "done" and not e.scanning():
            e.log("Списка файлов ещё нет — получаю его, перенос пойдёт следом")
            e.start_scan()
        self._launched.add(pid)
        return e.start_transfer(patient=e.thorough_active())

    def queue_start(self):
        """«Перенести отмеченные профили по очереди». Если очередь была на паузе — продолжает её."""
        with self.lock:
            q = self.meta["queue"]
            if q.get("current") and not q.get("active"):
                q["active"] = True
                self._launched.discard(q["current"])        # продолжить остановленный профиль
            else:
                order = [p["id"] for p in self.meta["profiles"] if p.get("queue", True)]
                if not order:
                    raise ApiError("Отметьте хотя бы один профиль для очереди")
                self.meta["queue"] = q = {"active": True, "pending": order, "current": None, "done": [],
                                          "started": time.time()}
            self._save()
            names = ", ".join(f"«{self.name(x)}»" for x in ([q["current"]] if q.get("current") else []) + q["pending"])
        self.main.log(f"Очередь профилей: {names}")
        self._tick()

    def queue_stop(self):
        """Остановить очередь: текущий перенос — на паузу, оставшиеся профили не запускаются."""
        with self.lock:
            q = self.meta["queue"]
            cur = q.get("current")
            q.update(active=False, pending=[], current=None)
            self._save()
        if cur:
            self.get(cur).pause_transfer()
        self.main.log("Очередь профилей остановлена")

    def _tick(self):
        """Раз в несколько секунд: следующий профиль очереди, ожидающий «Старт», повтор отложенных по расписанию."""
        with self.lock:
            if self.running():
                return
            q = self.meta["queue"]
            if q.get("active"):
                cur = q.get("current")
                if cur and cur in self.engines and cur not in self._launched:
                    self._try_launch(cur)                      # продолжить после перезапуска программы
                    return
                if cur:
                    e = self.engines.get(cur)
                    if e is not None and e.transfer_state == "paused" and e.db.get("transfer.wanted") == "0":
                        q["active"] = False                     # человек сам нажал «Пауза» — очередь ждёт его
                        self._save()
                        self.main.log(f"Очередь профилей на паузе: остановлен перенос «{self.name(cur)}». "
                                      f"Продолжить — «Перенести по очереди»")
                        return
                    if e is not None and e.transfer_state == "paused":
                        e.log(f"Перенос остановился: {e.pause_reason or 'ошибка'} — очередь идёт дальше", "warn")
                    q.setdefault("done", []).append(cur)
                    q["current"] = None
                while q.get("pending"):
                    pid = q["pending"].pop(0)
                    if pid not in self.engines:
                        continue
                    q["current"] = pid
                    self._save()
                    self.main.log(f"Очередь профилей: начинаю «{self.name(pid)}»")
                    if self._try_launch(pid):
                        return
                    q.setdefault("done", []).append(pid)
                    q["current"] = None
                q["active"] = False
                self._save()
                self.main.log("Очередь профилей пройдена: " +
                              ", ".join(f"«{self.name(x)}»" for x in q.get("done", [])))
                return
            while self.waiting:
                pid = self.waiting.pop(0)
                if pid in self.engines and self._try_launch(pid):
                    return
            for pid, e in self.engines.items():
                if e.thorough_step():            # следующий круг тщательного прохода
                    self._try_launch(pid)
                    return
            for pid, e in self.engines.items():
                if e.auto_retry_due():
                    when = AUTO_NAMES.get(e.settings['deferred_auto'], '')
                    if e.settings["deferred_auto_thorough"]:
                        e.log(f"По расписанию ({when}) — тщательный проход по отложенным")
                        e.thorough_start(auto=True)
                    else:
                        n = e.retry_errors(auto=True)
                        e.log(f"Повтор отложенных по расписанию ({when}): {n} файлов снова в очереди")
                    self._try_launch(pid)
                    return

    def _try_launch(self, pid):
        e = self.get(pid)
        try:
            return self._launch(pid, scan_if_needed=True)
        except Exception as ex:  # noqa — нет токена, входа в Google и т.п.: профиль пропускается
            e.log(f"Не удалось начать перенос: {ex}", "error")
            return False

    def run(self):
        """Фоновый поток: очередь, ожидание, расписание."""
        if self.runner and self.runner.is_alive():
            return

        def loop():
            while not self._stop.wait(TICK):
                try:
                    self._tick()
                except Exception as ex:  # noqa
                    self.main.log(f"Профили: {ex!r}", "error")
        self.runner = threading.Thread(target=loop, daemon=True, name="profiles")
        self.runner.start()

    def close(self):
        self._stop.set()

    def autostart(self):
        """При запуске программы (если включено «сам продолжать»): очередь — с того же профиля, иначе
        профиль, который переносил, когда программу закрыли; остальные начатые — следом."""
        q = self.meta["queue"]
        if not self.main.settings["autostart"]:
            if q.get("active"):
                q["active"] = False                  # очередь ждёт «Перенести по очереди»
                self._save()
            return
        for pid, e in self.engines.items():
            if e.db.get("scan.state") in ("interrupted", "stopped") and e.settings["yandex_token"]:
                e.log("Автозапуск: продолжаю прерванное получение списка")
                e.start_scan()
        if q.get("active"):
            self.main.log("Автозапуск: продолжаю очередь профилей")
            return
        wanted = [pid for pid, e in self.engines.items() if e.db.get("transfer.wanted") == "1"]
        for pid in wanted:
            self.get(pid).log("Автозапуск: продолжаю перенос")
            try:
                self.start(pid)
            except Exception as ex:  # noqa
                self.get(pid).log(f"Автозапуск не удался: {ex}", "error")

    # ------------------------------------------------------------------ для окна
    def listing(self):
        out = []
        run = self.running()
        q = self.meta["queue"]
        for p in self.meta["profiles"]:
            e = self.engines.get(p["id"])
            if e is None:
                continue
            bs = e._stats()["by_status"]
            total = sum(v["count"] for v in bs.values())
            tb = sum(v["bytes"] for v in bs.values())
            done = bs.get("done", {}).get("count", 0) + bs.get("skipped", {}).get("count", 0)
            db_ = bs.get("done", {}).get("bytes", 0) + bs.get("skipped", {}).get("bytes", 0)
            s = e.settings
            state = e.transfer_state
            if e.scanning():
                state = "scanning" if state not in ("running", "pausing") else state
            out.append({"id": p["id"], "name": p["name"], "queue": p.get("queue", True), "state": state,
                        "running": p["id"] == run, "waiting": p["id"] in self.waiting,
                        "files": total, "bytes": tb, "done": done, "done_bytes": db_,
                        "deferred": bs.get("deferred", {}).get("count", 0),
                        "yandex": self.accounts.label("yandex", s["yandex_account"]),
                        "google": self.accounts.label("google", s["google_account"]) or s["google_email"],
                        "deferred_auto": s["deferred_auto"], "scan": e.db.get("scan.state") or "never"})
        return {"profiles": out,
                "queue": {"active": bool(q.get("active")), "current": q.get("current"),
                          "pending": list(q.get("pending") or []), "done": list(q.get("done") or [])},
                "accounts": {k: [dict(a, used=self.accounts_in_use(k, a["id"])) for a in self.accounts.listing(k)]
                             for k in ("yandex", "google")}}

    # ------------------------------------------------------------------ перенос из прошлой версии
    def inspect_import(self, path):
        """Что лежит в папке прошлой версии (или в файле карты): сколько файлов и в каком состоянии, какие аккаунты."""
        db, st_path = find_source(path)
        for e in self.engines.values():
            if os.path.normcase(os.path.realpath(db)) == os.path.normcase(os.path.realpath(e.db.path)):
                raise ApiError("Это карта файлов, с которой программа уже работает (профиль "
                               f"«{self.name(e.profile_id)}») — импортировать её не нужно")
        try:
            c = sqlite3.connect(db, timeout=30)
            try:
                rows = c.execute("SELECT status, COUNT(*), IFNULL(SUM(size),0) FROM files GROUP BY status").fetchall()
                kv = dict(c.execute("SELECT key, value FROM kv").fetchall())
            finally:
                c.close()
        except sqlite3.Error as ex:
            raise ApiError(f"Не удалось прочитать карту файлов: {ex}")
        by = {r[0]: {"count": r[1], "bytes": r[2]} for r in rows}
        st = read_source_settings(st_path)
        return {"db": db, "settings": st_path, "by_status": by,
                "files": sum(v["count"] for v in by.values()), "bytes": sum(v["bytes"] for v in by.values()),
                "scan": kv.get("scan.state") or "never", "google_email": st.get("google_email") or "",
                "yandex_token": bool(st.get("yandex_token")), "yandex_cookies": bool(st.get("yandex_cookies")),
                "google_signed_in": bool(st.get("google_refresh_token")),
                "photos_root": st.get("photos_root") or "", "files_root": st.get("files_root") or ""}

    def import_old(self, path, pid=None, target="auto", name="", with_accounts=True):
        """Перенос из прошлой версии: карта файлов + (по желанию) аккаунты и раскладка папок на Drive.
        target: auto — в текущий профиль, если в нём ещё нет файлов, иначе в новый; new — всегда в новый."""
        info = self.inspect_import(path)
        st = read_source_settings(info["settings"])
        accs = {}
        if with_accounts and st:
            for kind in ("yandex", "google"):
                creds = {k: st.get(k) or "" for k, r in ROUTE.items() if r == kind}
                if any(v.strip() for v in creds.values()):
                    accs[kind] = self.accounts.adopt(kind, creds)
        cur = self.get(pid)
        empty = not cur.db.q1("SELECT 1 FROM files LIMIT 1")
        if target == "new" or (target == "auto" and not empty):
            label = name or (f"Импорт ({info['google_email']})" if info["google_email"] else "Импорт из прошлой версии")
            pid = self.create(label, accs.get("yandex", ""), accs.get("google", ""), copy_from=cur.profile_id or MAIN)
            e, fresh = self.get(pid), True
        else:
            e, fresh = cur, empty
        upd = {}
        if fresh and st:                     # та же раскладка папок на Drive, что у прошлой версии
            upd = {k: v for k, v in st.items() if k in DEFAULTS and k not in NOT_COPIED}
        for kind, aid in accs.items():
            if not e.settings[kind + "_account"]:
                upd[kind + "_account"] = aid
        if upd:
            e.settings.update(**upd)
        same_google = bool(info["google_email"]) and info["google_email"] == e.settings["google_email"]
        res = e.import_map(info["db"], with_folders=same_google)
        res.update(profile=e.profile_id, profile_name=self.name(e.profile_id), same_google=same_google,
                   source_google=info["google_email"], target_google=e.settings["google_email"],
                   accounts=sorted(accs))
        return res
