"""Regression tests for what the supervisor is willing to install.

Phase 11 declared a bare `httpx2` in [project].dependencies. Nothing in the repository
imported it, it was not the canonical HTTP client, and it was unpinned — so acceptance and
any production install resolved to whatever the index served under that name. It was
installed anyway: `product_dependencies` validated that entries were well-formed
requirement strings and that they were not pip options, and nothing more.

The reviewer caught it, twice, and only because it read the manifest. Provisioning is the
control that should have refused it. Two rules now apply before pip is invoked:

1. every requirement must constrain its upper end, so a manifest that passed review once
   cannot install different code later;
2. direct URL references are refused outright, because they name an arbitrary location
   rather than a distribution the index resolves.

Name vetting is deliberately not attempted here. A static allowlist would be brittle across
34 phases that legitimately add dependencies, and a wrong allowlist fails closed on honest
work. Bounding the version is the control that would have caught the actual regression.
"""
import json
import unittest

from buildctl.supervisor.gates import ProvisioningError, product_dependencies


def manifest(*deps: str) -> bytes:
    return ('[project]\nname="p"\nversion="0"\ndependencies='
            + json.dumps(list(deps)) + '\n').encode()


class UpperBoundIsRequiredTests(unittest.TestCase):
    def test_the_reported_bare_name_is_refused(self):
        """The exact Phase 11 regression."""
        with self.assertRaises(ProvisioningError) as caught:
            product_dependencies(manifest("httpx2"))
        self.assertIn("not upper-bounded", str(caught.exception))
        self.assertIn("httpx2", str(caught.exception))

    def test_a_lower_bound_alone_is_not_enough(self):
        """`httpx>=0.27` still resolves to whatever the index serves tomorrow."""
        with self.assertRaises(ProvisioningError):
            product_dependencies(manifest("httpx>=0.27"))

    def test_an_exclusion_alone_is_not_enough(self):
        with self.assertRaises(ProvisioningError):
            product_dependencies(manifest("httpx!=0.28"))

    def test_the_error_names_the_distribution_and_suggests_a_bound(self):
        with self.assertRaises(ProvisioningError) as caught:
            product_dependencies(manifest("structlog"))
        message = str(caught.exception)
        self.assertIn("structlog", message)
        self.assertIn("structlog>=X,<Y", message)

    def test_one_unbounded_entry_fails_the_whole_manifest(self):
        with self.assertRaises(ProvisioningError):
            product_dependencies(manifest("pydantic>=2.8,<3", "httpx2"))


class LegitimateFormsAreAcceptedTests(unittest.TestCase):
    def test_every_dependency_the_product_actually_ships_is_accepted(self):
        """The Phase 11 manifest, minus the finding, must still provision."""
        shipped = ("fastapi>=0.115,<1", "pydantic>=2.8,<3", "pydantic-settings>=2.4,<3",
                   "sqlalchemy>=2.0,<3", "alembic>=1.13,<2", "psycopg[binary]>=3.2,<4",
                   "structlog>=24.4,<26", "opentelemetry-api>=1.27,<2",
                   "opentelemetry-sdk>=1.27,<2", "uvicorn[standard]>=0.30,<1")
        self.assertEqual(product_dependencies(manifest(*shipped)), shipped)

    def test_each_upper_bound_operator_is_recognised(self):
        for dep in ("pydantic<3", "pydantic<=3", "pydantic==2.13.4",
                    "pydantic===2.13.4", "pydantic~=2.8"):
            self.assertEqual(product_dependencies(manifest(dep)), (dep,), dep)

    def test_extras_do_not_confuse_the_parser(self):
        for dep in ("psycopg[binary]>=3.2,<4", "uvicorn[standard]>=0.30,<1"):
            self.assertEqual(product_dependencies(manifest(dep)), (dep,), dep)

    def test_whitespace_in_the_specifier_is_tolerated(self):
        dep = "httpx >= 0.27 , < 0.29"
        self.assertEqual(product_dependencies(manifest(dep)), (dep,))

    def test_an_environment_marker_does_not_supply_the_bound(self):
        """A `<` inside a marker must not be mistaken for a version bound."""
        self.assertEqual(product_dependencies(manifest('tomli<3; python_version<"3.11"')),
                         ('tomli<3; python_version<"3.11"',))
        with self.assertRaises(ProvisioningError):
            product_dependencies(manifest('tomli; python_version<"3.11"'))


class DirectReferencesAreRefusedTests(unittest.TestCase):
    def test_a_url_requirement_is_refused(self):
        with self.assertRaises(ProvisioningError) as caught:
            product_dependencies(manifest("evil @ https://example.invalid/evil.whl"))
        self.assertIn("direct URL reference", str(caught.exception))

    def test_a_local_path_reference_is_refused(self):
        with self.assertRaises(ProvisioningError):
            product_dependencies(manifest("evil @ file:///tmp/evil.whl"))

    def test_a_malformed_requirement_is_refused(self):
        for bad in ("!!!", "==1.0", "[extras]>=1,<2"):
            with self.assertRaises(ProvisioningError, msg=bad):
                product_dependencies(manifest(bad))


class ExistingProtectionsAreIntactTests(unittest.TestCase):
    def test_pip_option_injection_is_still_refused(self):
        for hostile in ("--index-url=http://evil.invalid/simple", "-e /tmp/x",
                        "--target=/usr/local/lib"):
            with self.assertRaises(ProvisioningError) as caught:
                product_dependencies(manifest(hostile))
            self.assertIn("may not be a pip option", str(caught.exception))

    def test_a_manifest_without_dependencies_still_yields_nothing(self):
        self.assertEqual(product_dependencies(b'[project]\nname="p"\nversion="0"\n'), ())

    def test_invalid_toml_and_missing_project_table_still_fail(self):
        with self.assertRaisesRegex(ProvisioningError, "not valid TOML"):
            product_dependencies(b"[project\n")
        with self.assertRaisesRegex(ProvisioningError, r"no \[project\] table"):
            product_dependencies(b"[tool.other]\nx=1\n")

    def test_non_string_entries_still_fail(self):
        with self.assertRaisesRegex(ProvisioningError, "non-empty strings"):
            product_dependencies(b'[project]\nname="p"\nversion="0"\ndependencies=[{a=1}]\n')
