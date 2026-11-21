# Prepare the PostgreSQL push

Two commits separate native SeBS support from AWS evaluation tooling. The agent
has not run Git commands. These lists scope whole files; inspect staged hunks in
shared files before committing. Raw results, generated deployment state/configs,
credentials and caches are excluded. Do not use git add . or git add -A.

From the checkout containing these files, inspect your existing index first:

```bash
git status --short
git diff --cached --name-only
```

If unrelated changes are already staged, resolve that staging separately before
starting. Existing commits on this branch remain unchanged; the commands below
only separate changes that are still uncommitted. If Git reports an unfinished
merge/rebase, finish it before starting this sequence.

Optionally create a new branch (choose an unused name):

```bash
git switch -c sql-postgresql-finalize
```

Stage and review the native integration:

```bash
git add --pathspec-from-file=handoff/POSTGRESQL_SEBS_FILES.txt
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "Complete native PostgreSQL storage and SQL benchmark integration"
```

Stage and review the optional evaluation helpers:

```bash
git add --pathspec-from-file=handoff/POSTGRESQL_EVALUATION_FILES.txt
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "Add PostgreSQL AWS evaluation tools and reproduction guide"
```

If a scope has no changes because its files were already committed, skip that
commit; do not stage unrelated files to make it nonempty. The union list
POSTGRESQL_COMMIT_FILES.txt is for reference, not the two-commit staging sequence.

Finally inspect and push the current branch:

```bash
git status --short
git log -2 --oneline
git push -u origin HEAD
```

HEAD pushes the actual current branch, avoiding the earlier nonexistent-branch
refspec error. A remote rejection still needs an explicit fetch/reconciliation;
do not force-push over teammates' work. This preparation does not inspect Git
status/diffs, create branches, stage files, commit or contact the remote.

The SeBS commit includes framework code, the 720 benchmark, examples, regression
tests, native setup/API documentation, handoff and aggregate evidence. Evaluation
code remains under evaluation/sql; it is not required for native benchmark
invocation. The second commit includes its scripts/tests and separate procedures.

The recorded AWS deployment is deleted. Running the reproducibility guides again
creates a new billable experiment; no cloud setup is needed merely to commit.
