# Deploying the Streamlit UI

Two supported paths: **Streamlit Community Cloud** (free, hosted) and a **Docker image** (runs
anywhere). Both need the code on GitHub first.

> Heads-up: the Docker image and Streamlit Cloud build were **not verified on the dev machine**
> (no Docker daemon, no GitHub push from here). The manifests are written to spec; the steps below
> are what you run.

---

## 0. One-time prerequisites (git + GitHub)

Git isn't installed in the dev environment, so install it first, then publish the repo.

```bash
# Windows (PowerShell): install git (+ optional GitHub CLI)
winget install Git.Git
winget install GitHub.cli        # optional, for `gh` below

# From the project root:
git init
git add .
git commit -m "Satellite resource optimization simulator"

# Create the GitHub repo and push (either the gh CLI...):
gh repo create satellite-resource-optimizer --public --source=. --push

# ...or manually:
#   create an empty repo on github.com, then:
# git remote add origin https://github.com/<you>/satellite-resource-optimizer.git
# git branch -M main
# git push -u origin main
```

`.gitignore` already excludes `.venv`, caches, and `.env`. `.env.example` is committed; `.env` is not.

---

## A. Streamlit Community Cloud (hosted, free)

Streamlit Cloud installs with **pip** and does not read uv dependency-groups, so deployment uses
[`requirements.txt`](requirements.txt) (it installs the `satsim` package via `.` plus Streamlit).

1. Push the repo to GitHub (section 0).
2. Go to <https://share.streamlit.io> and sign in with GitHub.
3. **Create app → Deploy a public app from GitHub.**
4. Set:
   - **Repository**: `<you>/satellite-resource-optimizer`
   - **Branch**: `main`
   - **Main file path**: `streamlit_app.py`
5. (Optional) **Advanced settings → Python version → 3.12**.
6. **Deploy.** First build takes a few minutes (it compiles/install deps incl. OR-Tools).

Updates redeploy automatically on every push to `main`. App settings/theme come from
[`.streamlit/config.toml`](.streamlit/config.toml).

---

## B. Docker image (self-hosted / any container host)

Build and run locally (needs Docker installed):

```bash
docker build -f Dockerfile.ui -t satsim-ui .
docker run --rm -p 8501:8501 satsim-ui
# open http://localhost:8501

# or, equivalently:
docker compose up --build
```

Deploy that image to any container host — push it to a registry, then run it:

```bash
docker tag satsim-ui <registry>/<you>/satsim-ui:latest
docker push <registry>/<you>/satsim-ui:latest
```

- **Fly.io**: `fly launch --dockerfile Dockerfile.ui` (set internal port 8501).
- **Google Cloud Run**: deploy the pushed image; set the container port to `8501`.
- **Render / Railway / a VM**: run the image and expose port `8501`.

The container already binds `--server.address=0.0.0.0 --server.port=8501`, so it's reachable
outside the container. If a host injects its own `$PORT`, override the entrypoint port accordingly.

---

## Which to pick

- **Just want it online fast, for free** → Streamlit Community Cloud (path A).
- **Need control, private hosting, or to bundle with other services** → Docker image (path B).
