from datetime import datetime
from itertools import chain
import os
import subprocess
import tarfile
from tempfile import TemporaryDirectory

from pkgcore.ebuild.repository import UnconfiguredTree
from pkgcore.ebuild.misc import sort_keywords
from pkgcore.log import logger
from snakeoil.demandload import demand_compile_regexp
from snakeoil.osutils import pjoin
from snakeoil.strings import pluralism as _pl

from .. import addons, base

demand_compile_regexp(
    'ebuild_copyright_regex',
    r'^# Copyright (\d\d\d\d(-\d\d\d\d)?) .+')


class OutdatedCopyright(base.VersionedResult, base.Warning):
    """Changed ebuild with outdated copyright."""

    __slots__ = ('year', 'line')

    def __init__(self, pkg, year, line):
        super().__init__(pkg)
        self.year = year
        self.line = line

    @property
    def short_desc(self):
        return f'outdated copyright year {self.year!r}: {self.line!r}'


class DirectStableKeywords(base.VersionedResult, base.Error):
    """Newly committed ebuild with stable keywords."""

    __slots__ = ('keywords',)

    def __init__(self, pkg, keywords):
        super().__init__(pkg)
        self.keywords = tuple(keywords)

    @property
    def short_desc(self):
        return f'directly committed with stable keyword%s: [ %s ]' % (
            _pl(self.keywords), ', '.join(self.keywords))


class DroppedUnstableKeywords(base.PackageResult, base.Warning):
    """Unstable keywords dropped from package."""

    __slots__ = ('keywords', 'commit')
    status = 'unstable'

    def __init__(self, pkg, keywords, commit):
        super().__init__(pkg)
        self.keywords = tuple(sort_keywords(keywords))
        self.commit = commit

    @property
    def short_desc(self):
        keywords = ', '.join(self.keywords)
        return (
            f"commit {self.commit[:10]} (or later) dropped {self.status} "
            f"keyword{_pl(self.keywords)}: [ {keywords} ]"
        )


class DroppedStableKeywords(base.Error, DroppedUnstableKeywords):
    """Stable keywords dropped from package."""

    status = 'stable'


class DirectNoMaintainer(base.PackageResult, base.Error):
    """Directly added, new package with no specified maintainer."""

    @property
    def short_desc(self):
        return 'directly committed with no package maintainer'


class GitCommitsCheck(base.GentooRepoCheck):
    """Check unpushed git commits for various issues."""

    feed_type = base.package_feed
    filter_type = base.git_filter
    required_addons = (addons.GitAddon,)
    known_results = (
        DirectStableKeywords, DirectNoMaintainer,
        OutdatedCopyright, DroppedStableKeywords, DroppedUnstableKeywords,
    )

    def __init__(self, options, git_addon):
        super().__init__(options)
        self.today = datetime.today()
        self.repo = self.options.target_repo
        self.valid_arches = self.options.target_repo.known_arches
        self.added_repo = git_addon.commits_repo(addons.GitAddedRepo)

    def removal_checks(self, pkgset):
        removed = [pkg for pkg in pkgset if pkg.status == 'D']
        if not removed:
            return

        pkg = removed[0]
        commit = removed[0].commit
        paths = ' '.join([pjoin(pkg.category, pkg.package), 'eclass'])
        git_cmd = f'git archive {commit}~1 {paths}'

        with TemporaryDirectory() as repo_dir:
            # set up some basic repo files so pkgcore doesn't complain
            os.makedirs(pjoin(repo_dir, 'metadata'))
            with open(pjoin(repo_dir, 'metadata', 'layout.conf'), 'w') as f:
                f.write('masters =\n')
            os.makedirs(pjoin(repo_dir, 'profiles'))
            with open(pjoin(repo_dir, 'profiles', 'repo_name'), 'w') as f:
                f.write('old-repo\n')
            with open(pjoin(repo_dir, 'profiles', 'categories'), 'w') as f:
                f.write(f'{pkg.category}\n')

            old_files = subprocess.Popen(
                git_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.repo.location)
            with tarfile.open(mode='r|', fileobj=old_files.stdout) as tar:
                tar.extractall(path=repo_dir)
            if old_files.poll():
                error = old_files.stderr.read().decode().strip()
                logger.warning(f'skipping git removal checks: {error}')
                return

            old_repo = UnconfiguredTree(repo_dir)
            old_keywords = set(chain.from_iterable(
                pkg.keywords for pkg in old_repo.match(pkg.unversioned_atom)))
            new_keywords = set(chain.from_iterable(
                pkg.keywords for pkg in self.repo.match(pkg.unversioned_atom))) 

            dropped_keywords = old_keywords - new_keywords
            dropped_stable_keywords = dropped_keywords & self.valid_arches
            dropped_unstable_keywords = {
                x for x in dropped_keywords if x[0] == '~' and x[1:] in self.valid_arches}

            if dropped_stable_keywords:
                yield DroppedStableKeywords(pkg, dropped_stable_keywords, commit)
            if dropped_unstable_keywords:
                yield DroppedUnstableKeywords(pkg, dropped_unstable_keywords, commit)


    def feed(self, pkgset):
        # check for issues due to pkg removals
        yield from self.removal_checks(pkgset)

        for git_pkg in pkgset:
            try:
                pkg = self.repo.match(git_pkg.versioned_atom)[0]
            except IndexError:
                # weird situation where an ebuild was locally committed and then removed
                return

            # check copyright on new/modified ebuilds
            try:
                line = next(pkg.ebuild.text_fileobj())
            except StopIteration:
                # empty ebuild, should be caught by other checks
                return
            copyright = ebuild_copyright_regex.match(line)
            if copyright:
                year = copyright.group(1).split('-')[-1]
                if int(year) < self.today.year:
                    yield OutdatedCopyright(pkg, year, line.strip('\n'))

            # checks for newly added ebuilds
            if git_pkg.status == 'A':
                # check for stable keywords
                stable_keywords = sorted(x for x in pkg.keywords if x[0] not in '~-')
                if stable_keywords:
                    yield DirectStableKeywords(pkg, stable_keywords)

                # pkg was just added to the tree
                added_pkgs = self.added_repo.match(git_pkg.unversioned_atom)
                newly_added = all(x.date == added_pkgs[0].date for x in added_pkgs)

                # check for no maintainers
                if newly_added and not pkg.maintainers:
                    yield DirectNoMaintainer(pkg)
