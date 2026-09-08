import os
import sys
import json
import time
import subprocess
import tempfile
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

tokens_file = Path.home() / ".jsdelivr_tokens.json"
github_api = "https://api.github.com"

CREDITS = "Made by @arozely on discord, join https://discord.gg/BpWFG3qsme for updates!"

cdn_domains = {
    "1":  "cdn.jsdelivr.net",
    "2":  "fastly.jsdelivr.net",
    "3":  "gcore.jsdelivr.net",
    "4":  "testingcf.jsdelivr.net",
    "5":  "quantil.jsdelivr.net",
    "6":  "originfastly.jsdelivr.net",
    "7":  "cdn.staticdelivr.com",
    "8":  "jsd.onmicrosoft.cn",
    "9":  "cdn.jsdmirror.com",
    "10": "githubraw.com",
    "11": "cdn.githubraw.com",
    "12": "raw.githack.com",
    "13": "rawcdn.githack.com",
}

svg_only = {"githubraw.com", "cdn.githubraw.com", "raw.githack.com", "rawcdn.githack.com"}
raw_style = {"githubraw.com", "cdn.githubraw.com", "raw.githack.com", "rawcdn.githack.com"}
staticdelivr_style = {"cdn.staticdelivr.com"}

BATCH_SIZE = 2_500_000
CHUNK_SIZE_MB = 500


def load_tokens():
    if not tokens_file.exists():
        sys.exit(1)
    with open(tokens_file) as f:
        data = json.load(f)
    tokens = data.get("tokens", [])
    if not tokens:
        sys.exit(1)
    return tokens


def save_tokens(tokens):
    tokens_file.write_text(json.dumps({"tokens": tokens}, indent=2))


def add_token(token):
    tokens = []
    if tokens_file.exists():
        with open(tokens_file) as f:
            tokens = json.load(f).get("tokens", [])
    if len(tokens) >= 5:
        sys.exit(1)
    if token in tokens:
        sys.exit(1)
    tokens.append(token)
    save_tokens(tokens)


def remove_token(index):
    tokens = load_tokens()
    if index < 1 or index > len(tokens):
        sys.exit(1)
    tokens.pop(index - 1)
    save_tokens(tokens)


def list_tokens():
    tokens = load_tokens()
    for i, t in enumerate(tokens, 1):
        print(f"  {i}. {t[:6]}... ({len(t)})")


def parse_github_url(url):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("bad url")
    owner, repo = parts[0], parts[1]
    file_path = None
    if len(parts) >= 5 and parts[2] == "blob":
        file_path = "/".join(parts[4:])
        if not file_path.endswith(("index.html", "index.svg")):
            raise ValueError("needs index.html or index.svg")
    return owner, repo, file_path


def get_fork(owner, repo, token):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    me = requests.get(f"{github_api}/user", headers=headers)
    me.raise_for_status()
    user = me.json()["login"]
    forks = requests.get(f"{github_api}/repos/{owner}/{repo}/forks", headers=headers).json()
    for fork in forks:
        if fork["owner"]["login"] == user:
            return user, fork["name"]
    resp = requests.post(f"{github_api}/repos/{owner}/{repo}/forks", headers=headers)
    if resp.status_code != 202:
        sys.exit(1)
    fork = resp.json()
    for _ in range(30):
        try:
            check = requests.get(f"{github_api}/repos/{fork['full_name']}", headers=headers)
            if check.status_code == 200:
                break
        except:
            pass
        time.sleep(0.5)
    return fork["owner"]["login"], fork["name"]


def run_cmd(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(1)
    return result.stdout.strip()


def gofile_guest_token():
    resp = requests.post("https://api.gofile.io/accounts", timeout=15)
    data = resp.json()
    if data["status"] == "ok":
        return data["data"]["token"]
    raise RuntimeError(f"couldnt get gofile token: {resp.text[:200]}")


def gofile_server(token):
    resp = requests.get("https://api.gofile.io/servers",
                        headers={"Authorization": f"Bearer {token}"}, timeout=15)
    return resp.json()["data"]["servers"][0]["name"]


def gofile_make_folder(token):
    server = gofile_server(token)
    resp = requests.post(
        f"https://{server}.gofile.io/contents/uploadfile",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("credits.txt", CREDITS.encode(), "text/plain")},
        timeout=60
    )
    data = resp.json()
    if data["status"] != "ok":
        return None, None
    folder_id = data["data"]["parentFolder"]
    page = data["data"]["downloadPage"]
    credits_file_id = data["data"]["id"]
    return folder_id, page, credits_file_id


def gofile_verify_credits(token, credits_file_id):
    try:
        resp = requests.get(
            f"https://api.gofile.io/contents/{credits_file_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        data = resp.json()
        if data["status"] != "ok":
            return False
        name = data["data"].get("name", "")
        return name == "credits.txt"
    except Exception:
        return False


class GofileWriter:
    def __init__(self, folder_id, token):
        self.folder_id = folder_id
        self.token = token
        self.lock = threading.Lock()
        self.chunk_num = 0
        self.current_path = None
        self.current_file = None
        self.bytes_written = 0
        self.threads = []
        self.chunk_limit = CHUNK_SIZE_MB * 1024 * 1024
        self._new_chunk()

    def _new_chunk(self):
        self.chunk_num += 1
        fd, path = tempfile.mkstemp(suffix=f"_c{self.chunk_num}.txt")
        os.close(fd)
        self.current_path = path
        self.current_file = open(path, "w", buffering=8 * 1024 * 1024)
        self.bytes_written = 0

    def _rotate(self):
        self.current_file.flush()
        self.current_file.close()
        path, num, folder_id, token = self.current_path, self.chunk_num, self.folder_id, self.token
        t = threading.Thread(target=self._upload, args=(path, num, folder_id, token), daemon=True)
        t.start()
        self.threads.append(t)
        self._new_chunk()

    def _upload(self, fpath, num, folder_id, token):
        for attempt in range(3):
            try:
                server = gofile_server(token)
                with open(fpath, "rb") as f:
                    resp = requests.post(
                        f"https://{server}.gofile.io/contents/uploadfile",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (f"links_{num}.txt", f, "text/plain")},
                        data={"folderId": folder_id} if folder_id else {},
                        timeout=600
                    )
                if resp.status_code != 200:
                    print(f"chunk {num} gofile returned {resp.status_code}, retrying")
                    time.sleep(3)
                    continue
                txt = resp.text.strip()
                if not txt:
                    print(f"chunk {num} got empty response, retrying")
                    time.sleep(3)
                    continue
                try:
                    data = resp.json()
                except Exception:
                    print(f"chunk {num} gofile sent something weird: {txt[:300]!r}")
                    time.sleep(3)
                    continue
                if data["status"] == "ok":
                    try:
                        Path(fpath).unlink()
                    except:
                        pass
                    return
                print(f"chunk {num} gofile said: {txt[:200]}")
                time.sleep(3)
            except Exception as e:
                print(f"chunk {num} attempt {attempt + 1} failed: {e}")
                time.sleep(3)
        print(f"chunk {num} gave up after 3 tries")
        try:
            Path(fpath).unlink()
        except:
            pass

    def writelines(self, lines):
        with self.lock:
            for line in lines:
                self.current_file.write(line)
                self.bytes_written += len(line)
            if self.bytes_written >= self.chunk_limit:
                self._rotate()

    def finish(self):
        with self.lock:
            if self.bytes_written > 0:
                self._rotate()
        for t in self.threads:
            t.join()


def stream_to_stdin(stdin, head_sha, batch_size, global_offset):
    ts = int(time.time())
    for i in range(batch_size):
        msg = f"c{global_offset + i}\n"
        stdin.write(
            f"commit refs/heads/main\n"
            f"mark :{i + 1}\n"
            f"author Gen <gen@example.com> {ts} +0000\n"
            f"committer Gen <gen@example.com> {ts} +0000\n"
            f"data {len(msg)}\n"
            f"{msg}"
            f"{'from ' + head_sha if i == 0 else 'from :' + str(i)}\n\n"
        )
    stdin.close()


def process_token_streaming(token, owner, repo, file_path, commits_needed,
                             selected_cdns, writer, counter, counter_lock):
    tmp = Path(tempfile.mkdtemp(prefix="jsd_"))
    try:
        fork_owner, fork_repo = get_fork(owner, repo, token)
        remote = f"https://x-access-token:{token}@github.com/{fork_owner}/{fork_repo}.git"
        run_cmd(["git", "clone", "--depth=1", "--single-branch", "--no-tags", remote, str(tmp)])
        run_cmd(["git", "config", "user.email", "gen@example.com"], cwd=tmp)
        run_cmd(["git", "config", "user.name", "Gen"], cwd=tmp)

        cdn_prefixes = {}
        for cdn in selected_cdns:
            if cdn in staticdelivr_style:
                cdn_prefixes[cdn] = f"https://{cdn}/gh/{fork_owner}/{fork_repo}/"
            elif cdn in raw_style:
                cdn_prefixes[cdn] = f"https://{cdn}/{fork_owner}/{fork_repo}/"
            else:
                cdn_prefixes[cdn] = f"https://{cdn}/gh/{fork_owner}/{fork_repo}@"

        done = 0
        while done < commits_needed:
            batch = min(BATCH_SIZE, commits_needed - done)
            prev_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=tmp)

            proc = subprocess.Popen(
                ["git", "fast-import", "--quiet"],
                cwd=tmp,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1024 * 1024
            )
            stdin_thread = threading.Thread(
                target=stream_to_stdin,
                args=(proc.stdin, prev_head, batch, done)
            )
            stdin_thread.start()
            proc.wait()
            stdin_thread.join()

            push_done = threading.Event()
            def do_push():
                run_cmd(["git", "push", "origin", "main", "--force", "--no-verify"], cwd=tmp)
                push_done.set()
            push_thread = threading.Thread(target=do_push)
            push_thread.start()

            buf = []
            rev = subprocess.Popen(
                ["git", "rev-list", "--reverse", f"{prev_head}..HEAD"],
                cwd=tmp,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1024 * 1024
            )
            for line in rev.stdout:
                sha = line.strip()
                if not sha:
                    continue
                for cdn in selected_cdns:
                    buf.append(f"{cdn_prefixes[cdn]}{sha}/{file_path}\n")
                if len(buf) >= 500_000:
                    writer.writelines(buf)
                    buf.clear()
            if buf:
                writer.writelines(buf)
            rev.wait()

            push_thread.join()
            done += batch

            with counter_lock:
                prev_total = counter[0]
                counter[0] += batch
                new_total = counter[0]

            prev_m = prev_total // 1_000_000
            curr_m = new_total // 1_000_000
            for m in range(prev_m + 1, curr_m + 1):
                print(f"{m}M done")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def create_links(number, github_url, cdn_choice):
    tokens = load_tokens()
    owner, repo, file_path = parse_github_url(github_url)

    if not file_path:
        headers = {"Authorization": f"token {tokens[0]}", "Accept": "application/vnd.github.v3+json"}
        contents = requests.get(f"{github_api}/repos/{owner}/{repo}/contents/", headers=headers).json()
        for item in contents:
            if item["type"] == "file" and item["name"] in ("index.html", "index.svg"):
                file_path = item["name"]
                break
        if not file_path:
            sys.exit(1)

    is_svg = file_path.endswith(".svg")

    if cdn_choice == "14":
        if is_svg:
            selected_cdns = list(cdn_domains.values())
        else:
            selected_cdns = [v for v in cdn_domains.values() if v not in svg_only]
            print("html file detected, skipping svg-only cdns")
    elif cdn_choice in cdn_domains:
        chosen = cdn_domains[cdn_choice]
        if not is_svg and chosen in svg_only:
            print(f"{chosen} only works for svg files, your file is html")
            sys.exit(1)
        selected_cdns = [chosen]
    else:
        print("use either 1-13 for an specific cdn and 14 for every cdn bru")
        sys.exit(0)

    print(f"using {len(selected_cdns)} cdn(s)")

    per_token = number // len(tokens)
    remainder = number % len(tokens)
    commit_counts = [per_token + (1 if i < remainder else 0) for i in range(len(tokens))]

    try:
        token = gofile_guest_token()
    except Exception as e:
        print(f"couldnt get gofile token: {e}")
        sys.exit(1)

    folder_id, folder_link, credits_file_id = gofile_make_folder(token)
    if folder_link:
        print(f"gofile: {folder_link}")
    else:
        print("couldnt make a gofile folder, gonna try anyway")

    counter = [0]
    counter_lock = threading.Lock()
    writer = GofileWriter(folder_id, token)

    with ThreadPoolExecutor(max_workers=len(tokens)) as executor:
        futures = [
            executor.submit(
                process_token_streaming,
                t, owner, repo, file_path, cnt,
                selected_cdns, writer, counter, counter_lock
            )
            for t, cnt in zip(tokens, commit_counts)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    writer.finish()
    print("done")


def main():
    if len(sys.argv) < 2:
        print("Heavenlyy")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "token":
        if len(sys.argv) < 3:
            sys.exit(1)
        sub = sys.argv[2].lower()
        if sub == "add":
            token = input().strip()
            if token:
                add_token(token)
        elif sub == "list":
            list_tokens()
        elif sub == "remove":
            if len(sys.argv) < 4:
                sys.exit(1)
            try:
                idx = int(sys.argv[3])
            except ValueError:
                sys.exit(1)
            remove_token(idx)

    elif cmd == "create":
        if len(sys.argv) < 3:
            sys.exit(1)
        try:
            number = int(sys.argv[2])
        except ValueError:
            sys.exit(1)

        github_url = sys.argv[3] if len(sys.argv) > 3 else input().strip()
        cdn_choice = sys.argv[4] if len(sys.argv) > 4 else input().strip()
        if not cdn_choice:
            cdn_choice = "1"

        create_links(number, github_url, cdn_choice)


if __name__ == "__main__":
    main()
