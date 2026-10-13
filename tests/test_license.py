"""Each package ships the repository's license: `ui/` and `discover/` keep exact copies."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LicenseTests(unittest.TestCase):
    def test_every_package_carries_the_same_license_and_notice(self):
        for name in ("LICENSE", "NOTICE"):
            for package in ("ui", "discover"):
                self.assertEqual((ROOT / package / name).read_bytes(), (ROOT / name).read_bytes(),
                                 f"{package}/{name} differs from the root copy")

    def test_the_license_is_apache_2(self):
        self.assertIn("Apache License\n                           Version 2.0, January 2004",
                      (ROOT / "LICENSE").read_text())
        for pyproject in ("pyproject.toml", "ui/pyproject.toml", "discover/pyproject.toml"):
            self.assertIn('license = "Apache-2.0"', (ROOT / pyproject).read_text(), pyproject)


if __name__ == "__main__":
    unittest.main()
