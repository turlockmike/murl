# Publishing Setup Summary

This document provides a quick overview of the publishing setup that has been implemented for the `murl` project.

## ✅ What Has Been Implemented

### 1. Automated PyPI Publishing
- **Workflow File**: `.github/workflows/publish.yml`
- **Trigger**: Git tags matching `v*.*.*` pattern (e.g., `v0.2.0`)
- **Features**:
  - Automatic version validation (ensures tag matches `pyproject.toml`)
  - Package building and validation with `twine check`
  - Publishing to PyPI using trusted publishing (no token needed!)
  - Fallback to API token if needed

### 2. GitHub Releases
- Automatically creates GitHub releases when tags are pushed
- Includes installation instructions
- Generates release notes from commits

### 3. Homebrew Formula Generation
- Automatically generates a Homebrew formula file
- Downloads package from PyPI and calculates SHA256
- Uploads formula as a workflow artifact (90-day retention)
- Can be manually added to a Homebrew tap

### 4. Documentation
- **PUBLISHING.md**: Complete guide for maintainers on how to publish new versions
- **README.md**: Updated with Homebrew installation instructions and link to publishing docs

## 🔐 Required Setup (One-Time)

### PyPI Trusted Publishing (Recommended)

1. Go to https://pypi.org/manage/account/publishing/
2. Click "Add a new pending publisher"
3. Fill in the form:
   - **PyPI Project Name**: `murl`
   - **Owner**: `turlockmike`
   - **Repository name**: `murl`
   - **Workflow name**: `publish.yml`
   - **Environment name**: (leave empty)
4. Click "Add"

That's it! No secrets or tokens needed. The first release will claim the project name.

### Alternative: API Token Method

If you prefer using an API token instead:

1. Go to https://pypi.org/manage/account/token/
2. Create a new API token (scope it to the `murl` project if it already exists)
3. Go to your GitHub repository settings
4. Navigate to Secrets and variables > Actions
5. Add a new repository secret:
   - **Name**: `PYPI_API_TOKEN`
   - **Value**: (paste your PyPI token)
6. In `.github/workflows/publish.yml`, uncomment line 52:
   ```yaml
   password: ${{ secrets.PYPI_API_TOKEN }}
   ```

## 📦 How to Publish a New Version

### Step 1: Update Version
Edit these two files:

**`pyproject.toml`:**
```toml
version = "0.2.0"  # Change this
```

**`murl/__init__.py`:**
```python
__version__ = "0.2.0"  # Change this
```

### Step 2: Commit Changes
```bash
git add pyproject.toml murl/__init__.py
git commit -m "Bump version to 0.2.0"
git push origin main
```

### Step 3: Create and Push Tag
```bash
git tag v0.2.0
git push origin v0.2.0
```

### Step 4: Watch the Workflow
1. Go to https://github.com/turlockmike/murl/actions
2. Watch the "Publish to PyPI and Homebrew" workflow run
3. Within a few minutes:
   - Package will be published to PyPI
   - GitHub release will be created
   - Homebrew formula will be available as an artifact

## 🍺 Homebrew Setup (Optional)

The workflow generates a Homebrew formula but doesn't automatically publish it. To make it available via Homebrew:

### Option 1: Create a Homebrew Tap
1. Create a new repository: `homebrew-tap`
2. After each release, download the formula artifact from the workflow
3. Add it to your tap repository as `Formula/murl.rb`
4. Users can install with: `brew install turlockmike/tap/murl`

### Option 2: Submit to Homebrew Core
Once the project is stable and popular, you can submit it to Homebrew's main repository.

## 🧪 Testing the Workflow

You can test the workflow without publishing by:

1. Creating a test tag: `git tag v0.1.0-test && git push origin v0.1.0-test`
2. The workflow will run but you can cancel before it publishes
3. Delete the tag: `git push origin --delete v0.1.0-test && git tag -d v0.1.0-test`

## 📊 Version Numbering

Follow [Semantic Versioning](https://semver.org/):
- **MAJOR.MINOR.PATCH** (e.g., `1.2.3`)
- **MAJOR**: Breaking changes (increment when you break backwards compatibility)
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

## 🔍 Troubleshooting

### Version Mismatch Error
If the workflow fails with "Version in pyproject.toml does not match tag version":
- Make sure you updated the version in both `pyproject.toml` and `murl/__init__.py`
- The versions must match the tag (without the `v` prefix)

### PyPI Publishing Fails
- **With Trusted Publishing**: Ensure the pending publisher is configured correctly on PyPI
- **With API Token**: Verify the `PYPI_API_TOKEN` secret is set correctly

### Package Not Found on PyPI
- The workflow waits 2 minutes before generating the Homebrew formula
- If you still have issues, check https://pypi.org/project/murl/ to confirm it's published

## 📝 Files Changed

| File | Purpose |
|------|---------|
| `.github/workflows/publish.yml` | Publishing workflow |
| `PUBLISHING.md` | Detailed publishing guide |
| `README.md` | Updated with Homebrew info |
| `pyproject.toml` | Fixed license classifier |

## ✨ Next Steps

1. **Set up PyPI Trusted Publishing** (see above)
2. **Test the workflow** by publishing version 0.1.0 or 0.2.0
3. **Consider creating a Homebrew tap** if you want Homebrew support
4. **Update PUBLISHING.md** with any project-specific release procedures

## 🆘 Need Help?

Refer to:
- `PUBLISHING.md` for detailed publishing instructions
- GitHub Actions logs for workflow troubleshooting
- PyPI documentation: https://packaging.python.org/
- Homebrew documentation: https://docs.brew.sh/

---

**Security Summary**: CodeQL analysis passed with no security vulnerabilities detected.
