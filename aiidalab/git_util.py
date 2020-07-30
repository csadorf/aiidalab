# -*- coding: utf-8 -*-
"""Utility module for git-managed AiiDA lab apps."""
import re
from enum import Enum
from pathlib import Path
from subprocess import run
from dataclasses import dataclass

from dulwich.repo import Repo
from dulwich.porcelain import status, branch_list
from dulwich.config import parse_submodules, ConfigFile


class BranchTrackingStatus(Enum):
    """Descripe the tracking status of a branch."""
    BEHIND = -1
    EQUAL = 0
    AHEAD = 1
    DIVERGED = 2


class GitManagedAppRepo(Repo):
    """Utility class to simplify management of git-based apps."""

    def list_branches(self):
        """List all repository branches."""
        return branch_list(self)

    def branch(self):
        """Return the current branch.

        Raises RuntimeError if the repository is in a detached HEAD state.
        """
        branches = self._get_branch_for_ref(b'HEAD')
        if branches:
            return branches[0]
        raise RuntimeError("In detached HEAD state.")

    def get_tracked_branch(self, branch=None):
        """Return the tracked branch for a given branch or None if the branch is not tracking."""
        if branch is None:
            branch = self.branch()

        cfg = self.get_config()
        try:
            remote = cfg[(b'branch', branch)][b'remote']
            merge = cfg[(b'branch', branch)][b'merge']
            pattern = rb'refs\/heads'
            remote_ref = b'refs/remotes/' + remote + re.sub(pattern, b'', merge)
            return remote_ref
        except KeyError:
            return None

    def submodules(self):

        @dataclass
        class GitSubmodule:
            dirty: bool
            commit: str
            path: Path
            version: str

        proc = run(['git', 'submodule', 'status'], check=True, capture_output=True, encoding='utf-8', cwd=self.path)
        for line in proc.stdout.splitlines():
            status_commit, path, version = line.split()
            dirty = status_commit[0] != ' '
            commit = status_commit[1:]
            assert version.startswith('(') and version.endswith(')')
            yield GitSubmodule(dirty, commit, path, version[1:-1])
        return

        gitmodules = ConfigFile.from_path(Path(self.path).joinpath('.gitmodules'))
        for module in parse_submodules(gitmodules):
            yield GitSubmodule(*module)

    def dirty(self):
        """Check if there are likely local user modifications to the app repository."""
        status_ = status(self)
        any_staged = any(len(files) > 0 for files in status_.staged.values())
        for submodule in self.submodules():
            print(submodule)
        clean_submodules = (sm for sm in self.submodules() if sm.version != '(null)')
        sm_paths_ignore = (sm.path.encode() for sm in clean_submodules)
        unstaged = set(status_.unstaged).difference(sm_paths_ignore)
        return any_staged or any(unstaged)

    def update_available(self):
        """Check whether there non-pulled commits on the tracked branch."""
        return self.get_branch_tracking_status(self.branch()) is BranchTrackingStatus.BEHIND

    def get_branch_tracking_status(self, branch):
        """Return the tracking status of branch."""
        tracked_branch = self.get_tracked_branch(branch)
        if tracked_branch:
            ref = b'refs/heads/' + branch

            # Check if local branch points to same commit as tracked branch:
            if self.refs[ref] == self.refs[tracked_branch]:
                return BranchTrackingStatus.EQUAL

            # Check if local branch is behind the tracked branch:
            for commit in self.get_walker(self.refs[tracked_branch]):
                if commit.commit.id == self.refs[ref]:
                    return BranchTrackingStatus.BEHIND

            # Check if local branch is ahead of tracked branch:
            for commit in self.get_walker(self.refs[ref]):
                if commit.commit.id == self.refs[tracked_branch]:
                    return BranchTrackingStatus.AHEAD

            return BranchTrackingStatus.DIVERGED

        return None

    def _get_branch_for_ref(self, ref):
        """Get the branch name for a given reference."""
        pattern = rb'refs\/heads\/'
        return [re.sub(pattern, b'', ref) for ref in self.refs.follow(ref)[0] if re.match(pattern, ref)]
