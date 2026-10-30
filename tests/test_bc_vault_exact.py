"""Exact-name vault lookup avoids case-insensitive CLI ambiguity."""
import json
from unittest import mock

import pytest

from billcollector import vault


def response(data):
    return json.dumps({"success": True, "data": data})


def test_case_variants_resolve_exact_id_and_totp_by_id():
    chosen = {"id": "exact-id", "name": "CC Genevois"}
    detail = {**chosen, "login": {"username": "synthetic-user", "password": "synthetic-password",
                                "uris": [{"uri": "https://portal.invalid"}]}}
    with mock.patch.object(vault, "get_json", side_effect=[
            response({"data": [{"id": "other-id", "name": "CC genevois"}, chosen]}),
            response(detail)]) as get, mock.patch.object(vault, "get_totp", return_value=None) as totp:
        result = vault.fetch_credentials("http://127.0.0.1:8087", "CC", "Genevois")
    assert result["item_id"] == "exact-id"
    assert result["user"] == "synthetic-user"
    assert result["password"] == "synthetic-password"
    assert result["otp"] is None
    assert get.call_args_list == [
        mock.call("http://127.0.0.1:8087/list/object/items?search=CC%20Genevois"),
        mock.call("http://127.0.0.1:8087/object/item/exact-id")]
    totp.assert_called_once_with("http://127.0.0.1:8087/object/totp/exact-id")


@pytest.mark.parametrize("items", [[], [{"id": "one", "name": "CC genevois"}],
    [{"id": "a", "name": "CC Genevois"}, {"id": "b", "name": "CC Genevois"}],
    [{"id": "../SECRET_TOKEN", "name": "CC Genevois"}]])
def test_missing_ambiguous_or_invalid_id_fails_without_totp_or_secret_output(items):
    with mock.patch.object(vault, "get_json", return_value=response({"data": items})) as get, \
            mock.patch.object(vault, "get_totp") as totp:
        with pytest.raises(vault.VaultAPIError) as error:
            vault.fetch_credentials("http://127.0.0.1:8087", "CC", "Genevois")
    assert "SECRET_TOKEN" not in str(error.value)
    assert "Genevois" not in str(error.value)
    assert get.call_count == 1
    totp.assert_not_called()


def test_changed_item_identity_is_rejected():
    with mock.patch.object(vault, "get_json", side_effect=[
            response({"data": [{"id": "id", "name": "CC Genevois"}]}),
            response({"id": "id", "name": "other"})]), mock.patch.object(vault, "get_totp") as totp:
        with pytest.raises(vault.VaultAPIError):
            vault.fetch_credentials("http://127.0.0.1:8087", "CC", "Genevois")
    totp.assert_not_called()


def test_fresh_totp_uses_exact_id_after_initial_lookup():
    from types import SimpleNamespace
    from billcollector import runner
    bcs = SimpleNamespace(api_url="http://127.0.0.1:8087", vault_item_id="exact-id",
                          vault_item_name="CC Genevois", otp_fallback=None)
    with mock.patch.object(runner, "get_totp", return_value=response({"data":"synthetic-otp"})) as get:
        assert runner._fresh_otp(bcs) == "synthetic-otp"
    get.assert_called_once_with("http://127.0.0.1:8087/object/totp/exact-id")
