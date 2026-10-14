import argparse
import os
import unittest
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.adminErrors import AdminApiError
from api.services.adminAuthService import AdminAuthService
from scripts import manage_admin


class FakeAdminTable:
    def __init__(self, adminUsers, insertError=None):
        self.adminUsers = adminUsers
        self.insertError = insertError
        self.email = None
        self.insertedRows = []
        self.operation = None

    def select(self, _fields):
        self.operation = "select"
        return self

    def eq(self, _field, email):
        self.email = email
        return self

    def limit(self, _count):
        return self

    def insert(self, row):
        self.operation = "insert"
        self.insertedRows.append(row)
        return self

    def execute(self):
        if self.operation == "insert":
            if self.insertError:
                raise self.insertError
            row = {"id": f"admin-{len(self.adminUsers) + 1}", **self.insertedRows[-1]}
            self.adminUsers.append(row)
            return SimpleNamespace(data=[row])
        return SimpleNamespace(data=[row for row in self.adminUsers if row["email"] == self.email][:1])


class FakeAdminClient:
    def __init__(self, adminUsers, insertError=None):
        self.adminUsers = FakeAdminTable(adminUsers, insertError)

    def table(self, name):
        if name != "admin_users":
            raise AssertionError(f"Unexpected table: {name}")
        return self.adminUsers


def buildFakeAdminClient(adminUsers, insertError=None):
    return FakeAdminClient(adminUsers, insertError)


class AdminProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.client = buildFakeAdminClient(adminUsers=[])
        self.hasher = Mock()
        self.hasher.hash.return_value = "$argon2id$test-hash"
        self.service = AdminAuthService(client=self.client, passwordHasher=self.hasher)

    def test_create_admin_normalizes_and_hashes(self):
        created = self.service.createAdmin(" Admin@Example.COM ", " Admin Name ", "correct horse battery")
        self.assertEqual("admin@example.com", created["email"])
        self.assertEqual("Admin Name", created["name"])
        self.assertNotIn("password_hash", created)
        self.hasher.hash.assert_called_once_with("correct horse battery")
        inserted = self.client.adminUsers.insertedRows[0]
        self.assertEqual("$argon2id$test-hash", inserted["password_hash"])
        self.assertNotIn("correct horse battery", inserted.values())

    def test_create_admin_rejects_duplicate_and_weak_password(self):
        self.service.createAdmin("admin@example.com", "One", "correct horse battery")
        with self.assertRaises(AdminApiError) as duplicate:
            self.service.createAdmin("ADMIN@example.com", "Two", "another correct password")
        self.assertEqual(409, duplicate.exception.statusCode)
        with self.assertRaises(AdminApiError) as weak:
            self.service.createAdmin("two@example.com", "Two", "short")
        self.assertEqual(422, weak.exception.statusCode)

    def test_create_admin_maps_concurrent_duplicate_insert_to_conflict(self):
        client = buildFakeAdminClient([], insertError=RuntimeError("SQLSTATE 23505 duplicate key"))
        service = AdminAuthService(client=client, passwordHasher=self.hasher)
        with self.assertRaises(AdminApiError) as duplicate:
            service.createAdmin("admin@example.com", "Admin", "correct horse battery")
        self.assertEqual(409, duplicate.exception.statusCode)

    @patch("scripts.manage_admin.getpass.getpass", side_effect=["correct horse battery", "correct horse battery"])
    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_add_reads_hidden_confirmation(self, getService, _getpass):
        getService.return_value.createAdmin.return_value = {
            "id": "admin-id", "email": "admin@example.com", "name": "Admin Name"
        }
        exitCode = manage_admin.main(["add", "--email", "admin@example.com", "--name", "Admin Name"])
        self.assertEqual(0, exitCode)
        getService.return_value.createAdmin.assert_called_once()

    def test_cli_parser_does_not_accept_password_argument(self):
        with self.assertRaises(SystemExit):
            manage_admin.main(["add", "--email", "admin@example.com", "--name", "Admin Name", "--password", "secret"])

    def test_cli_help_runs_as_a_script(self):
        result = subprocess.run(
            [sys.executable, "scripts/manage_admin.py", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("--password", result.stdout)


class AdminLifecycleCliTests(unittest.TestCase):
    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_list_prints_accounts(self, getService):
        getService.return_value.listAdmins.return_value = [{
            "id": "admin-1",
            "email": "admin@example.com",
            "name": "Admin Name",
            "is_active": True,
            "last_login_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }]

        exitCode = manage_admin.main(["list"])

        self.assertEqual(0, exitCode)
        getService.return_value.listAdmins.assert_called_once_with()

    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_list_handles_no_accounts(self, getService):
        getService.return_value.listAdmins.return_value = []

        self.assertEqual(0, manage_admin.main(["list"]))

    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_deactivate_disables_account(self, getService):
        getService.return_value.setAdminActive.return_value = {
            "id": "admin-1", "email": "admin@example.com",
            "name": "Admin", "is_active": False, "revokedSessions": 2,
        }

        exitCode = manage_admin.main(["deactivate", "--email", "admin@example.com"])

        self.assertEqual(0, exitCode)
        getService.return_value.setAdminActive.assert_called_once_with(
            "admin@example.com", False
        )

    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_activate_enables_account(self, getService):
        getService.return_value.setAdminActive.return_value = {
            "id": "admin-1", "email": "admin@example.com",
            "name": "Admin", "is_active": True, "revokedSessions": 0,
        }

        exitCode = manage_admin.main(["activate", "--email", "admin@example.com"])

        self.assertEqual(0, exitCode)
        getService.return_value.setAdminActive.assert_called_once_with(
            "admin@example.com", True
        )

    @patch(
        "scripts.manage_admin.getpass.getpass",
        side_effect=["a valid password", "a valid password"],
    )
    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_reset_password_reads_hidden_confirmation(self, getService, _getpass):
        getService.return_value.changeAdminPassword.return_value = {
            "id": "admin-1", "email": "admin@example.com",
            "name": "Admin", "revokedSessions": 1,
        }

        exitCode = manage_admin.main(
            ["reset-password", "--email", "admin@example.com"]
        )

        self.assertEqual(0, exitCode)
        getService.return_value.changeAdminPassword.assert_called_once_with(
            "admin@example.com", "a valid password"
        )

    @patch(
        "scripts.manage_admin.getpass.getpass",
        side_effect=["password one!!", "password two!!"],
    )
    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_reset_password_rejects_mismatch_without_calling_service(
        self, getService, _getpass
    ):
        exitCode = manage_admin.main(
            ["reset-password", "--email", "admin@example.com"]
        )

        self.assertEqual(2, exitCode)
        getService.return_value.changeAdminPassword.assert_not_called()

    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_reports_admin_error_field_details(self, getService):
        getService.return_value.setAdminActive.side_effect = AdminApiError(
            404, "Administrator not found"
        )

        exitCode = manage_admin.main(["deactivate", "--email", "ghost@example.com"])

        self.assertEqual(1, exitCode)

    @patch("scripts.manage_admin.getAdminAuthService")
    def test_cli_reports_validation_errors_per_field(self, getService):
        getService.return_value.changeAdminPassword.side_effect = AdminApiError(
            422, "Validation failed", {"password": "Password must be between 12 and 128 characters"}
        )

        with patch(
            "scripts.manage_admin.getpass.getpass",
            side_effect=["short", "short"],
        ):
            exitCode = manage_admin.main(
                ["reset-password", "--email", "admin@example.com"]
            )

        self.assertEqual(1, exitCode)

    def test_no_subcommand_accepts_a_password_argument(self):
        parser = manage_admin.buildParser()
        subparsersAction = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        self.assertEqual(
            {"add", "list", "deactivate", "activate", "reset-password"},
            set(subparsersAction.choices),
        )
        for name, subparser in subparsersAction.choices.items():
            optionStrings = [
                option
                for action in subparser._actions
                for option in action.option_strings
            ]
            self.assertNotIn("--password", optionStrings, f"{name} accepts --password")

    def test_lifecycle_subcommands_require_email(self):
        for command in ("deactivate", "activate", "reset-password"):
            with self.assertRaises(SystemExit):
                manage_admin.main([command])


if __name__ == "__main__":
    unittest.main()


class AdminCliEnvironmentTests(unittest.TestCase):
    """
    The CLI must reach Supabase when run by hand from a developer machine.

    Nothing else in the codebase calls load_dotenv; the deployed app gets its
    configuration from the container environment. Without the CLI loading .env
    itself, provisioning dies on KeyError: 'SUPABASE_URL'.
    """

    def test_cli_loads_dotenv_before_importing_supabase_client(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "manage_admin.py"
        ).read_text(encoding="utf-8")

        loadCall = source.index("load_dotenv(")
        serviceImport = source.index("from api.services.adminAuthService")

        self.assertLess(
            loadCall,
            serviceImport,
            ".env must be loaded before the Supabase-backed service is imported",
        )

    def test_cli_does_not_override_existing_environment_variables(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "manage_admin.py"
        ).read_text(encoding="utf-8")

        self.assertIn("override=False", source)

    def test_cli_reads_supabase_url_from_dotenv_in_a_clean_process(self):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            source = Path(__file__).resolve().parents[1] / 'scripts/manage_admin.py'
            shutil.copyfile(source, root / 'scripts/manage_admin.py')
            (root / '.env').write_text('SUPABASE_URL=http://localhost:1\nSUPABASE_KEY=test-only\n')
            self._assert_cli_loads_dotenv(root)

    def _assert_cli_loads_dotenv(self, root):
        script = (
            "import os, sys;"
            "sys.argv = ['manage_admin.py', '--help'];"
            "sys.path.insert(0, r'"
            + str(Path(__file__).resolve().parents[1])
            + "');"
            "import runpy;runpy.run_path(r'" + str(root / 'scripts/manage_admin.py') + "');"
            "print('SUPABASE_URL' in os.environ)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            env={
                key: value for key, value in os.environ.items()
                if key not in ("SUPABASE_URL", "SUPABASE_KEY")
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("True", result.stdout)
