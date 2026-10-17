#!/usr/bin/env bash
# Publish this folder as a PUBLIC GitHub repo. Run in Cloud Shell:  bash push_github.sh
set -euo pipefail
REPO="${1:-fold}"
cd "$(dirname "$0")"
if ! command -v gh >/dev/null; then
  echo "Installing GitHub CLI..."
  (type -p wget >/dev/null || sudo apt-get install wget -y) >/dev/null
  sudo mkdir -p -m 755 /etc/apt/keyrings
  wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install gh -y -qq
fi
gh auth status >/dev/null 2>&1 || gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
git init -q 2>/dev/null || true
git checkout -B main -q
git add -A
git -c user.name="Neeti Singh" -c user.email="$(gh api user --jq '.id')+$(gh api user --jq '.login')@users.noreply.github.com" \
  commit -qm "Fold: compile repeated LLM calls into verified code (AI Builder Cup 2026, Team Origamind)" || true
if gh repo view "$REPO" >/dev/null 2>&1; then
  git remote add origin "https://github.com/$(gh api user --jq .login)/$REPO.git" 2>/dev/null || true
  git push -u origin main
else
  gh repo create "$REPO" --public --source . --remote origin --push \
    --description "Fold: fold repeated Gemini calls into verified, free code. Gemini + Cloud Run + Firestore + Firebase."
fi
echo "Repo: https://github.com/$(gh api user --jq .login)/$REPO"
