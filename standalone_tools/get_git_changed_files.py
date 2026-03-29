import argparse
from git import Repo, Git
from git.exc import InvalidGitRepositoryError, GitCommandError
import os

def get_categorized_changed_files_for_parsing(repo_path, commit_hash):
    """
    Finds and categorizes files changed since a given commit,
    with specific handling for renames and copies based on 100% similarity.

    Args:
        repo_path (str): The path to the Git repository.
        commit_hash (str): The commit hash to compare against.

    Returns:
        dict: A dictionary containing lists of files for each category:
              'added', 'modified', 'deleted', 'copied_exact', and 'renamed_exact'.
              Returns empty lists on error.
    """
    files_by_type = {
        'added': [],
        'modified': [],
        'deleted': [],
        'copied_exact': [],
        'renamed_exact': []
    }
    
    try:
        repo = Repo(repo_path)
        if repo.bare:
            print("This is a bare repository and cannot be checked this way.")
            return files_by_type
            
        head_commit = repo.head.commit
        
        try:
            target_commit = repo.commit(commit_hash)
        except GitCommandError:
            print(f"Error: Commit with hash '{commit_hash}' not found.")
            return files_by_type

        # Use GitPython's Git class to execute the exact command
        git = Git(repo_path)
        diff_output = git.diff_tree(
            '--find-copies-harder',
            '-M100%',
            '-C100%',
            target_commit.hexsha,
            head_commit.hexsha,
            '-r',
            '--abbrev=40',
            '--full-index',
            '--raw',
            '-z',
            '--no-color'
        )

        # Parse the raw diff output
        exact_renamed_paths = set()
        diff_lines = diff_output.split('\0')
        i = 0
        while i < len(diff_lines) - 1:  # -1 because last element may be empty
            line = diff_lines[i]
            if not line:
                i += 1
                continue
            parts = line.split()
            if len(parts) < 5:
                i += 1
                continue
            change_type = parts[4]
            src_path = diff_lines[i + 1]
            dst_path = diff_lines[i + 2] if change_type[0] in ('R', 'C') else src_path

            if change_type.startswith('R'):
                # Handle renames (exact renames with R100)
                files_by_type['renamed_exact'].append({'original': src_path, 'new': dst_path})
                exact_renamed_paths.add(src_path)
                exact_renamed_paths.add(dst_path)
                i += 3
            elif change_type.startswith('C'):
                # Handle copies (exact copies with C100)
                files_by_type['copied_exact'].append({'original': src_path, 'new': dst_path})
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

        return files_by_type
    
    except InvalidGitRepositoryError:
        print(f"Error: The path '{repo_path}' is not a valid Git repository.")
        return files_by_type
    except GitCommandError as e:
        print(f"Git command failed: {e}")
        return files_by_type

def main():
    """
    Parses command-line arguments and calls the main function.
    """
    parser = argparse.ArgumentParser(
        description="Categorize file changes since a specific commit in a Git repository."
    )
    parser.add_argument(
        "repo_path",
        type=str,
        help="The path to the Git repository."
    )
    parser.add_argument(
        "commit_hash",
        type=str,
        help="The commit hash to compare against."
    )
    
    args = parser.parse_args()
    
    repo_directory = args.repo_path
    previous_commit_hash = args.commit_hash
    
    changed_files_map = get_categorized_changed_files_for_parsing(repo_directory, previous_commit_hash)
    
    if changed_files_map:
        print("Files that need to be parsed (Added and Modified):")
        print(f"\nAdded files: {changed_files_map['added']}")
        print(f"\nModified files: {changed_files_map['modified']}")
        
        print("\nFiles that do NOT need to be parsed (Deleted, Exact Copies, Exact Renames):")
        print(f"\nDeleted files: {changed_files_map['deleted']}")
        print("\nCopied files (100% similarity):")
        if changed_files_map['copied_exact']:
            for cp in changed_files_map['copied_exact']:
                print(f" - {cp['original']} -> {cp['new']}")
        else:
            print("  (None)")
        print("\nRenamed files (100% similarity):")
        if changed_files_map['renamed_exact']:
            for rn in changed_files_map['renamed_exact']:
                print(f" - {rn['original']} -> {rn['new']}")
        else:
            print("  (None)")

if __name__ == "__main__":
    main()
