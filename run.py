"""Запуск Локалки: python run.py  (или двойной щелчок по run.bat)."""
import os
import sys

if sys.version_info < (3, 7):
    sys.exit("Нужен Python 3.7 или новее, а здесь {}.".format(sys.version.split()[0]))

sys.dont_write_bytecode = True  # не оставлять __pycache__ на флешке
# Переносимый Python (embeddable) сам не добавляет папку скрипта в пути поиска модулей.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lokalka.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
