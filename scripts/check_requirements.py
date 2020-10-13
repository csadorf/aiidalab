#!/usr/bin/env python
"""Check the aiidalab REQUIREMENTS."""
import enum
import json
import subprocess
from pathlib import Path
from packaging import version
from packaging.specifiers import SpecifierSet
from packaging.specifiers import InvalidSpecifier

import click
import tabulate
import toml

SETUP_JSON = Path('setup.json')
PIPFILE = Path('Pipfile')
REQUIREMENTS = Path('requirements.txt')


class UnableToInferVersionChange(RuntimeError):
    """Unable to infer version change for given change set."""


class _SpecifierSet(SpecifierSet):
    """Parse specifiers both in standard and extended (Pipfile) style.

    Examples:
        - standard: ~=1.2.3
        - extended: {"version": "~=1.2.3."}
    """

    def __init__(self, specifiers='', **kwargs):
        try:
            super().__init__(specifiers=specifiers, **kwargs)
            self.extras = None
        except AttributeError:
            specifiers = specifiers.copy()
            super().__init__(specifiers=specifiers.pop('version'))
            self.extras = specifiers.pop('extras')
            assert len(specifiers) == 0  # no other fields expected


class VersionChange(enum.IntEnum):
    """Enum to describe possible version changes."""
    LOWERED = -1
    NO_CHANGE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3

    @classmethod
    def from_versions(cls, pre, post):
        """Infer change from two versions."""
        pre = version.parse(pre)
        post = version.parse(post)
        if post < pre:
            return cls.LOWERED
        if post.major > pre.major:
            return cls.MAJOR
        if post.minor > pre.minor:
            return cls.MINOR
        if post.micro > pre.micro:
            return cls.PATCH
        return cls.NO_CHANGE

    @classmethod
    def from_specifiers(cls, specifier_pre, specifier_post):
        """Try to infer version change  two version specifiers."""
        if specifier_pre == specifier_post:
            return VersionChange.NO_CHANGE

        try:
            specifier_set_pre = _SpecifierSet(specifier_pre)
            specifier_set_post = _SpecifierSet(specifier_post)

            # Check whether the specifiers are comparable:
            assert len(specifier_set_pre) == len(specifier_set_post)
            assert list(specifier_set_pre)[0].operator == list(specifier_set_post)[0].operator == '~='
            assert specifier_set_pre.extras == specifier_set_post.extras

            return VersionChange.from_versions(list(specifier_set_pre)[0].version, list(specifier_set_post)[0].version)
        except (AttributeError, AssertionError, InvalidSpecifier):
            raise UnableToInferVersionChange(
                f"Unable to infer required change for: {specifier_pre} -> {specifier_post}")


COLOR_MAP = {
    VersionChange.LOWERED: 'blue',
    VersionChange.MAJOR: 'red',
    VersionChange.MINOR: 'yellow',
    VersionChange.PATCH: 'green',
    VersionChange.NO_CHANGE: 'white',
}

ICON_ADDED = '\u2714'
ICON_REMOVED = '\u2718'
ICON_SPEC_CHANGED = '\u21ba'


def _read_file(path, ref=None):
    if ref is None:
        ref = 'HEAD'
    return subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True, encoding='utf-8', check=True).stdout


def check_requirements(requirements, verbose):
    """Check whether changes to the 'requirements.txt' file warrant a version change."""
    requirements_changed = len(
        subprocess.run(['git', 'diff', '--cached', '--name-only', requirements], capture_output=True,
                       check=True).stdout) > 0

    if verbose and requirements_changed:
        click.echo(f"\n[{requirements}]")
        click.secho(f"The '{requirements}' file has been modified.\n", fg=COLOR_MAP[VersionChange.PATCH])

    return VersionChange.PATCH if requirements_changed else VersionChange.NO_CHANGE


def check_pipfile(pipfile, verbose):
    """Check whether changes to the 'Pipfile' warrant a version change."""
    pipfile_head = toml.loads(_read_file(pipfile))
    pipfile_staged = toml.loads(pipfile.read_text())

    packages_head = pipfile_head['packages']
    packages_staged = pipfile_staged['packages']

    added = set(packages_staged).difference(packages_head)
    removed = set(packages_head).difference(packages_staged)
    specifier_changed = {}
    for package, specifier_head in packages_head.items():
        specifier_staged = packages_staged.get(package)
        if specifier_staged and specifier_staged != specifier_head:
            specifier_changed[package] = (specifier_head, specifier_staged,
                                          VersionChange.from_specifiers(specifier_head, specifier_staged))

    if verbose:
        changes = []  # icon package description required-update
        changes.extend([(ICON_ADDED, package, 'added', VersionChange.MINOR) for package in added])
        changes.extend([(ICON_REMOVED, package, 'removed', VersionChange.MAJOR) for package in removed])
        for package, (spec_head, spec_staged, required_change) in specifier_changed.items():
            changes.append((ICON_SPEC_CHANGED, package, f'{spec_head} -> {spec_staged}', required_change))

        if changes:
            click.echo(f"[{pipfile} - [packages]]")
            rows = [[click.style(str(column), fg=COLOR_MAP[update])
                     for column in [icon, package, descr]]
                    for icon, package, descr, update in changes]
            click.echo(tabulate.tabulate(rows, tablefmt='plain') + '\n')

    required_version_change = VersionChange.NO_CHANGE  # baseline

    if removed:
        required_version_change = max(required_version_change, VersionChange.MAJOR)
    if added:
        required_version_change = max(required_version_change, VersionChange.MINOR)
    if specifier_changed:
        required_version_change = max(required_version_change, *(c[2] for c in specifier_changed.values()))

    return required_version_change


@click.command()
@click.argument('src', nargs=-1, type=click.Path())
@click.option('-v', '--verbose', count=True)
@click.option('-f', '--force', is_flag=True)
def cli(src, verbose, force):
    """Check whether changes warrant a version change."""
    if not any(str(path) in src for path in (REQUIREMENTS, PIPFILE)):
        return  # not relevant changes

    required_version_change = VersionChange.NO_CHANGE

    for path in (SETUP_JSON, PIPFILE, REQUIREMENTS):
        if subprocess.run(['git', 'diff', '--name-only', path], capture_output=True, check=True).stdout:
            if force:
                click.echo(f"Untracked changes: {path}")
            else:
                raise RuntimeError(f"Untracked changes: {path}")

    aiidalab_version_head = json.loads(_read_file(SETUP_JSON))['version']
    aiidalab_version_staged = json.loads(SETUP_JSON.read_text())['version']
    aiidalab_version_change = VersionChange.from_versions(aiidalab_version_head, aiidalab_version_staged)

    try:
        required_version_change = max(check_requirements(REQUIREMENTS, verbose=verbose),
                                      check_pipfile(PIPFILE, verbose=verbose))
        if required_version_change > aiidalab_version_change:
            if verbose:
                color_legend = ' '.join(
                    click.style(update.name, fg=COLOR_MAP[update])
                    for update in VersionChange
                    if update > VersionChange.NO_CHANGE)
                click.echo(color_legend)

            raise click.ClickException(
                "The package dependency specification has been changed in a way that would likely "
                f"require a {required_version_change.name} version change, "
                f"however the version ({aiidalab_version_head}) has not been changed.")
    except UnableToInferVersionChange as error:
        raise click.ClickException(f"Unable to detect required version change: {error}")


if __name__ == '__main__':
    cli()  # pylint: disable=no-value-for-parameter
