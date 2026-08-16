import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from grok_account_manager import cli
from grok_account_manager.api.routers.register import start_registration
from grok_account_manager.api.schemas import RegisterRequest
from grok_account_manager.api.services.jobs import RegistrationJobManager
from grok_account_manager.api.services.pending import PendingResultStore
from grok_account_manager.mail.cloud_mail import CloudMailSource
from grok_account_manager.mail.sources import build_mailbox_source


class CloudMailFactoryWiringTests(unittest.TestCase):
    def test_factory_builds_cloud_mail_source(self) -> None:
        source = build_mailbox_source(
            email_source="cloud_mail",
            cloud_mail_api_base="https://mail.example.test/api",
            cloud_mail_public_token="public-token",
            cloud_mail_domains="one.example.test,two.example.test",
        )

        self.assertIsInstance(source, CloudMailSource)
        self.assertEqual(source.api_base, "https://mail.example.test")
        self.assertEqual(source.domains, ("one.example.test", "two.example.test"))


class CloudMailApiWiringTests(unittest.TestCase):
    def test_registration_route_forwards_cloud_mail_fields(self) -> None:
        body = RegisterRequest(
            emailSource="cloud_mail",
            cloudMailApiBase="https://mail.example.test/api",
            cloudMailPublicToken="public-token",
            cloudMailLoginEmail="admin@example.test",
            cloudMailLoginPassword="login-password",
            cloudMailDomains="one.example.test\ntwo.example.test",
        )

        with patch(
            "grok_account_manager.api.routers.register.JOB_MANAGER.start",
            return_value={"id": "cloud-mail-job"},
        ) as start:
            result = start_registration(body)

        self.assertEqual(result, {"job": {"id": "cloud-mail-job"}})
        self.assertEqual(start.call_args.kwargs["email_source"], "cloud_mail")
        self.assertEqual(start.call_args.kwargs["cloud_mail_api_base"], body.cloudMailApiBase)
        self.assertEqual(start.call_args.kwargs["cloud_mail_public_token"], body.cloudMailPublicToken)
        self.assertEqual(start.call_args.kwargs["cloud_mail_login_email"], body.cloudMailLoginEmail)
        self.assertEqual(start.call_args.kwargs["cloud_mail_login_password"], body.cloudMailLoginPassword)
        self.assertEqual(start.call_args.kwargs["cloud_mail_domains"], body.cloudMailDomains)


class CloudMailJobWiringTests(unittest.TestCase):
    def test_start_and_retry_preserve_cloud_mail_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = RegistrationJobManager(
                pending_store=PendingResultStore(Path(directory) / "pending.json")
            )
            mail_source = Mock()
            mail_source.name = "cloud_mail"
            mail_source.count = 0
            with (
                patch(
                    "grok_account_manager.api.services.jobs.build_mailbox_source",
                    return_value=mail_source,
                ) as build_source,
                patch(
                    "grok_account_manager.api.services.jobs._build_proxy_pool",
                    return_value=(None, ""),
                ),
                patch("grok_account_manager.api.services.jobs.threading.Thread.start"),
            ):
                snapshot = manager.start(
                    total=1,
                    concurrency=1,
                    oauth_exchange=False,
                    email_source="cloud_mail",
                    cloud_mail_api_base="https://mail.example.test/api",
                    cloud_mail_public_token="public-token",
                    cloud_mail_login_email="admin@example.test",
                    cloud_mail_login_password="login-password",
                    cloud_mail_domains="one.example.test,two.example.test",
                )

            build_source.assert_called_once_with(
                email_source="cloud_mail",
                outlook_data="",
                outlook_file="",
                google_data="",
                google_file="",
                cloud_mail_api_base="https://mail.example.test/api",
                cloud_mail_public_token="public-token",
                cloud_mail_login_email="admin@example.test",
                cloud_mail_login_password="login-password",
                cloud_mail_domains="one.example.test,two.example.test",
            )
            self.assertEqual(snapshot["emailSource"], "cloud_mail")
            snapshot_text = str(snapshot)
            self.assertNotIn("public-token", snapshot_text)
            self.assertNotIn("login-password", snapshot_text)

            with patch.object(manager, "start", return_value={}) as restart:
                manager.retry()

            self.assertEqual(restart.call_args.kwargs["cloud_mail_api_base"], "https://mail.example.test/api")
            self.assertEqual(restart.call_args.kwargs["cloud_mail_public_token"], "public-token")
            self.assertEqual(restart.call_args.kwargs["cloud_mail_login_email"], "admin@example.test")
            self.assertEqual(restart.call_args.kwargs["cloud_mail_login_password"], "login-password")
            self.assertEqual(restart.call_args.kwargs["cloud_mail_domains"], "one.example.test,two.example.test")


class _Provider:
    name = "grok"
    chrome_lang = "zh-CN"
    enable_oauth_exchange = False
    mail_source = None
    retry_browser_callback = None

    def run_round(self, _session):
        return {"email": "created@example.test", "credential": "sso"}


class _Session:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class CloudMailCliWiringTests(unittest.TestCase):
    def test_cli_forwards_cloud_mail_options(self) -> None:
        sink = Mock()
        mail_source = object()
        argv = [
            "grok-account-manager",
            "grok",
            "--count",
            "1",
            "--no-proxy",
            "--email-source",
            "cloud_mail",
            "--cloud-mail-api-base",
            "https://mail.example.test/api",
            "--cloud-mail-public-token",
            "public-token",
            "--cloud-mail-login-email",
            "admin@example.test",
            "--cloud-mail-login-password",
            "login-password",
            "--cloud-mail-domains",
            "one.example.test,two.example.test",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(cli, "ensure_stable_python_runtime"),
            patch.object(cli, "warn_runtime_compatibility"),
            patch.object(cli, "load_dotenv"),
            patch.object(cli, "_build_cli_proxy_pool", return_value=(None, "")),
            patch.object(cli, "get_fixed_egress_proxy", return_value=None),
            patch.object(cli, "build_mailbox_source", return_value=mail_source) as build_source,
            patch.object(cli, "_make_sink", return_value=sink),
            patch.object(cli, "build_chromium_options", return_value=object()),
            patch.object(cli, "DrissionBrowserSession", return_value=_Session()),
            patch.dict(cli.PROVIDERS, {"grok": _Provider}),
        ):
            cli.main()

        build_source.assert_called_once_with(
            email_source="cloud_mail",
            outlook_data="",
            outlook_file="",
            google_data="",
            google_file="",
            cloud_mail_api_base="https://mail.example.test/api",
            cloud_mail_public_token="public-token",
            cloud_mail_login_email="admin@example.test",
            cloud_mail_login_password="login-password",
            cloud_mail_domains="one.example.test,two.example.test",
        )
        self.assertIs(_Provider.mail_source, None)
        sink.push.assert_called_once_with("grok", {"email": "created@example.test", "credential": "sso"})
        sink.flush.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
