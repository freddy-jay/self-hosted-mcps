import hashlib
import os
import unittest

from mcps import podman

# What base.Containerfile copies into the shared runtime image.
BAKED_IN = ("base.Containerfile", "gateway.js", "package.json", "package-lock.json")

# Change these two together, and only together with the tag in podman.py.
TAG = "localhost/mcps-base:4"
CONTENT_SHA256 = "fc82f514a71e683e08b09e9672d3521627b4733c15b69e8053ac530a3febd280"


def baked_in_digest() -> str:
    digest = hashlib.sha256()
    for name in BAKED_IN:
        # Line endings differ between checkouts; the image content does not.
        text = (podman.RUNTIME / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + text + b"\0")
    return digest.hexdigest()


class RuntimeImageTagTests(unittest.TestCase):
    @unittest.skipIf("MCPS_BASE_IMAGE" in os.environ, "base image overridden")
    def test_runtime_change_requires_a_new_base_image_tag(self) -> None:
        self.assertEqual(
            (podman.BASE_IMAGE, baked_in_digest()),
            (TAG, CONTENT_SHA256),
            "src/mcps/runtime changed: bump the tag in src/mcps/podman.py, then "
            "set TAG and CONTENT_SHA256 here. An unchanged tag makes "
            "`mcps add --force` reuse the old image silently.",
        )


if __name__ == "__main__":
    unittest.main()
