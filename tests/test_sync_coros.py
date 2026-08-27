import os
import sys
import types
import unittest
from unittest import mock


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda *_args, **_kwargs: {}
sys.modules.setdefault("yaml", yaml_stub)

requests_stub = types.ModuleType("requests")


class _RequestException(Exception):
    pass


class _HTTPError(_RequestException):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


requests_stub.RequestException = _RequestException
requests_stub.HTTPError = _HTTPError
requests_stub.Session = object
sys.modules.setdefault("requests", requests_stub)

import sync_coros  # noqa: E402


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sync_coros.requests.HTTPError("request failed", response=self)

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class SyncCorosTests(unittest.TestCase):
    def test_login_hashes_password_and_uses_region_host(self) -> None:
        session = _Session(
            [
                _Response(
                    {
                        "result": "0000",
                        "data": {"accessToken": "token", "userId": "42"},
                    }
                )
            ]
        )

        credentials = sync_coros._login(session, "runner@example.com", "secret", "eu")

        self.assertEqual(credentials, {"access_token": "token", "user_id": "42"})
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://teameuapi.coros.com/account/login")
        self.assertEqual(
            kwargs["json"]["pwd"],
            "5ebe2294ecd0e0f08eab7690d2a6ee69",
        )
        self.assertNotEqual(kwargs["json"]["pwd"], "secret")

    def test_fetch_page_passes_dates_and_access_token(self) -> None:
        session = _Session(
            [
                _Response(
                    {
                        "result": "0000",
                        "data": {"dataList": [], "totalPage": 0},
                    }
                )
            ]
        )

        result = sync_coros._fetch_page(
            session,
            "token",
            "us",
            page=2,
            size=500,
            start_day="2026-01-02",
            end_day="2026-02-03",
        )

        self.assertEqual(result["dataList"], [])
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://teamapi.coros.com/activity/query")
        self.assertEqual(kwargs["headers"], {"accessToken": "token"})
        self.assertEqual(
            kwargs["params"],
            {
                "modeList": "",
                "pageNumber": 2,
                "size": 200,
                "startDay": "20260102",
                "endDay": "20260203",
            },
        )

    def test_normalize_activity_maps_coros_fields(self) -> None:
        normalized = sync_coros._normalize_activity(
            {
                "labelId": "activity-1",
                "startTime": "2026-08-27 07:30:00",
                "sportType": 102,
                "name": "Morning trail",
                "distance": 12500,
                "totalTime": 4500,
                "totalAscent": 620,
            }
        )

        self.assertEqual(normalized["id"], "activity-1")
        self.assertEqual(normalized["type"], "TrailRun")
        self.assertEqual(normalized["distance"], 12500)
        self.assertEqual(normalized["moving_time"], 4500)
        self.assertEqual(normalized["total_elevation_gain"], 620)
        self.assertEqual(normalized["provider"], "coros")

    def test_sync_requires_credentials(self) -> None:
        with (
            mock.patch("sync_coros.load_config", return_value={"coros": {}}),
            self.assertRaisesRegex(RuntimeError, "coros.email and coros.password"),
        ):
            sync_coros.sync_coros(dry_run=True, prune_deleted=False)

    def test_region_rejects_unknown_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported COROS region"):
            sync_coros._base_url("au")

    def test_sync_reuses_configured_access_token_without_login(self) -> None:
        page_result = {
            "fetched": 0,
            "new_or_updated": 0,
            "activity_ids": [],
            "next_page": 1,
            "exhausted": True,
        }
        config = {
            "coros": {
                "email": "runner@example.com",
                "password": "secret",
                "access_token": "cached-token",
                "region": "eu",
            },
            "sync": {"recent_days": 7},
        }
        with (
            mock.patch("sync_coros.load_config", return_value=config),
            mock.patch("sync_coros.requests.Session", return_value=object(), create=True),
            mock.patch("sync_coros._sync_pages", return_value=page_result),
            mock.patch("sync_coros._login") as login_mock,
            mock.patch("sync_coros._save_token_cache") as save_mock,
            mock.patch("sync_coros.ensure_dir"),
        ):
            summary = sync_coros.sync_coros(dry_run=True, prune_deleted=False)

        self.assertFalse(summary["login_performed"])
        login_mock.assert_not_called()
        save_mock.assert_not_called()

    def test_sync_relogs_only_when_cached_token_is_rejected(self) -> None:
        page_result = {
            "fetched": 0,
            "new_or_updated": 0,
            "activity_ids": [],
            "next_page": 1,
            "exhausted": True,
        }
        config = {
            "coros": {
                "email": "runner@example.com",
                "password": "secret",
                "access_token": "expired-token",
                "region": "eu",
            },
            "sync": {"recent_days": 7},
        }
        with (
            mock.patch("sync_coros.load_config", return_value=config),
            mock.patch("sync_coros.requests.Session", return_value=object(), create=True),
            mock.patch(
                "sync_coros._sync_pages",
                side_effect=[RuntimeError("invalid access token"), page_result, page_result],
            ),
            mock.patch(
                "sync_coros._login",
                return_value={"access_token": "new-token", "user_id": "42"},
            ) as login_mock,
            mock.patch("sync_coros._save_token_cache") as save_mock,
            mock.patch("sync_coros.ensure_dir"),
        ):
            summary = sync_coros.sync_coros(dry_run=True, prune_deleted=False)

        self.assertTrue(summary["login_performed"])
        login_mock.assert_called_once()
        save_mock.assert_called_once_with("new-token")


if __name__ == "__main__":
    unittest.main()
