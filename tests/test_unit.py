"""Быстрые проверки отдельных частей без сети. Запуск: python tests/test_unit.py"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # консоль Windows в CI — cp1252
except AttributeError:
    pass
sys.path.insert(0, ROOT)

from ya2gd import throttle  # noqa: E402
from ya2gd.core import Settings  # noqa: E402
from ya2gd.db import DB  # noqa: E402
from ya2gd.engine import MemQueue, drive_dir_for, is_media, name_date  # noqa: E402
from ya2gd.yaweb import from_mpfs, parse_cookies, to_mpfs  # noqa: E402


def test_gate():
    throttle.GROW_EVERY = 0.05
    g = throttle.AdaptiveGate(6, start=3)
    stop = threading.Event()
    assert g.state()["limit"] == 3
    for _ in range(3):
        assert g.acquire(stop)
    g.tick(has_backlog=True)            # рано: с последнего изменения прошло мало
    assert g.state()["limit"] == 3
    time.sleep(0.06)
    g.tick(has_backlog=False)           # работы нет — не растём
    assert g.state()["limit"] == 3
    g.tick(has_backlog=True)
    assert g.state()["limit"] == 4, g.state()
    g.throttled()
    assert g.state()["limit"] == 2
    for _ in range(3):
        g.overload()
    assert g.state()["limit"] == 1
    for _ in range(3):
        g.release()
    # при лимите 1 второй поток ждёт, пока первый не освободит место
    assert g.acquire(stop)
    got = []
    t = threading.Thread(target=lambda: got.append(g.acquire(stop)))
    t.start()
    time.sleep(0.2)
    assert not got
    g.release()
    t.join(3)
    assert got == [True]
    g.release()
    g.configure(8, adaptive=True)        # новый «Старт» — снова со стартового числа, а не с 1
    assert g.state()["limit"] == 3 and g.state()["max"] == 8
    g.configure(5, adaptive=False)
    assert g.state()["limit"] == 5
    assert not hasattr(g, "error")       # недоступные файлы больше не уменьшают число потоков
    print("OK автоподбор потоков: растёт по одному, вдвое меньше только на 429, минус один на 503; "
          "недоступные файлы потоки не отнимают")


def test_settings_migration():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "settings.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"drive_root": "Yandex Disk", "photounlim_folder": "Фото (безлимит)",
                       "photounlim_split": "year", "workers": 8, "yandex_token": "t"}, f)
        s = Settings(p)
        assert s.migrated
        assert s["files_root"] == "Yandex Disk" and s["photos_root"] == "Yandex Disk/Фото (безлимит)"
        assert s["photos_split"] == "year" and s["yandex_streams_max"] == 8 and s["yandex_token"] == "t"
        assert s["photos_from_disk"] == "none"     # «Фотокамера» у старых установок остаётся в «Файлах»
        assert s["max_attempts"] == 2 and s["auto_retry_hours"] == 0 and s["yandex_streams_max"] == 8
        s.update(photos_root=" Архив \\ Фото/ ", buffer_gb="25", yandex_streams_max=99)
        assert s["photos_root"] == "Архив/Фото" and s["buffer_gb"] == 25 and s["yandex_streams_max"] == 16
        s2 = Settings(p)
        assert not s2.migrated and s2["photos_root"] == "Архив/Фото"
        # версия 2: значения по умолчанию обновляются, а изменённые человеком — нет
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"files_root": "Ф", "photos_root": "Фото", "max_attempts": 6, "auto_retry_hours": 3,
                       "yandex_streams_max": 12, "google_streams": 3, "save_previews": True}, f)
        s3 = Settings(p)
        assert (s3["max_attempts"], s3["auto_retry_hours"], s3["yandex_streams_max"], s3["google_streams"]) == \
            (2, 0, 12, 4), s3.values
        assert json.load(open(p, encoding="utf-8"))["settings_v"] == 3
        print("OK настройки прежней версии переносятся, папки на Drive остаются прежними")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_layout():
    s = {"photos_root": "Фото", "files_root": "Файлы с Яндекс Диска", "photos_split": "month",
         "photos_from_disk": "camera"}
    row = {"path": "photounlim:/2019-05-04 10-00-00.JPG", "source": "photounlim", "name": "2019-05-04 10-00-00.JPG"}
    assert drive_dir_for(row, s) == "Фото/2019/2019-05"
    row = {"path": "photounlim:/x.jpg", "source": "photounlim", "name": "x.jpg", "taken": "2021-03-01T10:00:00+00:00"}
    assert drive_dir_for(row, s) == "Фото/2021/2021-03"
    row = {"path": "photounlim:/x.jpg", "source": "photounlim", "name": "x.jpg"}
    assert drive_dir_for(row, s) == "Фото"
    assert drive_dir_for(row, dict(s, photos_split="none")) == "Фото"
    row = {"path": "disk:/Документы/2020/отчёт.pdf", "source": "disk", "name": "отчёт.pdf"}
    assert drive_dir_for(row, s) == "Файлы с Яндекс Диска/Документы/2020"
    row = {"path": "disk:/корень.txt", "source": "disk", "name": "корень.txt"}
    assert drive_dir_for(row, s) == "Файлы с Яндекс Диска"
    # автозагрузка без безлимита (папка «Фотокамера») — тоже в «Фото» по дате; документы оттуда — в «Файлы»
    cam = {"path": "disk:/Фотокамера/2020-01-02 10-00-00.MOV", "source": "disk", "name": "2020-01-02 10-00-00.MOV"}
    assert drive_dir_for(cam, s) == "Фото/2020/2020-01"
    cam_sub = {"path": "disk:/Фотокамера/Screenshots/a.png", "source": "disk", "name": "a.png",
               "created": "2021-07-01T00:00:00+00:00"}
    assert drive_dir_for(cam_sub, s) == "Фото/2021/2021-07/Screenshots"
    doc = {"path": "disk:/Фотокамера/notes.txt", "source": "disk", "name": "notes.txt", "mime": "text/plain"}
    assert drive_dir_for(doc, s) == "Файлы с Яндекс Диска/Фотокамера"
    other = {"path": "disk:/Отпуск/IMG_20190504_1.jpg", "source": "disk", "name": "IMG_20190504_1.jpg"}
    assert drive_dir_for(other, s) == "Файлы с Яндекс Диска/Отпуск"
    assert drive_dir_for(other, dict(s, photos_from_disk="all")) == "Фото/2019/2019-05"
    assert drive_dir_for(cam, dict(s, photos_from_disk="none")) == "Файлы с Яндекс Диска/Фотокамера"
    assert name_date("IMG_20190504_101010.jpg") == "2019-05-04"
    assert is_media({"name": "VID.MOV"}) and not is_media({"name": "a.pdf", "mime": "application/pdf"})
    assert to_mpfs("disk:/A/b.txt") == "/disk/A/b.txt" and from_mpfs("/disk/A/b.txt") == "disk:/A/b.txt"
    assert to_mpfs("photounlim:/a.jpg") == "/photounlim/a.jpg" and from_mpfs("/photounlim/a.jpg") == "photounlim:/a.jpg"
    assert parse_cookies("Cookie: a=1; b=2") == {"a": "1", "b": "2"}
    print("OK раскладка: «Фото/год/год-месяц» и «Файлы с Яндекс Диска/<те же папки>»")


def test_routing_sql_matches_python():
    import sqlite3
    from ya2gd.core import DEFAULTS
    from ya2gd.engine import goes_to_photos, photos_sql
    rows = [("photounlim", "photounlim:/a.jpg", "a.jpg", None, None),
            ("disk", "disk:/Фотокамера/IMG_1.JPG", "IMG_1.JPG", None, "application/octet-stream"),
            ("disk", "disk:/Фотокамера/x.pdf", "x.pdf", None, "application/pdf"),
            ("disk", "disk:/Видео проекты/clip.MOV", "clip.MOV", "video", "video/quicktime"),
            ("disk", "disk:/Документы/a.docx", "a.docx", "document", None),
            ("disk", "disk:/Camera Uploads/b.heic", "b.heic", None, None),
            ("disk", "disk:/camera uploads/c.jpg", "c.jpg", None, None),
            ("disk", "disk:/Фотокамера.jpg", "Фотокамера.jpg", "image", None)]
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE files(source, path, name, media_type, mime)")
    c.executemany("INSERT INTO files VALUES(?,?,?,?,?)", rows)
    for mode in ("camera", "all", "none"):
        in_sql = [bool(x[0]) for x in c.execute(f"SELECT {photos_sql(mode)} FROM files")]
        in_py = [goes_to_photos(dict(zip(("source", "path", "name", "media_type", "mime"), r)),
                                dict(DEFAULTS, photos_from_disk=mode)) for r in rows]
        assert in_sql == in_py, (mode, in_sql, in_py)
    c.close()
    print("OK подсчёт прогресса по папкам совпадает с тем, куда реально кладутся файлы")


def test_disk_warnings():
    from ya2gd.engine import Engine
    d = tempfile.mkdtemp()
    try:
        e = Engine(d)
        gb = 2 ** 30
        e.db.set("disk.info", json.dumps({"total": 10 * gb, "used": 12 * gb}))
        w = e.disk_warnings()
        assert w and w[0][0] == "err" and "44 дня" in w[0][1], w
        soon = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 30 * 86400))
        e.db.set("disk.info", json.dumps({"total": 10 * gb, "used": 9.8 * gb, "will_be_overdrawn": True,
                                          "unlim_end": soon + "T00:00:00+00:00"}))
        w = e.disk_warnings()
        assert [lv for lv, _ in w] == ["warn", "err"] and soon in w[1][1], w
        e.db.set("disk.info", json.dumps({"total": 10 * gb, "used": 1 * gb, "will_be_overdrawn": True,
                                          "unlim_end": "2020-01-01"}))
        assert e.disk_warnings() == []          # дата уже прошла — не пугаем
        e.db.set("disk.info", json.dumps({"total": 10 * gb, "used": 1 * gb}))
        assert e.disk_warnings() == []
        e.db.close()
        print("OK предупреждения: Диск переполнен (44/90 дней), почти полон, конец бесплатного безлимита")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sections_and_speed():
    """Раздел хранится в базе: выбор следующего файла быстрый даже на сотне тысяч файлов,
    смена настройки пересчитывает раздел, а обновление с версии 2 не ломает начатую раскладку."""
    from ya2gd.engine import Engine
    d = tempfile.mkdtemp()
    try:
        e = Engine(d)
        items = []
        for i in range(100_000):
            kind = i % 3
            if kind == 0:
                items.append({"path": f"photounlim:/{i}.jpg", "source": "photounlim", "name": f"{i}.jpg", "size": 3})
            elif kind == 1:
                items.append({"path": f"disk:/Фотокамера/{i}.jpg", "source": "disk", "name": f"{i}.jpg", "size": 3})
            else:
                items.append({"path": f"disk:/Документы/{i}.pdf", "source": "disk", "name": f"{i}.pdf", "size": 3})
        e._found(items, 1)
        t = time.time()
        for _ in range(5):
            row = e.db.claim_download(order="photos_first")
            assert row["section"] == 1, row
        per_claim = (time.time() - t) / 5
        assert per_claim < 0.3, f"выбор файла слишком медленный: {per_claim:.2f} c"
        t = time.time()
        st = e.db.stats()
        assert time.time() - t < 1.5
        assert st["by_section"]["photos"]["count"] == 66_667 and st["by_section"]["files"]["count"] == 33_333, st
        e.settings.update(photos_from_disk="none")
        e.sync_sections()
        st = e.db.stats()
        assert st["by_section"]["photos"]["count"] == 33_334, st["by_section"]
        row = e.db.claim_download(order="files_first")
        assert row["section"] == 0
        e.db.close()
        print(f"OK раздел хранится в базе: выбор файла {per_claim * 1000:.0f} мс на 100 тыс. файлов; "
              f"смена настройки пересчитывает раскладку")

        # обновление с версии 2: «Фотокамера» уже частично перенесена в «Файлы» — раскладку не меняем
        d2 = tempfile.mkdtemp()
        with open(os.path.join(d2, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"files_root": "Файлы с Яндекс Диска", "photos_root": "Фото"}, f)
        from ya2gd.db import DB
        db = DB(os.path.join(d2, "state.sqlite3"))
        db.upsert_files([{"path": "disk:/Фотокамера/a.jpg", "source": "disk", "name": "a.jpg", "size": 1}], 1)
        db.x("UPDATE files SET status='done'")
        db.close()
        e2 = Engine(d2)
        assert e2.settings["photos_from_disk"] == "none", e2.settings["photos_from_disk"]
        e2.db.close()
        d3 = tempfile.mkdtemp()
        with open(os.path.join(d3, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"files_root": "Файлы с Яндекс Диска", "photos_root": "Фото"}, f)
        e3 = Engine(d3)
        assert e3.settings["photos_from_disk"] == "camera"
        e3.db.close()
        for x in (d2, d3):
            shutil.rmtree(x, ignore_errors=True)
        print("OK обновление с прошлой версии: начатая раскладка «Фотокамеры» сохраняется")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_failure_policy():
    """Разбор неудач: ничего не крутится по кругу, исправное не откладывается, скачанное не теряется."""
    from ya2gd import engine as eng
    from ya2gd.fetch import DeadHosts, Overloaded, SpeedGuard, StorageDown
    from ya2gd.http_util import ApiError, RateLimited
    d = tempfile.mkdtemp()
    try:
        e = eng.Engine(d)
        e._online = lambda: True
        items = [{"path": f"disk:/A/{i}.jpg", "source": "disk", "name": f"{i}.jpg", "size": 10} for i in range(40)]
        e._found(items, 1)

        def row(i):
            return dict(e.db.q1("SELECT * FROM files WHERE path=?", (f"disk:/A/{i}.jpg",)))

        # 503 — не беда файла: повтор через минуту, потоков меньше, файл НЕ в «Отложенных»
        before = len(e.gate.overloads)
        e._failed(row(0), Overloaded("перегружен (503)"), "yandex")
        r = row(0)
        assert r["status"] == "pending" and r["attempts"] == 1 and r["next_try"] > time.time() + 30, r
        assert len(e.gate.overloads) == before + 1
        assert eng.Overloaded in eng.STOP_ERRORS          # и в циклах скачивания 503 не превращается в «нет файла»

        # нет интернета: ни попытки, ни откладывания
        e._online = lambda: False
        e._failed(row(1), eng.LinkDown("Яндекс не выдал ссылку: сеть"), "yandex")
        e._failed(row(2), StorageDown("s1.storage", "не отвечает", network=True), "yandex")
        assert row(1)["status"] == "pending" and row(1)["attempts"] == 0 and e.offline_since
        assert row(2)["status"] == "pending" and row(2)["defers"] == 0
        e._online = lambda: True
        e.offline_since = 0

        # ссылки другим файлам выдаются — сбой ссылки одного файла: одна повторная попытка, потом «Отложенные»
        e._last_link_ok = time.time()
        e._failed(row(3), eng.LinkDown("500"), "yandex")
        assert row(3)["status"] == "pending" and row(3)["attempts"] == 1
        e._failed(row(3), eng.LinkDown("500"), "yandex")
        assert row(3)["status"] == "deferred"
        # ссылки не выдаются никому — пауза, а файл не виноват (волна сбоев из всех потоков — одна пауза)
        e._last_link_ok = time.time() - 120
        e._failed(row(4), eng.LinkDown("500"), "yandex")
        e._failed(row(6), eng.LinkDown("500"), "yandex")
        assert row(4)["status"] == "pending" and row(4)["attempts"] == 0 and e.ya_cooldown_until > time.time()
        assert row(6)["attempts"] == 0 and e.api_hits == 1, e.api_hits
        # …но поблажка — один раз за проход: файл в хвосте очереди не ходит по кругу
        e.ya_cooldown_until = 0
        e._failed(row(4), eng.LinkDown("500"), "yandex")
        assert row(4)["status"] == "pending" and row(4)["attempts"] == 1
        e._failed(row(4), eng.LinkDown("500"), "yandex")
        assert row(4)["status"] == "deferred"
        e.ya_cooldown_until = 0

        # волна 429 из нескольких потоков — одна пауза и одно уполовинивание потоков
        e.gate.configure(8, adaptive=True)
        lim = e.gate.state()["limit"]
        for _ in range(4):
            e._yandex_limited(RateLimited("429"))
        assert e.gate.state()["limit"] == max(1, lim // 2) and e.ya_hits == 1, (e.gate.state(), e.ya_hits)
        e.ya_cooldown_until = 0

        # сбой Google: скачанная копия в буфере не удаляется, после ручного повтора — снова «в буфере»
        dat = os.path.join(d, "5.dat")
        with open(dat, "wb") as f:
            f.write(b"x" * 10)
        e.db.update_file(row(5)["id"], status="uploading", local_path=dat)
        for _ in range(eng.GOOGLE_MAX_ATTEMPTS):
            e._failed(row(5), ApiError("Google: ошибка сервера 503", status=503, retryable=True, service="google"),
                      "google")
        r = row(5)
        assert r["status"] == "error" and os.path.exists(dat) and "копия сохранена" in (r["note"] or ""), r
        assert e.g_cooldown_until < time.time()        # сбои одного файла — не повод останавливать всё
        e.retry_errors()
        assert row(5)["status"] == "buffered"
        # а сбои у разных файлов подряд — пауза загрузки, растущая, пока Google не оживёт
        for i in (7, 8, 9):
            e._google_trouble(row(i)["id"])
        first = e.g_cooldown_until - time.time()
        e.g_cooldown_until = 0
        for i in (7, 8, 9):
            e._google_trouble(row(i)["id"])
        assert 50 < first <= 60 and e.g_cooldown_until - time.time() > 100, (first, e.g_trouble)
        e._add_speed("google", 1)
        assert e.g_trouble == 0
        e.g_cooldown_until = 0

        # «предохранитель»: возвращает файл в очередь не больше одного раза — мёртвые не ходят по кругу
        eng.BREAKER_STREAK, eng.BREAKER_QUIET = 5, 0
        e._last_ya_success = time.time() - 10
        ids = [row(i)["id"] for i in range(10, 15)]
        for i in range(10, 15):
            e._defer(row(i), "Яндекс не начал отдавать файл", outage=True)
        assert all(row(i)["status"] == "pending" for i in range(10, 15))
        e.ya_cooldown_until = 0
        for i in range(10, 15):
            e._defer(row(i), "Яндекс не начал отдавать файл", outage=True)
        assert all(row(i)["status"] == "deferred" for i in range(10, 15)), [row(i)["status"] for i in range(10, 15)]
        assert set(ids) <= e._breaker_reverted
        e.ya_cooldown_until = 0
        print("OK 503, пропавший интернет, сбой ссылки, волна 429, сбой Google, предохранитель — без кругов и потерь")

        # проверка интернета: после каждого сбоя — свежая (старое «интернет есть» отложило бы исправные
        # файлы при обрыве связи), но потоки, упавшие разом, ждут одну общую проверку
        del e._online
        calls = []

        class FakeSession:
            def get(self, url, timeout=None):
                calls.append(url)
                time.sleep(0.3)
                raise OSError("нет сети")
        real_session = eng.session
        eng.session = lambda: FakeSession()
        try:
            assert e._online() is False and e._online() is False
            assert len(calls) == 4, calls                  # два сбоя подряд — две проверки (по 2 адреса)
            calls.clear()
            ts = [threading.Thread(target=e._online) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert len(calls) <= 4, calls                  # шесть потоков разом — одна-две проверки
        finally:
            eng.session = real_session
        print("OK «есть ли интернет» проверяется после каждого сбоя; потоки, упавшие разом, ждут одну проверку")

        # сервер хранения «молчит» только после нескольких отказов на разных файлах
        dh = DeadHosts()
        assert not dh.strike("s1", connect_failed=False) and not dh.strike("s1", connect_failed=False)
        assert dh.strike("s1", connect_failed=False) and dh.is_dead("s1")
        assert not dh.strike("s2", connect_failed=True) and dh.strike("s2", connect_failed=True)
        dh.forget("s1")
        assert not dh.is_dead("s1")

        # сторож скорости: «по капле» дольше окна — файл откладывается
        g = SpeedGuard("x.mp4")
        g.start -= 200
        g.marks = [(g.start, 0)]
        try:
            g.add(1000)
            raise AssertionError("сторож скорости не сработал")
        except StorageDown as ex:
            assert "по капле" in str(ex)
        g2 = SpeedGuard("y.mp4")
        g2.add(10 ** 6)                       # быстро — ничего
        print("OK сервер считается молчащим после нескольких отказов; сторож скорости ловит «по капле»")

        # перевод старых «ждущих» в «Отложенные» — один раз, а не при каждом запуске
        e.db.update_file(row(20)["id"], status="pending", defers=1, next_try=time.time() + 600)
        e.db.close()
        e2 = eng.Engine(d)
        assert dict(e2.db.q1("SELECT status FROM files WHERE path='disk:/A/20.jpg'"))["status"] == "pending"
        e2.db.close()
        print("OK обновление базы выполняется один раз: повторённые вручную файлы не уходят обратно")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_stats_cache():
    """Сводка по базе кэшируется на секунду, но после конца списка или переноса — всегда свежая,
    и сводка, посчитанная до этого, в кэш не попадает (иначе «Список получен. Всего файлов: 0»)."""
    from ya2gd import engine as eng
    d = tempfile.mkdtemp()
    try:
        e = eng.Engine(d)
        assert e._stats()["by_status"] == {}
        e._found([{"path": f"disk:/S/{i}.jpg", "source": "disk", "name": f"{i}.jpg", "size": 10} for i in range(5)], 1)
        assert e._stats()["by_status"] == {}                      # в пределах секунды — из кэша
        e._stats_changed()
        assert e._stats()["by_status"]["pending"]["count"] == 5
        real = e.db.stats

        def racing():                     # пока сводка считается, список заканчивается
            st = real()
            e._stats_changed()
            return st
        e.db.stats = racing
        e._stats_changed()
        e._stats()
        e.db.stats = real
        assert e._stats_cache is None, "сводка, посчитанная до изменения, попала в кэш"
        e.db.close()
        print("OK сводка по базе: после конца списка и переноса — свежая, устаревшая в кэш не попадает")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_memqueue():
    q = MemQueue(100)
    stop = threading.Event()
    assert q.put("a", 60, stop) and q.put("b", 40, stop)
    t = threading.Thread(target=lambda: q.put("c", 50, stop))
    t.start()
    time.sleep(0.2)
    assert len(q) == 2                   # «c» ждёт, пока освободится место
    assert q.get() == "a"
    t.join(2)
    assert len(q) == 2 and q.get() == "b" and q.get() == "c" and q.get() is None
    assert q.put("big", 500, stop)       # больше лимита, но очередь пуста — берём
    print("OK очередь в памяти ограничена по объёму")


def test_probe_estimate():
    """Пробный замер и разметка: типы файлов, серия «всего понемногу», расчёт времени и «предохранитель»."""
    from ya2gd import probe as P
    MB, GB = 2 ** 20, 2 ** 30
    for name, kind in (("IMG_1.HEIC", "photo"), ("v.MOV", "video"), ("a.pdf", "doc"), ("b.7z", "archive"),
                       ("y.bin", "other"), ("noext", "other")):
        assert P.kind_of({"name": name}) == kind, name
    assert P.kind_of({"name": "x", "media_type": "video"}) == "video"
    assert P.kind_of({"name": "x.bin", "mime": "image/jpeg"}) == "photo"

    def mk(n, name, size, **kw):
        stem, ext = name.rsplit(".", 1)
        return [dict({"id": len(name) * 10 ** 6 + i, "path": f"disk:/{stem}{i}.{ext}", "name": f"{stem}{i}.{ext}",
                      "size": size, "section": 0, "status": "pending", "source": "disk", "probe": None}, **kw)
                for i in range(n)]
    rows = (mk(5000, "p.jpg", 3 * MB, section=1, source="photounlim") + mk(300, "v.mp4", 60 * MB) +
            mk(20, "big.mov", 2 * GB) + mk(40, "d.pdf", 200_000) + mk(5, "z.zip", 50 * MB))
    pick = P.choose_sample(rows, 24)
    kinds = {P.kind_of(r) for r in pick}
    buckets = {(P.kind_of(r), P.bucket_of(r["size"])) for r in pick}
    assert len(pick) == 24 and kinds == {"photo", "video", "doc", "archive"} and len(buckets) == 5, buckets

    meas = {"all": {"speed_bps": 10 * MB, "overhead_s": 0.5, "fail_s": 8.0, "ok": 20, "dead": 0},
            "kinds": {}, "ya_bps": 80 * MB, "g_bps": 10 * MB, "g_rtt_s": 0.1}
    iv = {"photounlim": 0.12, "disk": 0.15}

    def est(rs, m=meas):
        for r in rs:
            r["kind"] = P.kind_of(r)
        return P.estimate(rs, m, 8, 4, iv)
    # упор в Google: 100 × 100 МБ при 10 МБ/с на загрузку
    e1 = est(mk(100, "v.mp4", 100 * MB))
    assert abs(e1["total_s"] - (10000 / 10 + 100 * 0.2 / 4)) < 1 and e1["bottleneck"] == "google", e1
    # упор в суточный лимит Google: 1000 ГБ даже на быстром канале — больше суток
    fast = dict(meas, ya_bps=1000 * MB, g_bps=1000 * MB, all=dict(meas["all"], speed_bps=200 * MB))
    e2 = est(mk(1000, "big.mov", GB), fast)
    assert 86400 < e2["total_s"] < 86400 + 600 and e2["bottleneck"] == "limit", e2
    # упор в число файлов: 20 000 маленьких — ссылки не чаще одной в 0,12 с
    e3 = est(mk(20000, "p.jpg", 100_000, section=1, source="photounlim"), fast)
    assert abs(e3["total_s"] - 20000 * 0.12) < 1 and e3["bottleneck"] == "ya_files", e3
    assert abs(e3["mark_s"] - 20000 * 0.12) < 1 and e3["unlabeled"] == 20000
    # метки: «не отдаётся» — недоступен, но перенос его попробует; отложенный — недоступен и не пробуется;
    # уже в буфере — только загрузка в Google
    rs = mk(10, "a.jpg", MB, probe="dead") + mk(10, "b.jpg", MB, status="deferred") + \
        mk(10, "c.jpg", MB, status="buffered") + mk(10, "d.jpg", MB, probe="ok")
    e4 = est(rs)
    assert e4["lost_files"] == 20 and e4["avail_files"] == 20 and e4["unlabeled"] == 10, e4
    assert abs(e4["dead_s"] - 10 * 8.0 / 8) < 1e-6, e4["dead_s"]
    # доля доступных для неразмеченных — по разметке этого типа (если меток хватает)
    rs = mk(30, "e.jpg", MB, probe="ok") + mk(10, "f.jpg", MB, probe="dead") + mk(100, "g.jpg", MB)
    e5 = est(rs)
    assert e5["prior"]["photo"] == 0.75 and abs(e5["lost_files"] - (10 + 25)) <= 1, e5
    # без замера скорости — без выдуманных цифр
    assert P.estimate(rows[:3], None, 8, 4, iv) is None
    print("OK замер: типы файлов, серия из всех групп «тип × размер»; расчёт упирается в Google, "
          "в суточный лимит, в число файлов; метки разметки учитываются")

    # «предохранитель» разметки: лежит весь Яндекс — метки снимаются, файл проверяется ещё раз (один раз)
    class FakeDB:
        def __init__(self):
            self.cleared = []

        def set_probe(self, fid, result, *a, **k):
            if result is None:
                self.cleared.append(fid)

    class FakeEngine:
        db = FakeDB()
        logs = []

        def log(self, msg, level="info"):
            self.logs.append(msg)
    fe = FakeEngine()
    pr = P.Prober(fe, threading.Event())
    pr.last_ok = time.time() - 1000
    batch = [{"id": i} for i in range(P.OUTAGE_STREAK)]
    back = []
    for r in batch:
        pr.results[r["id"]] = {"result": "dead"}
        pr.counts["dead"] += 1
        back += pr._outage(r, {"result": "dead", "outage": True})
    assert len(back) == P.OUTAGE_STREAK and pr.pause_until > time.time() and not pr.results, back
    assert sorted(fe.db.cleared) == list(range(P.OUTAGE_STREAK))
    again = []
    for r in batch:
        again += pr._outage(r, {"result": "dead", "outage": True})
    assert again == [], "файл возвращён на проверку второй раз"
    pr2 = P.Prober(fe, threading.Event())
    pr2.last_ok = time.time() - 1000
    assert not any(pr2._outage({"id": i}, {"result": "dead", "outage": False}) for i in range(50))
    print("OK «предохранитель» разметки: при сбое всего Яндекса метки не ставятся, повторная проверка — один раз")


def _old_version_folder(base, google_email="old@example.com"):
    """Папка «прошлой версии»: settings.json с токеном/куки/входом Google и карта файлов в схеме версии 2
    (без колонок раздела, разметки и повторов), с её статусами."""
    import sqlite3
    d = os.path.join(base, "old", "data")
    os.makedirs(d)
    with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"yandex_token": "tok-old", "yandex_cookies": "Session_id=s; sessionid2=x; yandexuid=1",
                   "google_client_id": "cid-old", "google_client_secret": "sec-old",
                   "google_refresh_token": "rt-old", "google_email": google_email,
                   "photos_root": "Архив/Фото", "files_root": "Архив/Файлы", "settings_v": 2}, f)
    c = sqlite3.connect(os.path.join(d, "state.sqlite3"))
    c.executescript("""
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, source TEXT NOT NULL, name TEXT NOT NULL,
          size INTEGER NOT NULL DEFAULT 0, md5 TEXT, sha256 TEXT, modified TEXT, created TEXT, taken TEXT, mime TEXT,
          media_type TEXT, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          next_try REAL NOT NULL DEFAULT 0, gd_id TEXT, upload_uri TEXT, bytes_done INTEGER NOT NULL DEFAULT 0, note TEXT,
          verified REAL, seen_scan INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, local_path TEXT,
          defers INTEGER NOT NULL DEFAULT 0, preview_id TEXT, done_at REAL, uploaded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE folders (path TEXT PRIMARY KEY, gd_id TEXT NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """)
    future = time.time() + 3600
    rows = [("disk:/Отпуск/a.jpg", "done", 0, 0, "gd-a", None),
            ("disk:/Отпуск/b.jpg", "pending", 2, future, None, None),       # версия 2: «ждёт сервер Яндекса»
            ("disk:/Отпуск/c.jpg", "error", 0, 0, None, None),
            ("disk:/Отпуск/d.jpg", "active", 0, 0, None, None),
            ("disk:/Отпуск/e.jpg", "buffered", 0, 0, None, "D:/buf/ya2gd-buffer/5.dat"),
            ("photounlim:/2019-05-04 10-00-00.JPG", "pending", 0, 0, None, None)]
    for path, st, defers, nt, gd, lp in rows:
        c.execute("INSERT INTO files(path, source, name, size, md5, status, defers, next_try, gd_id, local_path) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (path, path.split(":")[0], path.rsplit("/", 1)[-1], 1000, "m" * 32,
                                                  st, defers, nt, gd, lp))
    c.execute("INSERT INTO folders VALUES('Архив/Файлы/Отпуск', 'fold-1')")
    c.executemany("INSERT INTO kv VALUES(?,?)", [("scan.state", "done"), ("scan.phase", "done"), ("scan.found", "6")])
    c.commit()
    c.close()
    return os.path.dirname(d)


def test_profiles_and_accounts():
    """Профили и общая библиотека аккаунтов: прошлые настройки переезжают в библиотеку, один аккаунт — в нескольких
    профилях, общие настройки программы — одни, у каждого профиля свой буфер и журнал, удаление профиля."""
    from ya2gd import profiles as P
    from ya2gd.engine import Buffer
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"yandex_token": "tok-1", "google_client_id": "cid", "google_client_secret": "sec",
                       "google_refresh_token": "rt-1", "google_email": "me@example.com", "port": 9001,
                       "settings_v": 3}, f)
        m = P.Manager(d)
        main = m.main
        assert main.settings["yandex_token"] == "tok-1" and main.settings["google_email"] == "me@example.com"
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            stored = json.load(f)
        assert "yandex_token" not in stored and "google_refresh_token" not in stored, stored
        ya, g = main.settings["yandex_account"], main.settings["google_account"]
        assert ya and g and m.accounts.get("yandex", ya, "yandex_token") == "tok-1"
        # второй профиль с тем же аккаунтом Яндекса и без Google: новый токен виден обоим
        pid = m.create("Мамин Диск", yandex_account=ya)
        p2 = m.get(pid)
        assert p2.settings["yandex_token"] == "tok-1" and p2.settings["google_refresh_token"] == ""
        p2.settings.update(yandex_token="tok-2")
        assert main.settings["yandex_token"] == "tok-2"
        # ключи Google в профиле без аккаунта — новый аккаунт в библиотеке
        p2.settings.update(google_client_id="cid-2")
        assert p2.settings["google_account"] and p2.settings["google_account"] != g
        # окно/порт/автозапуск — общие
        assert p2.settings["port"] == 9001
        p2.settings.update(port=9002)
        assert main.settings["port"] == 9002
        pub = p2.settings.public()
        assert pub["yandex_token_set"] and "tok-2" not in json.dumps(pub, ensure_ascii=False), pub
        # свой буфер и свой журнал
        assert Buffer("X:/b", p2.buffer_sub).dir != Buffer("X:/b", main.buffer_sub).dir
        p2.log("строка второго профиля")
        main.log("строка основного")
        with open(os.path.join(m._dir(pid), "ya2gd.log"), encoding="utf-8") as f:
            text = f.read()
        assert "второго профиля" in text and "основного" not in text
        # повторное открытие: профили и аккаунты на месте
        names = [p["name"] for p in m.meta["profiles"]]
        m2 = P.Manager(d)
        assert [p["name"] for p in m2.meta["profiles"]] == names and m2.get(pid).settings["yandex_token"] == "tok-2"
        # аккаунт, выбранный в профиле, удалить нельзя; профиль — можно
        try:
            m2.delete_account("yandex", ya)
            raise AssertionError("удалился аккаунт, выбранный в профилях")
        except P.ApiError:
            pass
        for mm in (m, m2):
            for e in mm.engines.values():
                if e.profile_id == pid:
                    e.db.close()
                    e.log.close()
        m.engines.pop(pid)
        m2.delete(pid)
        assert not os.path.exists(m2._dir(pid)) and m2.profile(pid) is None
        for mm in (m, m2):
            for e in mm.engines.values():
                e.db.close()
                e.log.close()
        print("OK профили: аккаунты в общей библиотеке, один аккаунт в нескольких профилях, общие настройки окна, "
              "свой буфер и журнал, удаление профиля")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_queue_and_schedule():
    """Очередь профилей друг за другом, «Старт» во время чужого переноса — ожидание, повтор отложенных по расписанию."""
    from ya2gd import profiles as P
    d = tempfile.mkdtemp()
    try:
        m = P.Manager(d)
        ids = ["main", m.create("Второй"), m.create("Третий")]
        run = {}

        def fake(e):
            e.db.set("scan.state", "done")
            e.transferring = lambda: run.get(e.profile_id) == "run"

            def start():
                run[e.profile_id] = "run"
                e.transfer_state = "running"
                return True
            e.start_transfer = start
        for e in m.engines.values():
            fake(e)

        def finish(pid, state="done", wanted="0"):
            run[pid] = "stop"
            m.get(pid).transfer_state = state
            m.get(pid).db.set("transfer.wanted", wanted)
        m.set_queue(ids[2], False)
        m.queue_start()
        assert run.get("main") == "run" and m.meta["queue"]["current"] == "main"
        m._tick()
        assert ids[1] not in run                       # пока основной переносит — второй ждёт
        finish("main")
        m._tick()
        assert run.get(ids[1]) == "run" and ids[2] not in run
        # человек нажал «Пауза» — очередь ждёт, «Продолжить» запускает тот же профиль
        finish(ids[1], state="paused", wanted="0")
        m._tick()
        assert not m.meta["queue"]["active"] and m.meta["queue"]["current"] == ids[1]
        m.queue_start()
        assert run.get(ids[1]) == "run"
        finish(ids[1])
        m._tick()
        assert not m.meta["queue"]["active"] and ids[2] not in run, m.meta["queue"]
        # «Старт» третьего, пока переносит основной: ожидание и запуск следом
        run["main"] = "run"
        r = m.start(ids[2])
        assert r["queued"] and ids[2] in m.waiting
        finish("main")
        m._tick()
        assert run.get(ids[2]) == "run" and not m.waiting
        finish(ids[2])
        # повтор отложенных по расписанию: раз в день, последний — два дня назад
        e = m.get(ids[1])
        e._found([{"path": "disk:/x.jpg", "source": "disk", "name": "x.jpg", "size": 5}], 1)
        e.db.x("UPDATE files SET status='deferred'")
        e.settings.update(deferred_auto="day")
        e.db.set("deferred.last_retry", time.time() - 2 * 86400)
        assert e.auto_retry_due()
        run[ids[1]] = "stop"
        m._tick()
        assert run.get(ids[1]) == "run" and e.db.count_status("deferred") == 0
        info = e.deferred_info()
        assert info["retry"]["total"] == 1 and info["retry"]["auto"] and info["next_auto"] > time.time() + 80000
        finish(ids[1])
        e.db.x("UPDATE files SET status='deferred'")
        assert not e.auto_retry_due()                   # следующий — только через сутки
        e.settings.update(deferred_auto="off")
        e.db.set("deferred.last_retry", 0)
        assert not e.auto_retry_due()
        for x in m.engines.values():
            x.db.close()
            x.log.close()
        print("OK очередь профилей друг за другом (пауза и продолжение), ожидание «Старта», "
              "повтор отложенных по расписанию раз в день")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_import_export_map():
    """Перенос из прошлой версии: карта файлов и аккаунты без нового входа и без нового списка; экспорт карты
    и её импорт; ход повтора отложенных; ссылки на Яндекс Диск."""
    from ya2gd import profiles as P
    from ya2gd.engine import yandex_web_url
    d = tempfile.mkdtemp()
    try:
        old = _old_version_folder(d)
        root = os.path.join(d, "new")
        os.makedirs(root)
        m = P.Manager(root)
        info = m.inspect_import(old)              # папка, где лежал старый exe (внутри — data)
        assert info["files"] == 6 and info["yandex_token"] and info["google_email"] == "old@example.com", info
        res = m.import_old(old, "main")
        e = m.main
        assert res["profile"] == "main" and res["added"] == 6 and res["accounts"] == ["google", "yandex"], res
        assert e.settings["yandex_token"] == "tok-old" and e.settings["google_email"] == "old@example.com"
        assert e.settings["photos_root"] == "Архив/Фото" and e.settings["files_root"] == "Архив/Файлы"
        st = {r["path"]: dict(r) for r in e.db.q("SELECT * FROM files")}
        assert st["disk:/Отпуск/a.jpg"]["status"] == "done" and st["disk:/Отпуск/a.jpg"]["gd_id"] == "gd-a"
        assert st["disk:/Отпуск/b.jpg"]["status"] == "deferred"            # «ждёт сервер» версии 2
        assert st["disk:/Отпуск/d.jpg"]["status"] == "pending"
        assert st["disk:/Отпуск/e.jpg"]["status"] == "pending" and st["disk:/Отпуск/e.jpg"]["local_path"] is None
        assert st["photounlim:/2019-05-04 10-00-00.JPG"]["section"] == 1
        assert e.db.get("scan.state") == "done" and e.db.folder_get("Архив/Файлы/Отпуск") == "fold-1"
        # повторный импорт той же карты в непустой профиль — новый профиль, аккаунты те же (без дублей)
        res2 = m.import_old(old, "main")
        assert res2["profile"] != "main" and len(m.accounts.listing("yandex")) == 1, (res2, m.accounts.listing("yandex"))
        # экспорт карты и импорт файла карты в ещё один профиль
        exp = os.path.join(d, "map.sqlite3")
        e.export_map(exp)
        pid = m.create("Из файла")
        res3 = m.import_old(exp, pid, with_accounts=False)
        assert res3["profile"] == pid and res3["added"] == 6 and m.get(pid).db.count_status("done") == 1
        # ход повтора отложенных
        e.retry_errors()
        e.db.x("UPDATE files SET status='done' WHERE path='disk:/Отпуск/b.jpg'")
        e.db.x("UPDATE files SET status='deferred' WHERE path='disk:/Отпуск/c.jpg'")
        e._stats_changed()
        r = e.deferred_info()["retry"]
        assert r["total"] == 2 and r["done"] == 1 and r["deferred"] == 1 and r["left"] == 0, r
        # карту, с которой программа работает, импортировать нельзя
        try:
            m.inspect_import(root)
            raise AssertionError("импортировалась своя же карта")
        except P.ApiError:
            pass
        assert yandex_web_url("disk:/Отпуск/a.jpg").startswith("https://disk.yandex.ru/client/disk/%D0%9E")
        assert "idDialog=%2Fphotounlim%2F" in yandex_web_url("photounlim:/x.jpg")
        for x in m.engines.values():
            x.db.close()
            x.log.close()
        print("OK перенос из прошлой версии: карта и аккаунты без нового входа и списка, статусы версии 2 переведены; "
              "экспорт/импорт карты; ход повтора отложенных; ссылки на Яндекс Диск")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_db_claims():
    d = tempfile.mkdtemp()
    try:
        db = DB(os.path.join(d, "s.sqlite3"))
        items = [{"path": f"disk:/f{i}", "source": "disk", "name": f"f{i}", "size": sz, "md5": None}
                 for i, sz in enumerate([500, 10, 3000])]
        items.append({"path": "photounlim:/p.jpg", "source": "photounlim", "name": "p.jpg", "size": 50})
        db.upsert_files(items, 1)
        r = db.claim_download(order="photos_first")
        assert r["path"] == "photounlim:/p.jpg"
        r = db.claim_download(order="small_first")
        assert r["size"] == 10
        # буфер: свободно 600, размер буфера 1000 → 3000 > буфера идёт напрямую, 500 влезает
        r = db.claim_download(order="as_listed", fits=(600, 1000))
        assert r["size"] == 500
        r = db.claim_download(order="as_listed", fits=(0, 1000))
        assert r["size"] == 3000
        assert db.claim_download() is None
        assert db.reset_active() == 4 and db.count_status("pending") == 4
        # файл, который разметка нашла недоступным, перенос берёт последним
        pid = db.q1("SELECT id FROM files WHERE path='photounlim:/p.jpg'")[0]
        db.set_probe(pid, "dead", note="молчит")
        r = db.claim_download(order="photos_first")
        assert r["path"] != "photounlim:/p.jpg", r
        db.close()                       # Windows не даёт удалить открытый файл базы
        print("OK очередь: сначала «Фото», маленькие/по порядку, учёт места в буфере; недоступные по разметке — последними")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_gate()
    test_settings_migration()
    test_layout()
    test_routing_sql_matches_python()
    test_disk_warnings()
    test_sections_and_speed()
    test_failure_policy()
    test_stats_cache()
    test_memqueue()
    test_probe_estimate()
    test_profiles_and_accounts()
    test_queue_and_schedule()
    test_import_export_map()
    test_db_claims()
    print("\nМОДУЛЬНЫЕ ПРОВЕРКИ ПРОЙДЕНЫ")
