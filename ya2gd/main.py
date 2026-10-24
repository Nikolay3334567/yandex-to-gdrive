"""Точка входа: поднимает интерфейс на 127.0.0.1 и открывает окно программы."""
import argparse
import os
import threading
import time

from . import __version__, winsys
from .core import data_dir
from .profiles import Manager
from .web import serve

BANNER = """
  Яндекс Диск → Google Drive  v{v}
  ------------------------------------------------------------
  Это окно — сама программа. НЕ закрывайте его, пока идёт перенос.
  Окно с интерфейсом можно закрывать и открывать снова: {url}
  Если окно закрыли или выключили свет — запустите программу снова,
  перенос продолжится с того же места.

  Открытый исходный код, лицензия MIT. Программа предоставляется «как есть»,
  без гарантий; автор не несёт ответственности за последствия её использования.
  https://github.com/Nikolay3334567/yandex-to-gdrive
"""


def main():
    ap = argparse.ArgumentParser(description="Перенос Яндекс Диска на Google Drive")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    winsys.prepare_console()
    ddir = data_dir()
    lock = winsys.InstanceLock(ddir)
    port_file = os.path.join(ddir, "instance.port")
    if not lock.acquire():
        try:
            port = int(open(port_file).read().strip())
        except (OSError, ValueError):
            port = args.port or 8765
        url = f"http://127.0.0.1:{port}/"
        print(f"Программа уже запущена. Открываю её окно: {url}")
        if not args.no_browser:
            winsys.open_ui(url)
        time.sleep(2)
        return

    manager = Manager(ddir)
    engine = manager.main
    port = args.port or engine.settings["port"]
    try:
        httpd = serve(manager, port)
    except OSError:
        httpd = serve(manager, 0)           # порт занят чужой программой — берём любой свободный
        engine.log(f"Порт {port} занят другой программой, интерфейс на порту {httpd.server_address[1]}", "warn")
    port = httpd.server_address[1]
    with open(port_file, "w") as f:
        f.write(str(port))
    url = f"http://127.0.0.1:{port}/"
    print(BANNER.format(v=__version__, url=url), flush=True)
    engine.log(f"Интерфейс: {url}. Данные и журнал: {ddir}")
    winsys.set_title(f"Яндекс → Google — НЕ закрывайте это окно ({url})")

    manager.autostart()
    manager.run()                           # очередь профилей, «Старт» в ожидании, повтор по расписанию

    if not args.no_browser:
        def show():
            if winsys.open_ui(url, engine.settings["app_window"]):
                time.sleep(3)
                winsys.minimize_console()
        threading.Timer(1.0, show).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        engine.log("Остановка по Ctrl+C")
        manager.close()
        for e in manager.engines.values():
            e.stop_scanning()
            e.pause_transfer("закрытие программы", by_user=False)
        winsys.allow_sleep()


if __name__ == "__main__":
    main()
