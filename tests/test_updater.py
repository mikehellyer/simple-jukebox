from unittest.mock import patch

from simple_jukebox.core.updater import check_for_update, is_newer


def test_is_newer_detects_a_higher_version():
    assert is_newer("v1.2.0", "1.1.0")
    assert not is_newer("v1.1.0", "1.1.0")
    assert not is_newer("v1.0.9", "1.1.0")


def test_check_for_update_returns_none_when_unreachable():
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        assert check_for_update("0.1.0", "owner", "repo") is None


def test_check_for_update_returns_none_when_up_to_date():
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"tag_name": "v0.1.0", "html_url": "https://example.com"}'

    with patch("urllib.request.urlopen", return_value=_Response()):
        assert check_for_update("0.1.0", "owner", "repo") is None


def test_check_for_update_returns_info_when_newer():
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"tag_name": "v0.2.0", "html_url": "https://example.com/rel", "body": "notes"}'

    with patch("urllib.request.urlopen", return_value=_Response()):
        info = check_for_update("0.1.0", "owner", "repo")

    assert info is not None
    assert info.version == "v0.2.0"
    assert info.url == "https://example.com/rel"


def test_check_for_update_captures_release_assets():
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return (
                b'{"tag_name": "v0.2.0", "html_url": "https://example.com/rel", '
                b'"assets": ['
                b'{"name": "Simple-Jukebox-0.2.0-macOS.dmg", "browser_download_url": "https://dl/mac.dmg"},'
                b'{"name": "simple-jukebox_0.2.0_amd64.deb", "browser_download_url": "https://dl/lin.deb"}'
                b"]}"
            )

    with patch("urllib.request.urlopen", return_value=_Response()):
        info = check_for_update("0.1.0", "owner", "repo")

    assert info is not None
    assert info.assets == [
        ("Simple-Jukebox-0.2.0-macOS.dmg", "https://dl/mac.dmg"),
        ("simple-jukebox_0.2.0_amd64.deb", "https://dl/lin.deb"),
    ]
