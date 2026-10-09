"""The persistent "an update is available" banner shown above the tabs."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

RED = "#c0392b"


class UpdateBanner(QWidget):
    update_clicked = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: rgba(192, 57, 43, 0.12);")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        self._label = QLabel()
        self._label.setStyleSheet(f"color: {RED}; font-weight: 600;")
        # set_status() can now show a failed installer's actual stderr
        # text (see MainWindow._on_installer_finished) — unbounded
        # length, unlike the short fixed messages this label used to
        # only ever show.
        self._label.setWordWrap(True)
        layout.addWidget(self._label, stretch=1)

        self._update_button = QPushButton("Update")
        self._update_button.setStyleSheet(
            f"QPushButton {{ color: white; background-color: {RED}; "
            "padding: 4px 14px; border-radius: 4px; font-weight: 600; }"
            "QPushButton:disabled { background-color: #d8a29b; }"
        )
        self._update_button.clicked.connect(self.update_clicked)
        layout.addWidget(self._update_button)

        dismiss_button = QPushButton("✕")
        dismiss_button.setFlat(True)
        dismiss_button.setStyleSheet(f"color: {RED}; font-weight: 600; border: none;")
        dismiss_button.setFixedWidth(28)
        dismiss_button.clicked.connect(self._on_dismiss)
        layout.addWidget(dismiss_button)

        self.hide()

    def announce(self, version: str) -> None:
        self._label.setText(f"Update available: {version}")
        self._update_button.setText("Update")
        self._update_button.setEnabled(True)
        self.show()

    def set_status(self, text: str) -> None:
        self._label.setText(text)

    def set_busy(self, busy: bool, button_text: str = "Update") -> None:
        self._update_button.setEnabled(not busy)
        self._update_button.setText(button_text)

    def _on_dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()
