"""Common tasks shared between all our forks."""

import os
import sys
import json
import glob
import shlex
import errno
import shutil
import pathlib
import tempfile
import argparse
import subprocess
import urllib.request
from typing import Optional

from collections import namedtuple
from distutils.version import StrictVersion

import github


GitUser = namedtuple('GitUser', ['login', 'email'])
FOG_USER = GitUser('FriendsOfGalaxy', 'FriendsOfGalaxy@gmail.com')
BOT_USER = GitUser('FriendsOfGalaxyBot', 'FriendsOfGalaxy+bot@gmail.com')

RELEASE_NAME_MESSAGE = "Release version {tag}:Version {tag}"
RELEASE_FILE ="current_version.json"
RELEASE_FILE_COMMIT_MESSAGE = "Updated current_version.json"

FOG_BASE = 'master'
FOG_PR_BRANCH = 'autoupdate'
UPSTREAM_REMOTE = 'upstream'
ORIGIN_REMOTE = 'origin'
PATHS_TO_EXCLUDE = ['README.md', '.github/', RELEASE_FILE]


def _run(*args, **kwargs):
    cmd = list(args)
    if len(cmd) == 1:
        cmd = shlex.split(cmd[0])
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", True)
    print('executing', cmd)
    try:
        out = subprocess.run(cmd, **kwargs)
    except subprocess.CalledProcessError as e:
        err_str = f'{e.output}\n{e.stderr}'
        print('><', err_str)
        raise e
    else:
        print('>>', out.stdout)
        return out


class FogConfig:
    FILENAME = '.fog_config.json'

    def __init__(self, content: dict=None):
        if content is not None:
            self._config = content
        else:
            self._config = self.load_local()
        print(f'Config file: {json.dumps(self._config, indent=4)}')

    def load_local(self) -> dict:
        try:
            with open(self.FILENAME, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return dict()

    @property
    def dependencies_dir(self) -> str:
        return self._config.get("dependencies_dir", '.')


class LocalRepo:
    MANIFEST = 'manifest.json'
    REQUIREMENTS = os.path.join('requirements', 'app.txt')
    REQUIREMENTS_ALTERNATIVE = 'requirements.txt'

    def __init__(self, branch=None, check_requirements=True):
        self._manifest_dir = None
        self._manifest = None
        self._config = FogConfig()

        self._user_setup()
        if branch is not None and branch != self.current_branch:
            self._checkout(branch)
        if check_requirements:
            assert self.requirements_path.exists(), f"No requirements file found on {self.current_branch}"

    @staticmethod
    def _user_setup():
        _run(f'git config user.name {BOT_USER.login}')
        _run(f'git config user.email {BOT_USER.email}')

    @staticmethod
    def _checkout(branch):
        try:
            _run(f'git checkout --track {ORIGIN_REMOTE}/{branch}')
        except subprocess.CalledProcessError:  # no such branch on remote
            _run(f'git checkout -b {branch}')
            _run(f'git push -u {ORIGIN_REMOTE} {branch}')

    def _localize_manifest_dir(self):
        """Search for directory where manifest.json is placed starting with cwd"""
        for root, dirs, files in os.walk('.'):
            if self.MANIFEST in files:
                return root
        raise FileNotFoundError('No manifest in local repository')

    def load_manifest(self):
        with open(self.manifest_dir / self.MANIFEST, 'r') as f:
            self._manifest = json.load(f)
        return self._manifest.copy()

    @property
    def current_branch(self):
        proc = _run('git rev-parse --abbrev-ref HEAD')
        return proc.stdout.strip()

    @property
    def manifest_path(self):
        return self.manifest_dir / self.MANIFEST

    @property
    def requirements_path(self):
        req = pathlib.Path(self.REQUIREMENTS)
        if not req.exists():
            req = pathlib.Path(self.REQUIREMENTS_ALTERNATIVE)
        return req

    def get_local_version(self):
        if self._manifest is None:
            self.load_manifest()
        return self._manifest['version']

    @property
    def config(self) -> FogConfig:
        return self._config

    @property
    def manifest_dir(self):
        if self._manifest_dir is None:
            self._manifest_dir = pathlib.Path(self._localize_manifest_dir())
        return self._manifest_dir.resolve()


class FogRepoManager:
    """Will eventually replace CLI Hub tool"""
    FOG_RELEASE = 'fog_release'
    ALLOWED_LICENSES = ['mit', 'gpl-3.0']

    def __init__(self, fog_token, fork_repo):
        self.token = fog_token
        g = github.Github(fog_token)
        self.user = g.get_user()
        self.fork = g.get_repo(fork_repo)
        self.parent = self.fork.parent
        self._release_branch = None

    @property
    def release_branch(self):
        if self._release_branch is not None:
            return self._release_branch
        try:
            return self.parent.get_branch(self.FOG_RELEASE).name
        except github.GithubException as e:
            if e.status == 404:
                return self.parent.default_branch
            raise

    def _iterate_files(self, repo, ref, dir_):
        """BFS walk through parent repo files using github API
        :dir_: str or github.ContentFile.ContentFile
        """
        if isinstance(dir_, github.ContentFile.ContentFile):
            dir_ = dir_.path

        dirs = []
        for it in repo.get_contents(dir_, ref=ref):
            if it.type == 'dir':
                dirs.append(it)
            else:
                yield it
        for d in dirs:
            yield from self._iterate_files(repo, ref, d)

    def get_parent_manifest(self):
        for it in self._iterate_files(self.parent, self.release_branch, '/'):
            if it.name == 'manifest.json':
                print(f'Found manifest.json in location: {it.path}')
                return json.loads(it.decoded_content)
        raise RuntimeError('manifest.json not found in parent repository!')

    def get_parent_config(self) -> Optional[FogConfig]:
        try:
            config_file = self.parent.get_contents(FogConfig.FILENAME)
        except github.UnknownObjectException:
            return None
        else:
            return FogConfig(json.loads(config_file.decoded_content))

    def get_autoupdate_pr(self):
        pulls = self.fork.get_pulls(state='open', base=FOG_BASE, head=FOG_PR_BRANCH)
        assert pulls.totalCount <= 1
        if pulls.totalCount == 0:
            return None
        return pulls[0]

    def create_or_update_pr(self, version):
        title = f"Version {version}"
        pr = self.get_autoupdate_pr()

        if pr is not None:
            print(f'updating pull-request title version to {version}')
            pr.edit(title=f'Version {version}')
        else:
            print(f'creating pull-request from version {version}')
            pr = self.fork.create_pull(
                title=title,
                body="Sync with the original repository",
                base=FOG_BASE,
                head=FOG_PR_BRANCH
            )
            pr.set_labels('autoupdate')

    def assign_review(self):
        """Currently only to FoG"""
        reviewers = [self.user.login]
        pr = self.get_autoupdate_pr()
        pr.create_review_request(reviewers)

    def get_parent_license(self) -> github.License.License:
        try:
            lic = self.parent.get_license().license
        except github.UnknownObjectException as e:
            raise ValueError(f'Error while getting license: {e}')
        if lic.key not in self.ALLOWED_LICENSES:
            raise ValueError(f'{lic} license is not supported.')
        return lic

    def remove_fork_ref(self, ref, ignore_fail=False) -> None:
        """ref in form of head/<branch_name> or tags/<tag>"""
        try:
            git_ref = self.fork.get_git_ref(ref)
        except github.UnknownObjectException as e:
            if not ignore_fail:
                print(f'ignoring get_git_ref error: {e}')
                raise
        else:
            git_ref.delete()
    
    def release(self, tag: str, *asset_paths: pathlib.Path):
        name, message = RELEASE_NAME_MESSAGE.format(tag=tag).split(':')
        release = self.fork.create_git_release(
            tag=tag,
            name=name,
            message=message,
            draft=True,
            target_commitish=FOG_BASE  # lightweigth tag is created from here
        )
        try:
            for asset in asset_paths:
                release.upload_asset(str(asset))
            release.update_release(name=name, message=message, draft=False)
        except Exception as e:
            print(f'Failed to finalize release with uploaded assets: {repr(e)}.\nRemoving release...')
            release.delete_release()
        else:
            print(f'Release for {tag} sucessfully created with assets')
    
    def get_latest_release(self) -> github.GitRelease.GitRelease:
        return self.fork.get_latest_release()

    def send_repository_dispatch(self, event_type):
        url = f'https://api.github.com/repos/{self.fork.full_name}/dispatches'
        body = {
            "event_type": event_type
        }
        headers = {
            "Authorization": "token " + self.token,
            "Accept": "application/vnd.github.everest-preview+json, application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }
        jsondata = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, jsondata, headers)
        urllib.request.urlopen(req)


def _remove_items(paths):
    """Silently removes files or whole dir trees."""
    print('removing paths:', paths)
    for reserved_path in paths:
        try:
            try:
                os.remove(reserved_path)
            except IsADirectoryError:
                shutil.rmtree(reserved_path)
        except OSError as e:
            if e.errno != errno.ENOENT:  # file not exists
                raise


def sync(api) -> bool:
    """
    Checks if there is new version (in manifest) on upstream versus current master.
    If so, synchronize upstream changes to ORIGIN_REMOTE/FOG_PR_BRANCH
    Returns True if new update were pushed succesfully
    """
    # verify license
    api.get_parent_license()
    # verify upstream version
    upstream_ver = api.get_parent_manifest()['version']
    strict_upstream_ver = StrictVersion(upstream_ver)

    _run(f'git remote set-url {ORIGIN_REMOTE} https://{FOG_USER.login}:{api.token}@github.com/{api.fork.full_name}.git')
    _run(f'git remote add {UPSTREAM_REMOTE} {api.parent.clone_url}')

    # Comparing master version with upstream
    initial_commit = False
    local_repo = LocalRepo(branch=FOG_BASE, check_requirements=False)
    try:
        master_version = StrictVersion(local_repo.get_local_version())
    except FileNotFoundError:
        print('No local version - assuming it is initial PR. Going on.')
        initial_commit = True
    else:
        if strict_upstream_ver <= master_version:
            msg = f'== No new version to be sync to. Upstream: {upstream_ver}, fork on branch {local_repo.current_branch}: {master_version}'
            print(msg)
            return False

    # prevents dealing with already updated FOG_PR_BRANCH in case PR was closed
    if api.get_autoupdate_pr() is None:
        print(f'silently removing {FOG_PR_BRANCH} branch because PR is not open')
        api.remove_fork_ref(f'heads/{FOG_PR_BRANCH}', ignore_fail=True)

    # switching to autoupdate branch
    local_repo = LocalRepo(branch=FOG_PR_BRANCH, check_requirements=False)

    _run(f'git fetch {UPSTREAM_REMOTE}')

    print('removing reserved files')
    _remove_items(PATHS_TO_EXCLUDE)

    print(f'merging latest release from {UPSTREAM_REMOTE}/{api.release_branch}')
    unrelated_history = "--allow-unrelated-histories" if initial_commit else ''
    try:
        _run(f'git merge {unrelated_history} --no-commit --no-ff -s recursive -Xtheirs {UPSTREAM_REMOTE}/{api.release_branch}')
    except subprocess.CalledProcessError as e:
        _run(f'git status')
        if "CONFLICT" in e.output:  # case where file is renamed/deleted
            _run(f'git checkout --theirs ./*')
            _run(f'git add .')
        else:
            raise

    print('checkout reserved files')
    for path in PATHS_TO_EXCLUDE:
        try:
            _run(f'git checkout {ORIGIN_REMOTE}/{FOG_BASE} -- {path}')
        except subprocess.CalledProcessError:
            print(f'Warning: Cannot checkout {path} from remote {FOG_BASE}')

    upstream_config = api.get_parent_config()
    if upstream_config is not None and upstream_config.dependencies_dir != '.':
        deps = upstream_config.dependencies_dir
        print(f'Unstaging dependencies directory "{deps}" if present')
        _run(f'git reset {deps}', check=False)
    else:
        print(f'No dependencies_dir found in upstream config. Proceeding')

    print('commit and push if any changes')
    if _run('git diff-index --quiet --cached HEAD', check=False).returncode == 1:
        _run(f'git commit -m "Merge upstream"')
    else:
        print('No changes found. Ending')
        return False

    _run(f'git push {ORIGIN_REMOTE} {FOG_PR_BRANCH}')

    api.create_or_update_pr(upstream_ver)
    try:
        api.assign_review()
    finally:
        return True


def build(output, user_repo_name):
    local_repo = LocalRepo()
    src = local_repo.manifest_dir.resolve()

    outpath = pathlib.Path(output).resolve()
    try:
        outpath.relative_to(src)
    except ValueError:
        pass
    else:
        raise RuntimeError("dist (output) cannot be part of src")

    if os.path.exists(output):
        shutil.rmtree(output)

    print(f'copy integration code ignoring {RELEASE_FILE}, tests and all hidden files')
    to_ignore = shutil.ignore_patterns(RELEASE_FILE, '.*', 'test_*.py', '*_test.py', '*.pyc')
    shutil.copytree(src, output, ignore=to_ignore)

    if sys.platform == "win32":
        pip_platform = "win32"
    elif sys.platform == "darwin":
        pip_platform = "macosx_10_13_x86_64"
    pip_target = (outpath / local_repo.config.dependencies_dir).as_posix()

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        _run(f'pip-compile {local_repo.requirements_path.as_posix()} --output-file=-', stdout=tmp, stderr=subprocess.PIPE, capture_output=False)
        _run('pip', 'install',
            '-r', tmp.name,
            '--platform', pip_platform,
            '--target', pip_target,
            '--python-version', '37',
            '--no-compile',
            '--no-deps'
        )
    os.unlink(tmp.name)

    print('clean up dist directories')
    for dir_ in glob.glob(f"{output}/*.dist-info"):
        shutil.rmtree(dir_)
    for test in glob.glob(f"{output}/**/test_*.py", recursive=True):
        os.remove(test)

    print('add update_url entry in manifest')
    manifest = local_repo.load_manifest()
    manifest['update_url'] = f'https://raw.githubusercontent.com/{user_repo_name}/{FOG_BASE}/{RELEASE_FILE}'
    with open(outpath / 'manifest.json', 'w') as f:
        json.dump(manifest, f, indent=4)


def release(build_dir, api: FogRepoManager):
    """Zips dirs given in build_dir and upload them with newly created github release
    build_dir should contain asset for windows and/or macos.
    Asset names should start with 'windows' or 'macos' (case insensitive)
    """

    asset_dirs = os.listdir(build_dir)
    print('asset_dirs', asset_dirs)
    if not asset_dirs:
        raise RuntimeError(f'No assets found in {build_dir}')

    zip_assets_dir = os.path.join('..', 'assets')
    print(f"Clearing content of {zip_assets_dir}")
    if os.path.exists(zip_assets_dir):
        shutil.rmtree(zip_assets_dir)
    os.makedirs(zip_assets_dir)

    print(f"Zipping artifacts to {zip_assets_dir}")
    zip_names = {'windows', 'macos'}
    for zip_name in zip_names:
        for asset_dir in asset_dirs:
            if asset_dir.lower().startswith(zip_name):
                src = os.path.join(build_dir, asset_dir)
                asset = os.path.join(zip_assets_dir, zip_name)
                shutil.make_archive(asset, 'zip', root_dir=src, base_dir='.')
                break
        else:
            RuntimeError(f'No asset for {zip_name}!')

    asset_paths = [
        pathlib.Path(zip_assets_dir).absolute() / filename
        for filename in os.listdir(zip_assets_dir)
    ]
    version_tag = LocalRepo().get_local_version()

    print(f'Creating tag {version_tag} and releasing on github with assets: {asset_paths}')
    api.release(version_tag, *asset_paths)


def update_release_file(api):
    release = api.get_latest_release()
    version_tag = release.tag_name

    local_tag = LocalRepo().get_local_version()
    assert version_tag == local_tag, f"remote tag '{version_tag}' does not match with the local one '{local_tag}'"
    
    data = {
        "tag_name": version_tag,
        "assets": [
            {k: asset[k] for k in ('browser_download_url', 'name')}
            for asset in release.raw_data['assets']
        ]
    }
    with open(RELEASE_FILE, 'w') as f:
        json.dump(data, f, indent=4)

    LocalRepo()
    _run(f'git remote set-url {ORIGIN_REMOTE} https://{FOG_USER.login}:{api.token}@github.com/{api.fork.full_name}.git')

    _run(f'git add {RELEASE_FILE}')
    _run(f'git commit -m "{RELEASE_FILE_COMMIT_MESSAGE}"')
    _run(f'git push {ORIGIN_REMOTE} HEAD:{FOG_BASE}')


class ExpandPath(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
        expanded = os.path.expanduser(values)
        setattr(namespace, self.dest, expanded)


def main():
    current_dir = pathlib.Path(os.getcwd()).name
    default_repo = f'{FOG_USER.login}/{current_dir}'

    parser = argparse.ArgumentParser()
    parser.add_argument('task', choices=['sync', 'build', 'release', 'update_release_file'])
    parser.add_argument('--dir', required=sys.argv[1] in ['build', 'release'], help='build directory', action=ExpandPath)
    parser.add_argument('--token', default=os.environ.get('GITHUB_TOKEN'), help='github token with repo access')
    parser.add_argument('--repo', default=default_repo, help='github_user/repository_name')
    args = parser.parse_args()

    if args.task == 'build':
        assert args.token is None
        build(args.dir, args.repo)
        return

    if not args.token:
        raise RuntimeError('Github token not found. Have you set it in secrets?')
    man = FogRepoManager(args.token, args.repo)

    if args.task == 'sync':
        if sync(man):
            # Workaround for not working pull_request on forks: https://github.community/t5/GitHub-Actions/Github-Workflow-not-running-from-pull-request-from-forked/m-p/33484/highlight/true#M1524
            man.send_repository_dispatch('validation')
    elif args.task == 'release':
        release(args.dir, man)
    elif args.task == 'update_release_file':
        update_release_file(man)
    else:
        raise RuntimeError(f'unknown command {args.task}')


if __name__ == "__main__":
    main()