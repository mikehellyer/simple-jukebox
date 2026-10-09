import sys

from PySide6.QtWidgets import QApplication

from simple_jukebox import __version__
from simple_jukebox.core.diagnostics import enable_app_log, enable_crash_log, log_event
from simple_jukebox.gui.main_window import MainWindow


def main() -> int:
    enable_app_log(__version__)
    enable_crash_log()
    app = QApplication(sys.argv)
    app.setApplicationName("Simple-Jukebox")
    window = MainWindow()
    window.show()
    code = app.exec()
    log_event(f"event loop finished (code {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
