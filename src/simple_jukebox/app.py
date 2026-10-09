import sys

from PySide6.QtWidgets import QApplication

from simple_jukebox.core.diagnostics import enable_crash_log
from simple_jukebox.gui.main_window import MainWindow


def main() -> int:
    enable_crash_log()
    app = QApplication(sys.argv)
    app.setApplicationName("Simple-Jukebox")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
