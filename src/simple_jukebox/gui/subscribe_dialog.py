"""Podcasts ▸ Subscribe…: find a show by searching the Apple Podcasts
directory, or paste its RSS feed address directly."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from simple_jukebox.core.podcast_search import search_podcasts


class SubscribeDialog(QDialog):
    def __init__(self, run_in_background: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Subscribe to a Podcast")
        self.resize(560, 480)
        self._run_in_background = run_in_background
        self.feed_url: Optional[str] = None

        layout = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self._query = QLineEdit()
        self._query.setPlaceholderText("Search for a podcast by name…")
        self._query.returnPressed.connect(self._search)
        search_row.addWidget(self._query, stretch=1)
        self._search_button = QPushButton("Search")
        self._search_button.clicked.connect(self._search)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._results = QListWidget()
        self._results.setWordWrap(True)
        self._results.itemDoubleClicked.connect(lambda _item: self._accept_result())
        self._results.currentItemChanged.connect(lambda *_: self._update_button())
        layout.addWidget(self._results, stretch=1)

        self._message = QLabel("Results come from the Apple Podcasts directory.")
        self._message.setStyleSheet("color: gray;")
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        layout.addWidget(QLabel("Or paste the podcast's feed address (RSS):"))
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://example.com/podcast/feed.xml")
        self._url.textChanged.connect(lambda _text: self._update_button())
        layout.addWidget(self._url)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._subscribe_button = buttons.addButton("Subscribe", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_button()

    def _search(self) -> None:
        query = self._query.text().strip()
        if not query:
            return
        self._search_button.setEnabled(False)
        self._message.setText("Searching…")
        self._run_in_background(lambda: search_podcasts(query), self._on_results)

    def _on_results(self, results) -> None:
        self._search_button.setEnabled(True)
        self._results.clear()
        for result in results:
            item = QListWidgetItem(f"{result.name}\n{result.description}")
            item.setData(Qt.UserRole, result.feed_url)
            item.setToolTip(result.feed_url)
            self._results.addItem(item)
        self._message.setText(
            f"{len(results)} found — pick one and click Subscribe."
            if results
            else "Nothing found (or the directory couldn't be reached). Try other words, or paste the feed address."
        )
        if results:
            self._results.setCurrentRow(0)

    def _update_button(self) -> None:
        self._subscribe_button.setEnabled(bool(self._url.text().strip() or self._results.currentItem()))

    def _accept_result(self) -> None:
        item = self._results.currentItem()
        if item is not None:
            self.feed_url = item.data(Qt.UserRole)
            self.accept()

    def _accept(self) -> None:
        url = self._url.text().strip()
        if url:
            if "://" not in url:
                url = "https://" + url
            self.feed_url = url
            self.accept()
        else:
            self._accept_result()
