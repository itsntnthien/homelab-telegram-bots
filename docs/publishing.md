# Publish a new GitHub repository

The prepared repository is named `homelab-telegram-bots`. This package does not contain a remote URL or deployment secrets. A public repository makes it accessible from a CV; choose its visibility deliberately.

Before the first push:

```bash
python scripts/check.py --require-linux
git status --short
git log -1 --oneline
```

A fresh clone should contain source and examples only. The `.env.example` files are intentionally tracked; actual `.env`, keys, databases, logs, dependencies and backup directories are ignored. The focused checker detects common token/key shapes but cannot prove that every possible secret format is absent.

If using the prepared Git repository, create the GitHub repository and upload its commit:

```bash
gh repo create homelab-telegram-bots --public --source=. --remote=origin --push \
  --description "Two self-hosted Telegram bots for market signals and Linux homelab operations"
```

If starting from the ZIP, first initialize and commit it:

```bash
git init -b main
git add .
git commit -m "feat: package trading and homelab control bots"
```

Then run the same `gh repo create` command, add the topics listed in `portfolio.md`, and wait for the GitHub Actions run before adding a passing-CI badge. Do not claim that CI has run until the actual workflow completes. See `NOTICE.md` before selecting a repository-wide license.
