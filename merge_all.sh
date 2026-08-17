#!/bin/bash
git config --global merge.conflictstyle diff3
git checkout main
for branch in $(git branch -r | grep -v '\->' | grep -v 'main'); do
    echo "Merging $branch"
    git merge --no-edit -X ours $branch
    if [ $? -ne 0 ]; then
        echo "Conflict in $branch, auto-resolving"
        git diff --name-only --diff-filter=U | xargs -r git checkout --ours
        git add -u
        git commit --no-edit -m "Merge $branch and auto-resolve conflicts"
    fi
done
