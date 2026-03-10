"""Tests for wachtmeater/create_meater_watcher_job.py.

Requires kubernetes (imported at module level by create_meater_watcher_job).
"""

from unittest.mock import MagicMock, call

import pytest

k8s_mod = pytest.importorskip("wachtmeater.create_meater_watcher_job")

# Importing ApiException after importorskip guarantees kubernetes is available
from kubernetes.client import ApiException

# ============================================================================
# _short_uuid
# ============================================================================


class TestShortUuid:
    """Tests for _short_uuid(meater_url)."""

    def test_basic(self) -> None:
        url = "https://cooks.cloud.meater.com/cook/abc-123-def"
        assert k8s_mod._short_uuid(url) == "abc123de"

    def test_trailing_slash_stripped(self) -> None:
        url = "https://cooks.cloud.meater.com/cook/abc-123-def/"
        assert k8s_mod._short_uuid(url) == "abc123de"

    def test_no_dashes(self) -> None:
        url = "https://cooks.cloud.meater.com/cook/abcdefghijklmnop"
        assert k8s_mod._short_uuid(url) == "abcdefgh"

    def test_uppercase_lowered(self) -> None:
        url = "https://cooks.cloud.meater.com/cook/ABC-DEF-GHI"
        assert k8s_mod._short_uuid(url) == "abcdefgh"

    def test_short_uuid(self) -> None:
        url = "https://cooks.cloud.meater.com/cook/ab"
        assert k8s_mod._short_uuid(url) == "ab"


# ============================================================================
# build_env_content
# ============================================================================


class TestBuildConfigContent:
    """Tests for build_config_content(meater_url)."""

    URL = "https://cooks.cloud.meater.com/cook/abc-123-def"

    def test_url_in_output(self) -> None:
        content = k8s_mod.build_config_content(self.URL)
        assert self.URL in content

    def test_uuid_in_state_file(self) -> None:
        content = k8s_mod.build_config_content(self.URL)
        assert "abc-123-def" in content
        # state_file_name TOML key should contain the extracted UUID
        for line in content.splitlines():
            if "state_file_name" in line:
                assert "abc-123-def" in line
                break
        else:
            pytest.fail("state_file_name key not found")

    def test_required_toml_keys(self) -> None:
        content = k8s_mod.build_config_content(self.URL)
        required = [
            "cdp_url",
            "url",
            "homeserver",
            "user",
            "password",
            "crypto_store_path",
            "method",
            "operator_url",
        ]
        for key in required:
            assert key in content, f"Missing key: {key}"

    def test_different_urls_different_state_files(self) -> None:
        c1 = k8s_mod.build_config_content("https://cooks.cloud.meater.com/cook/uuid-one")
        c2 = k8s_mod.build_config_content("https://cooks.cloud.meater.com/cook/uuid-two")

        # Extract state_file_name values
        def _state_file(content: str) -> str:
            for line in content.splitlines():
                if "state_file_name" in line:
                    return line
            return ""

        assert _state_file(c1) != _state_file(c2)


# ============================================================================
# apply_resource
# ============================================================================


class TestApplyResource:
    """Tests for apply_resource(create_fn, replace_fn, kind)."""

    def test_create_succeeds(self) -> None:
        create = MagicMock()
        replace = MagicMock()
        k8s_mod.apply_resource(create, replace, "test/resource")
        create.assert_called_once()
        replace.assert_not_called()

    def test_create_409_calls_replace(self) -> None:
        create = MagicMock(side_effect=ApiException(status=409))
        replace = MagicMock()
        k8s_mod.apply_resource(create, replace, "test/resource")
        create.assert_called_once()
        replace.assert_called_once()

    def test_create_500_reraises(self) -> None:
        create = MagicMock(side_effect=ApiException(status=500))
        replace = MagicMock()
        with pytest.raises(ApiException):
            k8s_mod.apply_resource(create, replace, "test/resource")
        replace.assert_not_called()
