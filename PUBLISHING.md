# Publishing Guide

This document describes how to publish new versions of `murl` to PyPI and generate Homebrew formulas.

## Prerequisites

### PyPI Publishing

The project uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) for secure, tokenless publishing. You need to configure this once in your PyPI account:

1. Go to https://pypi.org/manage/account/publishing/
2. Add a new pending publisher with these details:
   - **PyPI Project Name**: `murl`
   - **Owner**: `turlockmike`
   - **Repository name**: `murl`
   - **Workflow name**: `publish.yml`
   - **Environment name**: (leave empty)

**Alternative: Using API Token**

If you prefer to use an API token instead of trusted publishing:

1. Generate an API token at https://pypi.org/manage/account/token/
2. Add it as a repository secret named `PYPI_API_TOKEN`
3. Uncomment the `password` line in `.github/workflows/publish.yml`:
   ```yaml
   password: ${{ secrets.PYPI_API_TOKEN }}
   ```

### Homebrew Publishing (Optional)

The workflow automatically generates a Homebrew formula and uploads it as an artifact. To publish to a Homebrew tap:

1. Create a tap repository (e.g., `homebrew-tap`)
2. Add the generated formula to the tap
3. Users can then install with: `brew install turlockmike/tap/murl`

For automated tap updates, you can extend the workflow with a step that commits the formula to your tap repository.

## Publishing a New Version

### 1. Update Version

Update the version in two places:

**`pyproject.toml`:**
```toml
[project]
version = "0.2.0"  # Update this
```

**`murl/__init__.py`:**
```python
__version__ = "0.2.0"  # Update this
```

### 2. Commit Version Changes

```bash
git add pyproject.toml murl/__init__.py
git commit -m "Bump version to 0.2.0"
git push origin main
```

### 3. Create and Push Tag

```bash
git tag v0.2.0
git push origin v0.2.0
```

This will trigger the publishing workflow automatically.

### 4. Monitor Workflow

1. Go to https://github.com/turlockmike/murl/actions
2. Watch the "Publish to PyPI and Homebrew" workflow
3. The workflow will:
   - Verify the version matches the tag
   - Build the Python package
   - Publish to PyPI
   - Create a GitHub Release
   - Generate a Homebrew formula

### 5. Download Homebrew Formula

After the workflow completes:

1. Go to the workflow run page
2. Download the "homebrew-formula" artifact
3. Use it to update your Homebrew tap (if you have one)

## Troubleshooting

### Version Mismatch Error

If the workflow fails with a version mismatch:

```
Error: Version in pyproject.toml (0.1.0) does not match tag version (0.2.0)
```

Make sure you updated the version in `pyproject.toml` before creating the tag.

### PyPI Publishing Fails

**If using Trusted Publishing:**
- Ensure the pending publisher is configured correctly on PyPI
- The first publish will claim the project name

**If using API Token:**
- Verify the `PYPI_API_TOKEN` secret is set correctly
- Check that the token has permission to publish to the `murl` project

### Package Not Available on PyPI

The workflow waits 2 minutes before generating the Homebrew formula to ensure the package is available. If you still encounter issues:

1. Check PyPI: https://pypi.org/project/murl/
2. Manually verify the package is published
3. Re-run the "update-homebrew" job if needed

## Version Numbering

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR.MINOR.PATCH** (e.g., `1.2.3`)
- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

Examples:
- `v0.1.0` → `v0.1.1` (bug fix)
- `v0.1.1` → `v0.2.0` (new feature)
- `v0.2.0` → `v1.0.0` (stable release / breaking changes)

## Manual Publishing (Emergency)

If the automated workflow fails, you can publish manually:

### PyPI

```bash
# Install dependencies
pip install build twine

# Build package
python -m build

# Check package
twine check dist/*

# Upload to PyPI
twine upload dist/*
```

### Homebrew Formula

```bash
# Download source from PyPI
VERSION=0.2.0
curl -L -o murl-${VERSION}.tar.gz \
  https://files.pythonhosted.org/packages/source/m/murl/murl-${VERSION}.tar.gz

# Calculate SHA256
sha256sum murl-${VERSION}.tar.gz

# Update the formula with the new version and SHA256
```
