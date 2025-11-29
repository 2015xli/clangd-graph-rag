#!/usr/bin/env python3
"""
This module provides a wrapper around GitPython to identify categorized file changes.
"""

import os
import git
import logging
from typing import Optional

from compilation_parser import CompilationParser # Import CompilationParser

logger = logging.getLogger(__name__)

def get_git_repo(folder: str) -> Optional[git.Repo]:
    """
    Finds the git.Repo object for a given folder path.
    Searches parent directories and ensures the folder is within the repo.
    """
    try:
        repo = git.Repo(folder, search_parent_directories=True)
        # Ensure the provided folder is within the found repository's working tree
        if not os.path.abspath(folder).startswith(os.path.abspath(repo.working_tree_dir)):
            return None
        return repo
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        return None

def resolve_commit_ref_to_hash(repo: git.Repo, ref: str) -> str:
    """
    Resolves a Git reference (tag, branch, hash) to a full commit hash.
    Raises ValueError if the reference is invalid.
    """
    if not ref:
        raise ValueError("Commit reference cannot be empty.")
    try:
        commit = repo.commit(ref)
        return commit.hexsha
    except (git.exc.BadName, git.exc.GitCommandError) as e:
        raise ValueError(f"Could not resolve git reference '{ref}': {e}") from e


class GitManager:
    """Manages Git operations for the graph updater."""

    def __init__(self, repo_path: str):
        """
        Initializes the GitManager.

        Args:
            repo_path (str): The path to the Git repository.
        
        Raises:
            git.InvalidGitRepositoryError: If the path is not a valid Git repository.
        """
        try:
            self.repo_path = repo_path
            self.repo = git.Repo(repo_path)
            self.git = git.Git(repo_path)
            if self.repo.bare:
                raise git.InvalidGitRepositoryError(f"Repository at {repo_path} is a bare repository.")
        except git.InvalidGitRepositoryError as e:
            logger.error(f"Error: The path '{repo_path}' is not a valid Git repository.")
            raise

    def _filter_source_files(self, file_list):
        """Filters a list of file paths for C/C++ source and header files."""
        return [f for f in file_list if f.lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS)]

    def _get_detailed_changed_files(self, old_commit_hash: str, new_commit_hash: str) -> dict:
        """
        Finds and categorizes source files changed between two commits,
        with specific handling for renames and copies based on 100% similarity.
        Returns 5 categories: 'added', 'modified', 'deleted', 'renamed_exact', 'copied_exact'.
        """
        files_by_type = {
            'added': [],
            'modified': [],
            'deleted': [],
            'renamed_exact': [],  # List of {'original': old_path, 'new': new_path}
            'copied_exact': []     # List of {'original': old_path, 'new': new_path}
        }

        try:
            old_commit = self.repo.commit(old_commit_hash)
            new_commit = self.repo.commit(new_commit_hash)

            # Use raw git diff-tree to get precise, machine-readable output
            diff_output = self.git.diff_tree(
                '--find-copies-harder', '-M100%', '-C100%',
                old_commit.hexsha, new_commit.hexsha,
                '-r', '--raw', '-z', '--no-color'
            )

            # Parse the null-delimited raw output
            exact_renamed_paths = set() # To track paths involved in exact renames/copies
            raw_files = diff_output.split('\0')
            i = 0
            while i < len(raw_files) - 1:  # -1 because last element may be empty
                line = raw_files[i]
                if not line:
                    i += 1
                    continue
                parts = line.split()
                if len(parts) < 5:
                    i += 1
                    continue
                change_type = parts[4]
                
                # Determine src_path and dst_path based on change_type
                if change_type.startswith('R') or change_type.startswith('C'):
                    src_path = raw_files[i + 1]
                    dst_path = raw_files[i + 2]
                else:
                    src_path = raw_files[i + 1]
                    dst_path = src_path # For A, D, M, src and dst are the same logical path

                if change_type.startswith('R'):
                    # Handle renames (exact renames with R100)
                    files_by_type['renamed_exact'].append({'original': src_path, 'new': dst_path})
                    exact_renamed_paths.add(src_path)
                    exact_renamed_paths.add(dst_path)
                    i += 3
                elif change_type.startswith('C'):
                    # Handle copies (exact copies with C100)
                    files_by_type['copied_exact'].append({'original': src_path, 'new': dst_path})
                    exact_renamed_paths.add(dst_path) # Only new path is "added"
                    i += 3
                elif change_type == 'A':
                    # Handle added files
                    if dst_path not in exact_renamed_paths:
                        files_by_type['added'].append(dst_path)
                    i += 2
                elif change_type == 'D':
                    # Handle deleted files
                    if src_path not in exact_renamed_paths:
                        files_by_type['deleted'].append(src_path)
                    i += 2
                elif change_type == 'M':
                    # Handle modified files
                    if dst_path not in exact_renamed_paths:
                        files_by_type['modified'].append(dst_path)
                    i += 2
                else:
                    i += 2  # Skip unknown change types

            # Filter all lists for relevant source files
            files_by_type['added'] = self._filter_source_files(files_by_type['added'])
            files_by_type['modified'] = self._filter_source_files(files_by_type['modified'])
            files_by_type['deleted'] = self._filter_source_files(files_by_type['deleted'])
            
            filtered_renamed_exact = []
            for rename_pair in files_by_type['renamed_exact']:
                if rename_pair['original'].lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS) or \
                   rename_pair['new'].lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                    filtered_renamed_exact.append(rename_pair)
            files_by_type['renamed_exact'] = filtered_renamed_exact

            filtered_copied_exact = []
            for copy_pair in files_by_type['copied_exact']:
                if copy_pair['original'].lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS) or \
                   copy_pair['new'].lower().endswith(CompilationParser.ALL_C_CPP_EXTENSIONS):
                    filtered_copied_exact.append(copy_pair)
            files_by_type['copied_exact'] = filtered_copied_exact

            return files_by_type

        except git.exc.GitCommandError as e:
            logger.error(f"Git command failed while diffing commits: {e}")
            return files_by_type

    def get_categorized_changed_files(self, old_commit_hash: str, new_commit_hash: str) -> dict:
        """
        Provides categorized file changes (added, modified, deleted) for the graph updater.
        Treats renamed files as a deletion of the old path and an addition of the new path.
        Treats copied files as an addition of the new path.
        """
        detailed_changes = self._get_detailed_changed_files(old_commit_hash, new_commit_hash)

        # Initialize the final categories for the graph updater
        updater_categories = {
            'added': [],
            'modified': [],
            'deleted': [],
        }

        # Start with genuinely added, modified, deleted files
        updater_categories['added'].extend(detailed_changes['added'])
        updater_categories['modified'].extend(detailed_changes['modified'])
        updater_categories['deleted'].extend(detailed_changes['deleted'])

        # Process renamed files: treat as deleted (original) and added (new)
        for rename_pair in detailed_changes['renamed_exact']:
            # Only add to deleted/added if they are source files (already filtered in _get_detailed_changed_files)
            updater_categories['deleted'].append(rename_pair['original'])
            updater_categories['added'].append(rename_pair['new'])

        # Process copied files: treat as added (new)
        for copy_pair in detailed_changes['copied_exact']:
            # Only add to added if it is a source file (already filtered in _get_detailed_changed_files)
            updater_categories['added'].append(copy_pair['new'])

        # Ensure uniqueness and filter again for safety, though _get_detailed_changed_files should have handled most
        updater_categories['added'] = list(set(updater_categories['added']))
        updater_categories['modified'] = list(set(updater_categories['modified']))
        updater_categories['deleted'] = list(set(updater_categories['deleted']))

        del detailed_changes        
        return updater_categories


    def get_changed_files_abs_path(self, old_commit_hash: str, new_commit_hash: str) -> dict:
        categorized_files = self.get_categorized_changed_files(old_commit_hash, new_commit_hash)
        
        # Convert relative paths to absolute paths
        changed_files_abs_path = {}
        for category in ['added', 'modified', 'deleted']:
            changed_files_abs_path[category] = [
                os.path.join(self.repo_path, file_path)
                for file_path in categorized_files[category]
            ]        

        del categorized_files
        return changed_files_abs_path
    
    def get_head_commit_hash(self) -> str:
        """Returns the hexsha of the current HEAD commit."""
        return self.repo.head.object.hexsha
