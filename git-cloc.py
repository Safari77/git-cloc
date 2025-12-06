#!/bin/python3

"""
GIT CODE OWNERSHIP ANALYZER
---------------------------
Methodology: "Survivor Lines" (Current Ownership)

1. Scope: Analyzes the HEAD of the current branch.
2. Attribution: Uses 'git blame'. Credit is given to the author/email
   of the line as it exists *right now*.
3. Filtering: Uses 'cloc' to strip comments and blank lines.
4. Formatting: Uses 'wcwidth' to correctly align double-width Unicode characters.

Usage:
  python3 git_cloc.py                     # All files, text output
  python3 git_cloc.py --format=json       # JSON output
  python3 git_cloc.py --since="1 year"    # Lines modified in last year
  python3 git_cloc.py --exclude="*test*"  # Exclude patterns
  python3 git_cloc.py src/                # Scan specific directory
"""

"""
     This program is free software: you can redistribute it and/or modify it under the terms of the
     GNU General Public License as published by the Free Software Foundation, either version 3 of
     the License, or (at your option) any later version.

     This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
     without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
     the GNU General Public License for more details.

     You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/lic$
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
from multiprocessing import Pool, cpu_count

# Check for wcwidth for precise unicode alignment
try:
    from wcwidth import wcswidth
except ImportError:
    # Fallback if not installed, though alignment might be slightly off for CJK
    def wcswidth(s): return len(s)

# Default set if no arguments provided
DEFAULT_EXTENSIONS = (
    '.c', '.h', '.cpp', '.hpp', '.rs', '.go', '.py', '.js', '.ts',
    '.java', '.cs', '.pl'
)

# Global variables for worker processes
_worker_git_root = None
_worker_temp_dir = None
_worker_blame_args = None

def run_command(cmd, cwd=None):
    try:
        return subprocess.check_output(cmd, shell=True, cwd=cwd).decode('utf-8', errors='ignore')
    except subprocess.CalledProcessError as e:
        raise e

def get_all_cloc_extensions():
    try:
        out = run_command("cloc --show-ext")
    except Exception:
        return ()

    exts = []
    lines = out.splitlines()
    start_parsing = False
    for line in lines:
        if line.startswith("---------"):
            start_parsing = True
            continue
        if start_parsing:
            parts = line.split()
            if parts:
                ext = parts[0].strip()
                if ext: exts.append('.' + ext)
    return tuple(exts)

def get_visual_width(s):
    return wcswidth(str(s))

def pad_col(text, width):
    text = str(text)
    vis_len = get_visual_width(text)
    padding = max(0, width - vis_len)
    return text + " " * padding

def init_worker(git_root, temp_dir, blame_args):
    global _worker_git_root, _worker_temp_dir, _worker_blame_args
    _worker_git_root = git_root
    _worker_temp_dir = temp_dir
    _worker_blame_args = blame_args

def process_file_task(filepath):
    if not filepath: return {}

    git_root = _worker_git_root
    temp_dir = _worker_temp_dir
    blame_extra_args = _worker_blame_args

    local_author_map = {}

    try:
        quoted_path = shlex.quote(filepath)
        cmd = f"git blame -w --line-porcelain {blame_extra_args} -- {quoted_path}"
        blame_out = run_command(cmd, cwd=git_root)
    except subprocess.CalledProcessError:
        return {}

    lines = blame_out.split('\n')
    file_structure = {}

    current_name = "Unknown"
    current_email = ""
    current_hash = None

    for line in lines:
        if line.startswith("author "):
            current_name = line[7:].strip()
        elif line.startswith("author-mail "):
            current_email = line[12:].strip().replace('<', '').replace('>', '')
        elif line.startswith("\t"):
            if current_name:
                auth_hash = hashlib.sha256(current_name.encode('utf-8')).hexdigest()
                current_hash = auth_hash
                if auth_hash not in local_author_map:
                    local_author_map[auth_hash] = (current_name, current_email)
                content = line[1:]
                if current_hash not in file_structure:
                    file_structure[current_hash] = []
                file_structure[current_hash].append(content)

    for auth_hash, content_lines in file_structure.items():
        safe_filepath = filepath.lstrip(os.sep)
        auth_dir = os.path.join(temp_dir, auth_hash)
        target_path = os.path.join(auth_dir, safe_filepath)

        if not os.path.commonprefix([os.path.abspath(target_path), auth_dir]) == auth_dir:
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
    if total == 0: return ""
    pct = count / total
    total_subblocks = pct * width * 8
    full_blocks = int(total_subblocks // 8)
    remainder = int(total_subblocks % 8)
    blocks = ["", "\u258f", "\u258e", "\u258d", "\u258c", "\u258b", "\u258a", "\u2589"]

    bar_str = "\u2588" * full_blocks
    if remainder > 0:
        bar_str += blocks[remainder]

    if full_blocks == 0 and remainder == 0 and count > 0:
        bar_str = "\u258f"

    current_visual_len = full_blocks + (1 if (remainder > 0 or (count > 0 and full_blocks == 0)) else 0)
    padding = width - current_visual_len
    track = "·" * max(0, padding)

    return f"{bar_str}{track} {pct * 100:.2f}%"

def main():
    parser = argparse.ArgumentParser(description="Compute lines of code per git author using cloc.")
    parser.add_argument('pathspecs', nargs='*', help='Files or dirs to process (default: all)')
    parser.add_argument('--exclude', action='append', help='Glob pattern to exclude (e.g. *.test.js)')
    parser.add_argument('--extensions', help="Comma separated (e.g. 'py,rs') or 'all'")
    parser.add_argument('--since', help='Analyze lines modified since date')
    parser.add_argument('--until', help='Analyze lines modified until date')
    parser.add_argument('--format', choices=['text', 'json', 'csv'], default='text', help='Output format')
    parser.add_argument('--per-file', action='store_true', help="Show stats per file (Text mode only)")
    parser.add_argument('--email', action='store_true', help="Show author email")
    parser.add_argument('--threads', type=int, default=cpu_count(), help='Number of threads (default: CPU count)')
    parser.add_argument('--debug', action='store_true', help="Show debug info")
    parser.add_argument('--verbose', action='store_true', help="Show progress")
    args = parser.parse_args()

    if shutil.which('cloc') is None:
        print("Error: 'cloc' is not installed or not in PATH.")
        sys.exit(1)

    if args.extensions:
        if args.extensions.lower() == 'all':
            active_extensions = get_all_cloc_extensions()
        else:
            parts = args.extensions.split(',')
            cleaned = []
            for p in parts:
                p = p.strip()
                if not p: continue
                if not p.startswith('.'): p = '.' + p
                cleaned.append(p)
            active_extensions = tuple(cleaned)
    else:
        active_extensions = DEFAULT_EXTENSIONS

    try:
        git_root = run_command("git rev-parse --show-toplevel").strip()
    except Exception:
        print("Error: Not a git repository.")
        sys.exit(1)

    os.chdir(git_root)

    if args.verbose: print(f"[*] Searching files in HEAD...")

    base_cmd = ["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"]
    if args.pathspecs:
        base_cmd.append("--")
        base_cmd.extend(args.pathspecs)

    cmd_str = " ".join(shlex.quote(p) for p in base_cmd)

    try:
        files_raw = run_command(cmd_str)
        all_files = files_raw.split('\0')
    except Exception as e:
        print(f"Error listing files: {e}")
        sys.exit(1)

    files_to_process = []
    excludes = args.exclude if args.exclude else []

    for f in all_files:
        if not f: continue
        if not f.endswith(active_extensions):
            continue
        if any(fnmatch.fnmatch(f, pat) for pat in excludes):
            continue
        files_to_process.append(f)

    if not files_to_process:
        print("No files matched criteria.")
        sys.exit(0)

    total_files = len(files_to_process)
    if args.verbose: print(f"[*] Found {total_files} files to process with {args.threads} threads.")

    blame_args_list = []
    if args.since: blame_args_list.append(f"--since={shlex.quote(args.since)}")
    if args.until: blame_args_list.append(f"--until={shlex.quote(args.until)}")
    blame_args_str = " ".join(blame_args_list)

    with tempfile.TemporaryDirectory() as raw_temp_dir:
        temp_dir = os.path.realpath(raw_temp_dir)
        if args.debug: print(f"[DEBUG] Temp dir: {temp_dir}")

        global_author_map = {}
        processed_count = 0

        # Start Multiprocessing
        with Pool(processes=args.threads, initializer=init_worker, initargs=(git_root, temp_dir, blame_args_str)) as pool:

            # Use imap_unordered so we can update progress as each file finishes
            for partial_map in pool.imap_unordered(process_file_task, files_to_process):
                processed_count += 1
                global_author_map.update(partial_map)

                # Update Progress Bar (overwrite line)
                if args.verbose:
                    pct = (processed_count / total_files) * 100
                    # \r moves cursor to start of line, end='' prevents newline
                    print(f"\r[*] Processing: [{processed_count}/{total_files}] {pct:.1f}%", end="", flush=True)

        # Clear the progress line
        if args.verbose: print("")

        if args.verbose: print("[*] Running cloc...")

        cloc_cmd = f"cloc --json --by-file --quiet '{temp_dir}'"
        try:
            cloc_out = run_command(cloc_cmd)
            cloc_data = json.loads(cloc_out)
        except Exception as e:
            print(f"Error running cloc: {e}")
            sys.exit(1)

        results = []
        total_code_lines = 0

        for key, stats in cloc_data.items():
            if key == "header" or key == "SUM": continue

            real_key_path = os.path.realpath(key)
            try:
                rel_path = os.path.relpath(real_key_path, temp_dir)
            except ValueError:
                continue

            parts = rel_path.split(os.sep)
            if len(parts) < 2: continue

            auth_hash = parts[0]
            name_email = global_author_map.get(auth_hash, ("Unknown", ""))

            real_file_path = os.path.join(*parts[1:])
            code_count = stats.get('code', 0)
            total_code_lines += code_count

            results.append({
                'author': name_email[0],
                'email': name_email[1],
                'path': real_file_path,
                'blank': stats.get('blank', 0),
                'comment': stats.get('comment', 0),
                'code': code_count
            })

        if not results:
            if args.format == 'json': print("[]")
            else: print("No results found.")
            sys.exit(0)

        if args.format == 'json':
            print(json.dumps(results, indent=2))

        elif args.format == 'csv':
            writer = csv.writer(sys.stdout)
            header = ['author', 'email', 'path', 'blank', 'comment', 'code']
            writer.writerow(header)
            for r in results:
                writer.writerow([r['author'], r['email'], r['path'], r['blank'], r['comment'], r['code']])

        else:
            if args.per_file:
                results.sort(key=lambda x: (x['author'], x['path']))
                w_auth, w_email, w_path = get_max_lengths(results)

                if args.email:
                    print(f"{pad_col('AUTHOR', w_auth)} {pad_col('EMAIL', w_email)} {pad_col('FILE', w_path)} {'BLANK':>8} {'COMMENT':>8} {'CODE':>8}")
                    print("-" * (w_auth + w_email + w_path + 30))
                    for r in results:
                        print(f"{pad_col(r['author'], w_auth)} {pad_col(r['email'], w_email)} {pad_col(r['path'], w_path)} {r['blank']:>8} {r['comment']:>8} {r['code']:>8}")
                else:
                    print(f"{pad_col('AUTHOR', w_auth)} {pad_col('FILE', w_path)} {'BLANK':>8} {'COMMENT':>8} {'CODE':>8}")
                    print("-" * (w_auth + w_path + 30))
                    for r in results:
                        print(f"{pad_col(r['author'], w_auth)} {pad_col(r['path'], w_path)} {r['blank']:>8} {r['comment']:>8} {r['code']:>8}")
            else:
                agg = {}
                for r in results:
                    a = r['author']
                    if a not in agg:
                        agg[a] = {'email': r['email'], 'blank': 0, 'comment': 0, 'code': 0, 'files': 0}
                    agg[a]['blank'] += r['blank']
                    agg[a]['comment'] += r['comment']
                    agg[a]['code'] += r['code']
                    agg[a]['files'] += 1

                sorted_agg = sorted(agg.items(), key=lambda item: item[1]['code'], reverse=True)

                agg_rows = [{'author': k, 'email': v['email']} for k, v in sorted_agg]
                w_auth, w_email, _ = get_max_lengths(agg_rows)

                if args.email:
                    print(f"{pad_col('AUTHOR', w_auth)} {pad_col('EMAIL', w_email)} {'FILES':>8} {'CODE':>8}  {'DISTRIBUTION':<38}")
                    print("-" * (w_auth + w_email + 65))
                else:
                    print(f"{pad_col('AUTHOR', w_auth)} {'FILES':>8} {'CODE':>8}  {'DISTRIBUTION':<38}")
                    print("-" * (w_auth + 55))

                for auth, data in sorted_agg:
                    bar = draw_bar(data['code'], total_code_lines)
                    if args.email:
                        print(f"{pad_col(auth, w_auth)} {pad_col(data['email'], w_email)} {data['files']:>8} {data['code']:>8}  {bar}")
                    else:
                        print(f"{pad_col(auth, w_auth)} {data['files']:>8} {data['code']:>8}  {bar}")

if __name__ == "__main__":
    main()
