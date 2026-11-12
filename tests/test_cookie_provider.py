"""Regression tests for automatic Netscape-cookie loading."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.app_paths import AppPaths
from core.cookie_provider import (
    CookieProvider,
    build_cookie_header,
    parse_netscape_cookie_file,
)


def _netscape_row(
    domain: str,
    include_subdomains: str,
    expires: int,
    name: str,
    value: str,
    *,
    path: str = "/",
    secure: str = "TRUE",
) -> str:
    return "\t".join(
        [domain, include_subdomains, path, secure, str(expires), name, value]
    )


def test_app_paths_keep_existing_layout_without_writing(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)

    assert paths.project_root == tmp_path.resolve()
    assert paths.cookie_file == tmp_path / "www.pixiv.net_cookies.txt"
    assert paths.legacy_config_file == tmp_path / "pixiv_config.json"
    assert paths.downloaded_ids_file == tmp_path / "downloaded_ids.txt"
    assert paths.download_logs_dir == tmp_path / "download_logs"
    assert not paths.cookie_file.exists()


def test_netscape_parser_filters_domain_and_expired_rows(tmp_path: Path) -> None:
    path = tmp_path / "www.pixiv.net_cookies.txt"
    path.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                _netscape_row(".pixiv.net", "TRUE", 0, "session", "kept"),
                _netscape_row("www.pixiv.net", "FALSE", 4, "short", "kept"),
                _netscape_row(".pixiv.net", "TRUE", 99, "expired", "gone"),
                _netscape_row("evilpixiv.net", "TRUE", 9999999999, "evil", "gone"),
                "#HttpOnly_.pixiv.net\tTRUE\t/\tTRUE\t9999999999\thttp\tkept",
                "malformed row",
            ]
        ),
        encoding="utf-8",
    )

    records = parse_netscape_cookie_file(path, host="https://www.pixiv.net:443")
    active_names = {record.name for record in records if record.expires in (None, 9999999999)}

    assert active_names == {"session", "http"}
    assert {record.domain for record in records} == {"pixiv.net", "www.pixiv.net"}

    # Expiry is evaluated separately so a parsed cache can be reused.
    provider = CookieProvider(cookie_path=path, host="www.pixiv.net", clock=lambda: 100)
    assert provider.get_cookie_header() == "session=kept; http=kept"


def test_root_cookie_file_has_priority_over_legacy_config(tmp_path: Path) -> None:
    cookie_path = tmp_path / "www.pixiv.net_cookies.txt"
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n"
        + _netscape_row(".pixiv.net", "TRUE", 9999999999, "root_cookie", "root-secret")
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "pixiv_config.json").write_text(
        json.dumps({"cookie": "legacy_cookie=legacy-secret"}),
        encoding="utf-8",
    )

    provider = CookieProvider(project_root=tmp_path, clock=lambda: 100)

    assert provider.get_cookie_header() == "root_cookie=root-secret"
    assert provider.source == "netscape"
    assert provider.source_path == cookie_path.resolve()


def test_legacy_cookie_fallback_is_used_when_export_missing(tmp_path: Path) -> None:
    (tmp_path / "pixiv_config.json").write_text(
        json.dumps({"cookie": "a=1; b=two=parts"}),
        encoding="utf-8",
    )

    provider = CookieProvider(project_root=tmp_path)

    assert provider.get_cookie_header() == "a=1; b=two=parts"
    assert provider.source == "legacy"


def test_mtime_cache_reloads_only_after_file_metadata_changes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "www.pixiv.net_cookies.txt"
    path.write_text(
        _netscape_row(".pixiv.net", "TRUE", 9999999999, "a", "one") + "\n",
        encoding="utf-8",
    )
    provider = CookieProvider(cookie_path=path, clock=lambda: 100)
    original_parser = __import__("core.cookie_provider", fromlist=["parse_netscape_cookie_file"])
    calls = 0
    original = original_parser.parse_netscape_cookie_file

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(original_parser, "parse_netscape_cookie_file", counted)
    assert provider.get_cookie_header() == "a=one"
    assert provider.get_cookie_header() == "a=one"
    assert calls == 1

    path.write_text(
        _netscape_row(".pixiv.net", "TRUE", 9999999999, "a", "two") + "\n",
        encoding="utf-8",
    )
    assert provider.get_cookie_header() == "a=two"
    assert calls == 2


def test_no_sensitive_values_are_emitted_by_repr_or_optional_logger(tmp_path: Path, caplog) -> None:
    secret = "secret-cookie-value"
    path = tmp_path / "www.pixiv.net_cookies.txt"
    path.write_text(
        _netscape_row(".pixiv.net", "TRUE", 9999999999, "session", secret) + "\n",
        encoding="utf-8",
    )
    logger = logging.getLogger("cookie-provider-test")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        provider = CookieProvider(cookie_path=path, clock=lambda: 100, logger=logger)
        records = provider.get_records()
        repr(records)
        assert provider.get_cookie_header() == f"session={secret}"

    assert secret not in caplog.text


def test_header_deduplicates_by_specificity_and_preserves_equals() -> None:
    path_records = [
        # The exact host wins over the parent domain.
        *parse_netscape_cookie_file_from_lines(
            [
                _netscape_row(".pixiv.net", "TRUE", 9999999999, "same", "parent"),
                _netscape_row("www.pixiv.net", "FALSE", 9999999999, "same", "exact"),
            ]
        ),
    ]
    assert build_cookie_header(path_records) == "same=exact"


def test_production_fetch_config_uses_automatic_provider(monkeypatch) -> None:
    import core.pixiv_fetcher as fetcher_module

    monkeypatch.setattr(fetcher_module, "resolve_cookie_header", lambda value: value or "auto=1")
    config = fetcher_module.FetchConfig()
    assert config.auth_cookie == "auto=1"


def test_auth_context_and_fetch_config_repr_redact_cookie_values() -> None:
    from core.auth_context import AuthContext
    from core.pixiv_fetcher import FetchConfig

    secret = "session=do-not-log-this"
    assert secret not in repr(AuthContext(cookie_header=secret))
    assert secret not in repr(FetchConfig(auth_cookie=secret))


def test_original_meta_cache_key_does_not_retain_cookie_value(monkeypatch) -> None:
    import core.download_engine as engine

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"body": {"urls": {"original": "https://i.pximg.net/img-original/img/123_p0.jpg"}, "pageCount": 1}}

    monkeypatch.setattr(engine, "request_get", lambda *args, **kwargs: Response())
    engine._META_CACHE.clear()
    secret = "not-safe-to-retain"
    engine.fetch_original_meta("123", cookie=secret)
    assert engine._META_CACHE
    assert all(secret not in key for key in engine._META_CACHE)


def parse_netscape_cookie_file_from_lines(lines: list[str]):
    """Small test helper kept local to avoid creating a fixture file."""

    from core.cookie_provider import parse_netscape_cookie_lines

    return parse_netscape_cookie_lines(lines, host="www.pixiv.net")
