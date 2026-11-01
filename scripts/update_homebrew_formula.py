#!/usr/bin/env python3
"""Regenerate Formula/murl.rb for a released mcp-curl version.

Resolves the full dependency set for Python 3.12 on macOS with `uv pip compile`,
then writes one `resource` block per dependency using that package's sdist URL and
sha256 from PyPI. The whole file is rewritten, so resource checksums can never be
clobbered by a blanket search-and-replace.

Usage: update_homebrew_formula.py VERSION OUT_PATH [--verify]
  --verify  download every sdist and check its sha256 before writing
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request

PKG = "mcp-curl"
PYTHON = "3.12"


def pypi_sdist(name, version):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as r:
        data = json.load(r)
    sd = [u for u in data["urls"] if u["packagetype"] == "sdist"]
    if not sd:
        raise SystemExit(f"no sdist on PyPI for {name}=={version}")
    return data["info"]["name"], sd[0]["url"], sd[0]["digests"]["sha256"]


def resolve(version):
    with tempfile.NamedTemporaryFile("w", suffix=".in", delete=False) as f:
        f.write(f"{PKG}=={version}\n")
        req_in = f.name
    out = subprocess.run(
        ["uv", "pip", "compile", req_in, "--python-version", PYTHON,
         "--python-platform", "macos", "--no-header", "--no-annotate", "-q"],
        check=True, capture_output=True, text=True).stdout
    pins = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, ver = line.split("==")
            pins.append((name, ver))
    return pins


def render(version, main, resources):
    _, url, sha = main
    blocks = "\n".join(
        f'  resource "{n}" do\n    url "{u}"\n    sha256 "{s}"\n  end\n' for n, u, s in resources)
    return f'''class Murl < Formula
  include Language::Python::Virtualenv

  desc "MCP Curl - CLI for querying Model Context Protocol servers"
  homepage "https://github.com/turlockmike/murl"
  url "{url}"
  sha256 "{sha}"
  license "MIT"

  depends_on "pkgconf" => :build
  depends_on "rust" => :build
  depends_on "openssl@3"
  depends_on "python@{PYTHON}"

{blocks}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "murl version {version}", shell_output("#{{bin}}/murl --version")
  end
end
'''


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    version, out_path = sys.argv[1], sys.argv[2]
    pins = resolve(version)
    main_pkg, resources = None, []
    for name, ver in pins:
        entry = pypi_sdist(name, ver)
        if name.lower() == PKG:
            main_pkg = entry
        else:
            resources.append(entry)
    if main_pkg is None:
        raise SystemExit(f"{PKG}=={version} missing from resolution")
    if "--verify" in sys.argv:
        for n, u, s in [main_pkg] + resources:
            with urllib.request.urlopen(u, timeout=120) as r:
                if hashlib.sha256(r.read()).hexdigest() != s:
                    raise SystemExit(f"sha256 mismatch for {n}")
    resources.sort(key=lambda r: r[0].lower())
    with open(out_path, "w") as f:
        f.write(render(version, main_pkg, resources))
    print(f"wrote {out_path}: {PKG} {version}, {len(resources)} resources")


if __name__ == "__main__":
    main()
