#!/usr/bin/python3

"""
GIT CODE OWNERSHIP ANALYZER
-----------------------------------------------
Methodology: "Survivor Lines" (Current Ownership)

1. Scope: Analyzes a specific commit (default: HEAD).
2. Attribution: Uses 'git blame --porcelain' for optimal speed.
   - Metadata is parsed once per commit (efficient).
   - Content is read directly from the stream.

Caveat: each author's lines are extracted into a separate file before
counting, so tokei loses surrounding block-comment context. A line owned
by author A inside author B's /* ... */ block may be classified as code
instead of comment (or vice versa). Total line attribution is exact;
the blank/comment/code split is approximate for interleaved authorship.

Usage:
  python3 git-cloc.py                       # All files, text output
  python3 git-cloc.py --commit=v2.6.39      # Analyze specific tag
  python3 git-cloc.py --extra-options="-C"  # Detect copies
"""

"""
     This program is free software: you can redistribute it and/or modify it under the terms of the
     GNU General Public License as published by the Free Software Foundation, either version 3 of
     the License, or (at your option) any later version.

     This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
     without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
     the GNU General Public License for more details.

     You should have received a copy of the GNU General Public License along with this program.
     If not, see <https://www.gnu.org/licenses/>.
"""


import subprocess
import os
import shutil
import tempfile
import json
import argparse
import sys
import hashlib
import shlex
import csv
import fnmatch
import email.header
import re
from multiprocessing import Pool, cpu_count

# Check for wcwidth for precise unicode alignment
try:
    from wcwidth import wcswidth
except ImportError:

    def wcswidth(s):
        return len(s)


DEFAULT_EXTENSIONS = (
    '.rs',
    '.c',
    '.h',
    '.cpp',
    '.cc',
    '.hpp',
    '.go',
    '.py',
    '.js',
    '.ts',
    '.java',
    '.cs',
    '.pl',
    '.rb',
    '.php',
    '.sh',
)

# Global variables for worker processes
_worker_git_root = None
_worker_temp_dir = None
_worker_blame_args = None
_worker_commit_ref = None


def run_command(cmd_list, cwd=None, binary=False, check=False):
    """
    Executes command directly without an intermediate shell.
    cmd_list must be a list of strings: ["git", "blame", ...]
    With check=True a failing command raises CalledProcessError instead
    of silently returning an empty result.
    """
    try:
        # shell=False is the default. We execute the binary directly.
        # bufsize increased to optimize stream reading.
        out = subprocess.check_output(
            cmd_list, cwd=cwd, bufsize=1024 * 1024, stderr=subprocess.DEVNULL
        )
        return out if binary else out.decode('utf-8', errors='ignore')
    except subprocess.CalledProcessError:
        if check:
            raise
        return b'' if binary else ''


def decode_author_name(raw_name):
    if '=?' not in raw_name:
        return raw_name
    try:
        parts = email.header.decode_header(raw_name)
        decoded_parts = []
        for content, encoding in parts:
            if isinstance(content, bytes):
                enc = encoding if encoding else 'utf-8'
                decoded_parts.append(content.decode(enc, errors='ignore'))
            else:
                decoded_parts.append(str(content))
        return ''.join(decoded_parts)
    except Exception:
        return raw_name


def get_visual_width(s):
    # wcswidth returns -1 for strings containing non-printable characters;
    # fall back to len() so padding math stays sane.
    s = str(s)
    width = wcswidth(s)
    return width if width >= 0 else len(s)


def pad_col(text, width):
    text = str(text)
    vis_len = get_visual_width(text)
    padding = max(0, width - vis_len)
    return text + ' ' * padding


def init_worker(git_root, temp_dir, blame_args, commit_ref):
    global _worker_git_root, _worker_temp_dir, _worker_blame_args, _worker_commit_ref
    _worker_git_root = git_root
    _worker_temp_dir = temp_dir
    _worker_blame_args = blame_args
    _worker_commit_ref = commit_ref


def process_file_task(filepath):
    """
    Parses 'git blame --porcelain'.
    Format guarantees:
    1. Header line: <sha> <src> <dst> <count>
    2. Optional metadata (author, email, etc.) - only appears once per commit.
    3. Content line: \t<content>
    """
    if not filepath:
        return {}

    git_root = _worker_git_root
    temp_dir = _worker_temp_dir
    blame_extra_args = _worker_blame_args
    commit_ref = _worker_commit_ref

    local_author_map = {}  # Hash -> (Name, Email)
    file_structure = {}  # AuthHash -> [Lines]

    # Cache for commit metadata: CommitHash -> AuthID
    commit_cache = {}

    # Current state
    current_commit_hash = None

    try:
        # Construct command as a LIST. No shell quoting needed.
        # ["git", "blame", "--porcelain", arg1, arg2, commit, "--", path]
        cmd = ['git', 'blame', '--porcelain', '--encoding=utf-8']
        if blame_extra_args:
            cmd.extend(blame_extra_args)
        cmd.append(commit_ref)
        cmd.append('--')
        cmd.append(filepath)

        blame_out = run_command(cmd, cwd=git_root)
    except Exception:
        return {}

    if not blame_out:
        return {}

    # Regex for the header line: 40 hex chars (64 in SHA-256 repos), space, numbers...
    header_pattern = re.compile(r'^([0-9a-f]{40,64}) \d+ \d+')

    lines = blame_out.split('\n')

    # Temporary buffer for metadata if we encounter a new commit
    pending_metadata = {}

    for line in lines:
        if not line:
            continue

        # 1. Content Line (prefixed by TAB)
        if line.startswith('\t'):
            content = line[1:]  # Strip the leading tab

            # Identify Author
            auth_id = commit_cache.get(current_commit_hash)

            # If we missed metadata (rare edge case in git blame output ordering),
            # check if we have pending data to flush
            if not auth_id and current_commit_hash in pending_metadata:
                pm = pending_metadata[current_commit_hash]
                name = pm.get('author', 'Unknown')
                email = pm.get('author-mail', '').replace('<', '').replace('>', '')

                # Hash Name + Email
                identity_str = f'{name} {email}'
                auth_id = hashlib.sha256(identity_str.encode('utf-8')).hexdigest()

                commit_cache[current_commit_hash] = auth_id
                local_author_map[auth_id] = (name, email)
                del pending_metadata[current_commit_hash]

            if not auth_id:
                # Fallback if git blame gave us absolutely nothing for this hash
                auth_id = 'unknown_author'
                if auth_id not in local_author_map:
                    local_author_map[auth_id] = ('Unknown', '')

            if auth_id not in file_structure:
                file_structure[auth_id] = []
            file_structure[auth_id].append(content)
            continue

        # 2. Header Line (SHA start)
        match = header_pattern.match(line)
        if match:
            current_commit_hash = match.group(1)
            # If this is a new commit we haven't seen metadata for, init buffer
            if (
                current_commit_hash not in commit_cache
                and current_commit_hash not in pending_metadata
            ):
                pending_metadata[current_commit_hash] = {}
            continue

        # 3. Metadata Lines
        if line.startswith('author '):
            if current_commit_hash and current_commit_hash in pending_metadata:
                raw_name = line[7:].strip()
                pending_metadata[current_commit_hash]['author'] = decode_author_name(raw_name)

        elif line.startswith('author-mail '):
            if current_commit_hash and current_commit_hash in pending_metadata:
                pending_metadata[current_commit_hash]['author-mail'] = line[12:].strip()

    # Write files for tokei
    for auth_hash, content_lines in file_structure.items():
        safe_filepath = filepath.lstrip('/')
        auth_dir = os.path.join(temp_dir, auth_hash)
        target_path = os.path.abspath(os.path.join(auth_dir, safe_filepath))

        # Component-aware containment check. os.path.commonprefix is
        # character based (/tmp/abcd would pass against /tmp/abc), so
        # compare with a trailing separator instead.
        if not target_path.startswith(auth_dir + os.sep):
            continue

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            with open(target_path, 'w', encoding='utf-8', errors='ignore') as f:
                f.write('\n'.join(content_lines))
        except IOError:
            pass

    return local_author_map


def get_max_lengths(data_rows):
    if not data_rows:
        return 10, 20, 20
    len_auth = max(get_visual_width(r['author']) for r in data_rows)
    len_email = 0
    if 'email' in data_rows[0]:
        len_email = max(get_visual_width(r['email']) for r in data_rows)
    len_path = 0
    if 'path' in data_rows[0]:
        len_path = max(get_visual_width(r['path']) for r in data_rows)
    return max(len_auth, 10), max(len_email, 10), max(len_path, 20)


def draw_bar(count, total, width=30):
    if total == 0:
        return ''
    pct = count / total
    total_subblocks = pct * width * 8
    full_blocks = int(total_subblocks // 8)
    remainder = int(total_subblocks % 8)
    blocks = ['', '\u258f', '\u258e', '\u258d', '\u258c', '\u258b', '\u258a', '\u2589']

    bar_str = '\u2588' * full_blocks
    if remainder > 0:
        bar_str += blocks[remainder]

    if full_blocks == 0 and remainder == 0 and count > 0:
        bar_str = '\u258f'

    current_visual_len = full_blocks + (
        1 if (remainder > 0 or (count > 0 and full_blocks == 0)) else 0
    )
    padding = width - current_visual_len
    track = '\u2e31' * max(0, padding)

    return f'{bar_str}{track} {pct * 100:>6.2f}%'


def main():
    parser = argparse.ArgumentParser(
        description='Compute lines of code per git author using tokei.'
    )
    parser.add_argument('pathspecs', nargs='*', help='Files or dirs to process (default: all)')
    parser.add_argument(
        '--exclude', action='append', help='Glob pattern to exclude (e.g. *.test.js)'
    )
    parser.add_argument(
        '--extensions',
        help="Comma separated (e.g. 'py,rs') or 'all' (no filter; tokei classifies every file)",
    )
    parser.add_argument(
        '--commit', default='HEAD', help='Git commit/tag to analyze (default: HEAD)'
    )
    parser.add_argument('--extra-options', help='Pass extra options to git blame (e.g. "-C -w")')
    parser.add_argument(
        '--format', choices=['text', 'json', 'csv'], default='text', help='Output format'
    )
    parser.add_argument(
        '--per-file', action='store_true', help='Show stats per file (Text mode only)'
    )
    parser.add_argument('--email', action='store_true', help='Show author email')
    parser.add_argument(
        '--threads',
        type=int,
        default=cpu_count(),
        help='Number of worker processes (default: CPU count)',
    )
    parser.add_argument('--debug', action='store_true', help='Show debug info')
    parser.add_argument('--verbose', action='store_true', help='Show progress')
    args = parser.parse_args()
    num_workers = max(1, args.threads)

    if shutil.which('tokei') is None:
        print("Error: 'tokei' is not installed or not in PATH.")
        sys.exit(1)

    if args.extensions:
        if args.extensions.lower() == 'all':
            # No extension filter: blame every tracked file and let tokei
            # decide what it can classify. This also covers files tokei
            # recognizes by full name (e.g. Makefile, Dockerfile), which
            # an extension list could never match.
            active_extensions = None
        else:
            parts = args.extensions.split(',')
            cleaned = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                if not p.startswith('.'):
                    p = '.' + p
                cleaned.append(p)
            active_extensions = tuple(cleaned)
    else:
        active_extensions = DEFAULT_EXTENSIONS

    if active_extensions is not None and not active_extensions:
        print('Error: No valid extensions given.')
        sys.exit(1)

    try:
        git_root = run_command(['git', 'rev-parse', '--show-toplevel'], check=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Not a git repository (or 'git' is not installed).")
        sys.exit(1)

    os.chdir(git_root)

    # Validate the commit early so a typo gives a clear error instead of
    # an empty file list ("No files matched criteria").
    try:
        run_command(
            ['git', 'rev-parse', '--verify', '--quiet', args.commit + '^{commit}'], check=True
        )
    except subprocess.CalledProcessError:
        print(f"Error: Cannot resolve commit '{args.commit}'.")
        sys.exit(1)

    if args.verbose:
        print(f'[*] Searching files in {args.commit}...')

    # Build LIST for ls-tree
    base_cmd = ['git', 'ls-tree', '-r', '-z', '--name-only', args.commit]
    if args.pathspecs:
        base_cmd.append('--')
        base_cmd.extend(args.pathspecs)

    try:
        files_raw = run_command(base_cmd, check=True)
        all_files = files_raw.split('\0')
    except Exception as e:
        print(f'Error listing files: {e}')
        sys.exit(1)

    files_to_process = []
    excludes = args.exclude if args.exclude else []

    for f in all_files:
        if not f:
            continue
        if active_extensions is not None and not f.endswith(active_extensions):
            continue
        if any(fnmatch.fnmatch(f, pat) for pat in excludes):
            continue
        files_to_process.append(f)

    if not files_to_process:
        print('No files matched criteria.')
        sys.exit(0)

    total_files = len(files_to_process)
    if args.verbose:
        print(f'[*] Found {total_files} files to process with {num_workers} workers.')

    # Build LIST for blame arguments
    blame_args_list = []
    if args.extra_options:
        blame_args_list.extend(shlex.split(args.extra_options))

    with tempfile.TemporaryDirectory() as raw_temp_dir:
        temp_dir = os.path.realpath(raw_temp_dir)
        if args.debug:
            print(f'[DEBUG] Temp dir: {temp_dir}')

        global_author_map = {}
        processed_count = 0

        # Pass 'blame_args_list' (which is a LIST) to worker
        with Pool(
            processes=num_workers,
            initializer=init_worker,
            initargs=(git_root, temp_dir, blame_args_list, args.commit),
        ) as pool:
            for partial_map in pool.imap_unordered(process_file_task, files_to_process):
                processed_count += 1
                global_author_map.update(partial_map)
                if args.verbose:
                    pct = (processed_count / total_files) * 100
                    print(
                        f'\r[*] Processing: [{processed_count}/{total_files}] {pct:.1f}%',
                        end='',
                        flush=True,
                    )

        if args.verbose:
            print('\n[*] Running tokei...')

        # Run tokei directly as a LIST.
        # --hidden/--no-ignore: the temp dir is outside any repo, but make
        # sure tokei never skips extracted files due to hidden names or
        # stray ignore files.
        tokei_cmd = ['tokei', '--output', 'json', '--files', '--hidden', '--no-ignore', temp_dir]
        try:
            tokei_out = run_command(tokei_cmd, check=True)
            tokei_data = json.loads(tokei_out)
        except Exception as e:
            print(f'Error running tokei: {e}')
            sys.exit(1)

        results = []
        total_code_lines = 0

        # Tokei JSON: { "<Language>": { "reports": [ { "name": <path>,
        # "stats": { "blanks": N, "code": N, "comments": N, ... } }, ... ],
        # ... }, "Total": {...} }
        for language, lang_data in tokei_data.items():
            if language == 'Total':
                continue
            for report in lang_data.get('reports', []):
                real_key_path = os.path.realpath(report.get('name', ''))
                try:
                    rel_path = os.path.relpath(real_key_path, temp_dir)
                except ValueError:
                    continue

                parts = rel_path.split(os.sep)
                if len(parts) < 2:
                    continue

                auth_hash = parts[0]
                name_email = global_author_map.get(auth_hash, ('Unknown', ''))

                real_file_path = os.path.join(*parts[1:])
                stats = report.get('stats', {})
                code_count = stats.get('code', 0)
                total_code_lines += code_count

                results.append(
                    {
                        'author': name_email[0],
                        'email': name_email[1],
                        'path': real_file_path,
                        'blank': stats.get('blanks', 0),
                        'comment': stats.get('comments', 0),
                        'code': code_count,
                    }
                )

        if not results:
            if args.format == 'json':
                print('[]')
            else:
                print('No results found.')
            sys.exit(0)

        if args.debug and not args.email:
            name_collision_map = {}
            for r in results:
                name = r['author']
                email = r['email']
                if name not in name_collision_map:
                    name_collision_map[name] = set()
                name_collision_map[name].add(email)

            # Check for names with multiple emails
            collisions = {n: emails for n, emails in name_collision_map.items() if len(emails) > 1}

            if collisions:
                print(
                    f'[DEBUG] WARNING: The following authors share the same name but have different emails:'
                )
                for name, emails in collisions.items():
                    print(f'  - {name}: {", ".join(emails)}')
                print(
                    f'[DEBUG] Recommendation: Use a .mailmap file to merge them or use --email to see distinctions.\n'
                )

        if args.format == 'json':
            print(json.dumps(results, indent=2))

        elif args.format == 'csv':
            writer = csv.writer(sys.stdout)
            header = ['author', 'email', 'path', 'blank', 'comment', 'code']
            writer.writerow(header)
            for r in results:
                writer.writerow(
                    [r['author'], r['email'], r['path'], r['blank'], r['comment'], r['code']]
                )

        else:
            if args.per_file:
                results.sort(key=lambda x: (x['author'], x['path']))
                w_auth, w_email, w_path = get_max_lengths(results)

                if args.email:
                    print(
                        f'{pad_col("AUTHOR", w_auth)} {pad_col("EMAIL", w_email)} {pad_col("FILE", w_path)} {"BLANK":>8} {"COMMENT":>8} {"CODE":>8}'
                    )
                    print('-' * (w_auth + w_email + w_path + 29))
                    for r in results:
                        print(
                            f'{pad_col(r["author"], w_auth)} {pad_col(r["email"], w_email)} {pad_col(r["path"], w_path)} {r["blank"]:>8} {r["comment"]:>8} {r["code"]:>8}'
                        )
                else:
                    print(
                        f'{pad_col("AUTHOR", w_auth)} {pad_col("FILE", w_path)} {"BLANK":>8} {"COMMENT":>8} {"CODE":>8}'
                    )
                    print('-' * (w_auth + w_path + 28))
                    for r in results:
                        print(
                            f'{pad_col(r["author"], w_auth)} {pad_col(r["path"], w_path)} {r["blank"]:>8} {r["comment"]:>8} {r["code"]:>8}'
                        )
            else:
                agg = {}
                for r in results:
                    a = r['author']
                    if a not in agg:
                        agg[a] = {
                            'email': r['email'],
                            'blank': 0,
                            'comment': 0,
                            'code': 0,
                            'files': 0,
                        }
                    agg[a]['blank'] += r['blank']
                    agg[a]['comment'] += r['comment']
                    agg[a]['code'] += r['code']
                    agg[a]['files'] += 1

                sorted_agg = sorted(agg.items(), key=lambda item: item[1]['code'], reverse=True)

                agg_rows = [{'author': k, 'email': v['email']} for k, v in sorted_agg]
                w_auth, w_email, _ = get_max_lengths(agg_rows)

                if args.email:
                    print(
                        f'{pad_col("AUTHOR", w_auth)} {pad_col("EMAIL", w_email)} {"FILES":>8} {"CODE":>8}  {"DISTRIBUTION":<38}'
                    )
                    print('-' * (w_auth + w_email + 59))
                else:
                    print(
                        f'{pad_col("AUTHOR", w_auth)} {"FILES":>8} {"CODE":>8}  {"DISTRIBUTION":<38}'
                    )
                    print('-' * (w_auth + 58))

                for auth, data in sorted_agg:
                    bar = draw_bar(data['code'], total_code_lines)
                    if args.email:
                        print(
                            f'{pad_col(auth, w_auth)} {pad_col(data["email"], w_email)} {data["files"]:>8} {data["code"]:>8}  {bar}'
                        )
                    else:
                        print(
                            f'{pad_col(auth, w_auth)} {data["files"]:>8} {data["code"]:>8}  {bar}'
                        )


if __name__ == '__main__':
    main()
