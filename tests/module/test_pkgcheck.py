from functools import partial
from io import StringIO
import os
import shlex
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from pkgcore import const as pkgcore_const
from pkgcore.ebuild import restricts, atom
from pkgcore.ebuild.repository import UnconfiguredTree
from pkgcore.restrictions import packages
import pytest
from snakeoil.contexts import chdir
from snakeoil.fileutils import touch
from snakeoil.formatters import PlainTextFormatter
from snakeoil.osutils import pjoin

from pkgcheck import base, checks, const, reporters,  __title__ as project
from pkgcheck.checks.profiles import ProfileWarning
from pkgcheck.scripts import run, pkgcheck

from .misc import fakeconfig, fakerepo, tool


def test_script_run(capsys):
    """Test regular code path for running scripts."""
    script = partial(run, project)

    with patch(f'{project}.scripts.import_module') as import_module:
        import_module.side_effect = ImportError("baz module doesn't exist")

        # default error path when script import fails
        with patch('sys.argv', [project]):
            with pytest.raises(SystemExit) as excinfo:
                script()
            assert excinfo.value.code == 1
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert len(err) == 3
            assert err[0] == "Failed importing: baz module doesn't exist!"
            assert err[1].startswith(f"Verify that {project} and its deps")
            assert err[2] == "Add --debug to the commandline for a traceback."

        # running with --debug should raise an ImportError when there are issues
        with patch('sys.argv', [project, '--debug']):
            with pytest.raises(ImportError):
                script()
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert len(err) == 2
            assert err[0] == "Failed importing: baz module doesn't exist!"
            assert err[1].startswith(f"Verify that {project} and its deps")

        import_module.reset_mock()


class TestPkgcheckScanParseArgs(object):

    @pytest.fixture(autouse=True)
    def _setup(self, tool):
        self.tool = tool
        self.args = ['scan']

    def test_skipped_checks(self):
        options, _func = self.tool.parse_args(self.args)
        assert options.enabled_checks
        # some checks should always be skipped by default
        assert set(options.enabled_checks) != set(const.CHECKS.values())

    def test_enabled_check(self):
        options, _func = self.tool.parse_args(self.args + ['-c', 'PkgDirCheck'])
        assert options.enabled_checks == [checks.pkgdir.PkgDirCheck]

    def test_disabled_check(self):
        options, _func = self.tool.parse_args(self.args)
        assert checks.pkgdir.PkgDirCheck in options.enabled_checks
        options, _func = self.tool.parse_args(self.args + ['-c=-PkgDirCheck'])
        assert options.enabled_checks
        assert checks.pkgdir.PkgDirCheck not in options.enabled_checks

    def test_targets(self):
        options, _func = self.tool.parse_args(self.args + ['dev-util/foo'])
        assert list(options.limiters) == [atom.atom('dev-util/foo')]

    def test_stdin_targets(self):
        with patch('sys.stdin', StringIO('dev-util/foo')):
            options, _func = self.tool.parse_args(self.args + ['-'])
            assert list(options.limiters) == [atom.atom('dev-util/foo')]

    def test_invalid_targets(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            options, _func = self.tool.parse_args(self.args + ['dev-util/f$o'])
            # force target parsing
            list(options.limiters)
        assert excinfo.value.code == 2
        out, err = capsys.readouterr()
        err = err.strip()
        assert err == "pkgcheck scan: error: invalid package atom: 'dev-util/f$o'"

    def test_selected_targets(self, fakerepo):
        # selected repo
        options, _func = self.tool.parse_args(self.args + ['-r', 'stubrepo'])
        assert options.target_repo.repo_id == 'stubrepo'
        assert options.limiters == [packages.AlwaysTrue]

        # dir path
        options, _func = self.tool.parse_args(self.args + [fakerepo])
        assert options.target_repo.repo_id == 'fakerepo'
        assert options.limiters == [packages.AlwaysTrue]

        # file path
        os.makedirs(pjoin(fakerepo, 'dev-util', 'foo'))
        ebuild_path = pjoin(fakerepo, 'dev-util', 'foo', 'foo-0.ebuild')
        touch(ebuild_path)
        options, _func = self.tool.parse_args(self.args + [ebuild_path])
        restrictions = [
            restricts.RepositoryDep('fakerepo'),
            restricts.CategoryDep('dev-util'),
            restricts.PackageDep('foo'),
            restricts.VersionMatch('=', '0'),
        ]
        assert list(options.limiters) == [packages.AndRestriction(*restrictions)]
        assert options.target_repo.repo_id == 'fakerepo'

        # cwd path in unconfigured repo
        with chdir(pjoin(fakerepo, 'dev-util', 'foo')):
            options, _func = self.tool.parse_args(self.args)
            assert options.target_repo.repo_id == 'fakerepo'
            restrictions = [
                restricts.RepositoryDep('fakerepo'),
                restricts.CategoryDep('dev-util'),
                restricts.PackageDep('foo'),
            ]
            assert list(options.limiters) == [packages.AndRestriction(*restrictions)]

        # cwd path in configured repo
        stubrepo = pjoin(pkgcore_const.DATA_PATH, 'stubrepo')
        with chdir(stubrepo):
            options, _func = self.tool.parse_args(self.args)
            assert options.target_repo.repo_id == 'stubrepo'
            assert list(options.limiters) == [
                packages.AndRestriction(restricts.RepositoryDep('stubrepo'))]

    def test_unknown_repo(self, capsys):
        for opt in ('-r', '--repo'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith(
                "pkgcheck scan: error: argument -r/--repo: couldn't find repo 'foo'")

    def test_unknown_reporter(self, capsys):
        for opt in ('-R', '--reporter'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith(
                "pkgcheck scan: error: no reporter matches 'foo'")

    def test_unknown_scope(self, capsys):
        for opt in ('-s', '--scopes'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith("pkgcheck scan: error: unknown scope: 'foo'")

    def test_unknown_check(self, capsys):
        for opt in ('-c', '--checks'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith("pkgcheck scan: error: unknown check: 'foo'")

    def test_unknown_keyword(self, capsys):
        for opt in ('-k', '--keywords'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt, 'foo'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith("pkgcheck scan: error: unknown keyword: 'foo'")

    def test_selected_keywords(self):
        for opt in ('-k', '--keywords'):
            options, _func = self.tool.parse_args(self.args + [opt, 'InvalidPN'])
            result_cls = next(v for k, v in const.KEYWORDS.items() if k == 'InvalidPN')
            assert options.enabled_keywords == [result_cls]
            check = next(x for x in const.CHECKS.values() if result_cls in x.known_results)
            assert options.enabled_checks == [check]

    def test_missing_scope(self, capsys):
        for opt in ('-s', '--scopes'):
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + [opt])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[0] == (
                'pkgcheck scan: error: argument -s/--scopes: expected one argument')

    def test_no_active_checks(self, capsys):
            args = self.args + ['-c', 'UnusedInMastersCheck']
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(args)
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith("pkgcheck scan: error: no active checks")


class TestPkgcheck(object):

    script = partial(run, project)

    def test_version(self, capsys):
        with patch('sys.argv', [project, '--version']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            assert excinfo.value.code == 0
            out, err = capsys.readouterr()
            assert out.startswith(project)


class TestPkgcheckScan(object):

    script = partial(run, project)

    @pytest.fixture(autouse=True)
    def _setup(self, fakeconfig, tmp_path):
        self.args = [project, '--config', fakeconfig, 'scan']
        self.cache_dir = str(tmp_path)
        self.testdir = os.path.dirname(os.path.dirname(__file__))

    def test_empty_repo(self, capsys, tmp_path):
        # no reports should be generated since the default repo is empty
        with patch('sys.argv', self.args), \
                patch('pkgcheck.base.CACHE_DIR', self.cache_dir):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            assert excinfo.value.code == 0
            out, err = capsys.readouterr()
            assert out == err == ''

    results = []
    for name, cls in const.CHECKS.items():
        for result in cls.known_results:
            results.append((name, result))

    def test_pkgcheck_test_repos(self):
        """Make sure the test repos are up to date check/result naming wise."""
        # grab custom targets
        custom_targets = set()
        for root, _dirs, files in os.walk(pjoin(self.testdir, 'data')):
            for f in files:
                if f == 'target':
                    with open(pjoin(root, f)) as target:
                        custom_targets.add(target.read().strip())

        # all pkgs that aren't custom targets must be check/keyword
        for repo_dir in os.listdir(pjoin(self.testdir, 'repos')):
            repo = UnconfiguredTree(pjoin(self.testdir, 'repos', repo_dir))
            results = set((name, cls.__name__) for name, cls in self.results)
            for cat, pkgs in sorted(repo.packages.items()):
                for pkg in sorted(pkgs):
                    if f'{cat}/{pkg}' not in custom_targets:
                        assert (cat, pkg) in results

    def test_pkgcheck_test_data(self):
        """Make sure the test data is up to date check/result naming wise."""
        for check in os.listdir(pjoin(self.testdir, 'data')):
            assert check in const.CHECKS
            for keyword in os.listdir(pjoin(self.testdir, f'data/{check}')):
                assert keyword in const.KEYWORDS

    @pytest.mark.parametrize('check, result', results)
    def test_pkgcheck_scan(self, check, result, capsys, tmp_path):
        # run pkgcheck against test pkgs in bundled repo, verifying result output
        keyword = result.__name__
        expected_path = pjoin(self.testdir, f'data/{check}/{keyword}/expected')
        if not os.path.exists(expected_path):
            pytest.skip('expected test data not available')

        # determine what repo the test requires
        try:
            repo = open(pjoin(self.testdir, f'data/{check}/{keyword}/repo')).read()
        except FileNotFoundError:
            repo = 'standalone'
        repo_dir = pjoin(self.testdir, 'repos', repo)

        # determine what test target to use
        try:
            target = open(pjoin(self.testdir, f'data/{check}/{keyword}/target'))
            args = shlex.split(target.read())
        except FileNotFoundError:
            if result.threshold in (base.package_feed, base.versioned_feed):
                args = [f'{check}/{keyword}']
            elif result.threshold in base.category_feed:
                args = [f'{keyword}/*']
            elif result.threshold in base.repository_feed:
                args = ['-k', keyword]

        with patch('sys.argv', self.args + ['-r', repo_dir] + args), \
                patch('pkgcheck.base.CACHE_DIR', self.cache_dir):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert err == ''
            assert excinfo.value.code == 0
            with open(expected_path) as expected:
                assert out == expected.read()

    @pytest.mark.parametrize('check, result', results)
    def test_pkgcheck_scan_fix(self, check, result, capsys, tmp_path):
        # apply fixes to pkgs, verifying the related results are fixed
        keyword = result.__name__

        def _patch(fix):
            with open(fix) as f:
                p = subprocess.run(['patch', '-p1'], cwd=fixed_repo, stdin=f)
                p.check_returncode()

        def _script(fix):
            p = subprocess.run([fix], cwd=fixed_repo)
            p.check_returncode()

        fix_map = {
            'fix.patch': _patch,
            'fix.sh': _script,
        }

        for f, func in fix_map.items():
            fix = pjoin(self.testdir, f'data/{check}/{keyword}/{f}')
            if os.path.exists(fix):
                break
        else:
            pytest.skip('fix not available')

        # determine what repo the test requires
        try:
            repo = open(pjoin(self.testdir, f'data/{check}/{keyword}/repo')).read()
        except FileNotFoundError:
            repo = 'standalone'

        repo_dir = pjoin(self.testdir, 'repos', repo)

        if result.threshold in (base.package_feed, base.versioned_feed):
            args = [f'{check}/{keyword}']
        elif result.threshold in base.category_feed:
            args = [f'{keyword}/*']
        elif result.threshold in base.repository_feed:
            args = ['-k', keyword]

        # apply a fix if one exists and make sure the related result doesn't appear
        fixed_repo = str(tmp_path / f'fixed-{repo}')
        shutil.copytree(repo_dir, fixed_repo)
        func(fix)
        with patch('sys.argv', self.args + ['-r', fixed_repo] + args), \
                patch('pkgcheck.base.CACHE_DIR', self.cache_dir):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert err == ''
            assert out == ''
            assert excinfo.value.code == 0
        shutil.rmtree(fixed_repo)


class TestPkgcheckShow(object):

    script = partial(run, project)

    @pytest.fixture(autouse=True)
    def _setup(self, fakeconfig):
        self.args = [project, '--config', fakeconfig, 'show']

    def test_show_no_args(self, capsys):
        # defaults to outputting keywords list if no option is passed
        with patch('sys.argv', self.args):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            regular_output = out
            assert out == sorted(const.KEYWORDS.keys())
            assert excinfo.value.code == 0

    def test_show_keywords(self, capsys):
        # regular mode
        with patch('sys.argv', self.args + ['--keywords']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            regular_output = out
            assert out == sorted(const.KEYWORDS.keys())
            assert excinfo.value.code == 0

        # verbose mode
        with patch('sys.argv', self.args + ['--keywords', '-v']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            verbose_output = out
            assert excinfo.value.code == 0

        # verbose output shows much more info
        assert len(regular_output) < len(verbose_output)

    def test_show_checks(self, capsys):
        # regular mode
        with patch('sys.argv', self.args + ['--checks']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            regular_output = out
            assert out == sorted(const.CHECKS.keys())
            assert excinfo.value.code == 0

        # verbose mode
        with patch('sys.argv', self.args + ['--checks', '-v']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            verbose_output = out
            assert excinfo.value.code == 0

        # verbose output shows much more info
        assert len(regular_output) < len(verbose_output)

    def test_show_scopes(self, capsys):
        with patch('sys.argv', self.args + ['--scopes']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            assert out == list(base.known_scopes)
            assert excinfo.value.code == 0

    def test_show_reporters(self, capsys):
        # regular mode
        with patch('sys.argv', self.args + ['--reporters']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            regular_output = out
            assert out == sorted(const.REPORTERS.keys())
            assert excinfo.value.code == 0

        # verbose mode
        with patch('sys.argv', self.args + ['--reporters', '-v']):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not err
            out = out.strip().split('\n')
            verbose_output = out
            assert excinfo.value.code == 0

        # verbose output shows much more info
        assert len(regular_output) < len(verbose_output)


class TestPkgcheckReplay(object):

    script = partial(run, project)

    @pytest.fixture(autouse=True)
    def _setup(self, fakeconfig):
        self.args = [project, '--config', fakeconfig, 'replay']

    def test_missing_file_arg(self, capsys):
        with patch('sys.argv', self.args):
            with pytest.raises(SystemExit) as excinfo:
                self.script()
            out, err = capsys.readouterr()
            assert not out
            err = err.strip().split('\n')
            assert len(err) == 1
            assert err[0] == (
                'pkgcheck replay: error: the following arguments are required: pickle_file')
            assert excinfo.value.code == 2

    def test_replay(self, capsys):
        with tempfile.NamedTemporaryFile() as f:
            out = PlainTextFormatter(f)
            reporter = reporters.BinaryPickleStream(out=out)
            reporter.start()
            result = ProfileWarning('profile warning: foo')
            reporter.report(result)
            reporter.finish()
            f.flush()
            with patch('sys.argv', self.args + ['-R', 'StrReporter', f.name]):
                with pytest.raises(SystemExit) as excinfo:
                    self.script()
                out, err = capsys.readouterr()
                assert not err
                assert out == 'profile warning: foo\n'
