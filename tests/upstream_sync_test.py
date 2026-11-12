"""Regression coverage for the trusted inline sync resolver (no remote calls)."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

SOURCE = yaml.safe_load((Path(__file__).parents[1] / '.github/workflows/upstream-rebase.yml').read_text())['jobs']['sync']['steps'][1]['run']


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REPOSITORY', 'test/repository')
    (tmp_path / 'tests').mkdir()
    scope = {'__name__': 'sync_test'}
    exec(compile(SOURCE, 'upstream-rebase.yml', 'exec'), scope)
    calls = []

    def configure(base, ours, theirs, path='tests/known-slugs.json', modes=None, stages=('1', '2', '3'), extra=''):
        values = {'base': base, 'ours': ours, 'theirs': theirs}
        modes = modes or ['100644'] * 3
        lines = [f'{mode} {blob} {stage}\t{path}' for mode, blob, stage in zip(modes, values, stages)]
        if extra:
            lines.append(extra)

        def git(*args):
            calls.append(args)
            if args == ('ls-files', '-u'):
                return '\n'.join(lines)
            if args[:2] == ('cat-file', 'blob'):
                return json.dumps(values[args[2]])
            if args[:2] == ('add', '--'):
                return ''
            raise AssertionError(args)

        scope['git'] = git
        return scope['resolve_generated_conflicts'], calls

    return configure


def test_preserves_additions_and_removals(resolver):
    run, calls = resolver([['old', 'a'], ['keep', 'b']], [['keep', 'b'], ['fork', 'c']], [['keep', 'b'], ['new', 'd']])
    assert run()
    assert json.loads(Path('tests/known-slugs.json').read_text()) == [['fork', 'c'], ['keep', 'b'], ['new', 'd']]
    assert calls[-1] == ('add', '--', 'tests/known-slugs.json')


@pytest.mark.parametrize('path', ['tests/known-slugs.json', 'tests/known-modules.json', 'tests/known-racks.json'])
def test_null_identifier_and_deterministic_format(resolver, path):
    run, _ = resolver([], [[None, 'path']], [], path=path)
    assert run()
    assert Path(path).read_text() == '[\n[\nnull,\n"path"\n]\n]\n'


@pytest.mark.parametrize('bad', [{'a': 'b'}, ['wrong'], [[1, 'path']], [['slug', None]], [['slug']], [['slug', 'path'], ['slug', 'path']]])
def test_malformed_cache_aborts_before_writes(resolver, bad):
    run, calls = resolver([], bad, [])
    with pytest.raises(RuntimeError):
        run()
    assert not list(Path('tests').iterdir())
    assert not any(c[0] == 'add' for c in calls)


def test_missing_stage_is_not_resolved(resolver):
    run, _ = resolver([], [], [], stages=('2', '3'))
    assert not run()


def test_mixed_definition_conflict_is_not_resolved(resolver):
    run, _ = resolver([], [], [], extra='100644 other 2\tdevice-types/Vendor/model.yaml')
    assert not run()


def test_symlink_stage_is_not_resolved(resolver):
    run, _ = resolver([], [], [], modes=['100644', '120000', '100644'])
    assert not run()


def test_worktree_symlink_is_replaced_not_followed(resolver):
    run, _ = resolver([], [['safe', 'path']], [])
    Path('target').write_text('unchanged')
    Path('tests/known-slugs.json').symlink_to('../target')
    assert run()
    assert Path('target').read_text() == 'unchanged'
    assert not Path('tests/known-slugs.json').is_symlink()


@pytest.mark.parametrize('definition_conflict', [False, True])
def test_real_git_merge_and_abort(tmp_path, monkeypatch, definition_conflict):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('REPOSITORY', 'test/repository')
    scope = {'__name__': 'sync_test'}
    exec(compile(SOURCE, 'upstream-rebase.yml', 'exec'), scope)
    git = scope['git']
    git('init', '-b', 'main')
    git('config', 'user.name', 'Sync test')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'core.hooksPath', '/dev/null')
    Path('tests').mkdir()
    caches = ['tests/known-slugs.json', 'tests/known-modules.json']

    def write_commit(identifier):
        for path in caches:
            Path(path).write_text(json.dumps([[identifier, 'path']]) + '\n')
        if definition_conflict:
            Path('definition.yaml').write_text(identifier + '\n')
        git('add', '.')
        git('commit', '-m', identifier)
        return git('rev-parse', 'HEAD')

    base = write_commit('base')
    ours = write_commit('fork')
    git('checkout', '--detach', base)
    theirs = write_commit('upstream')
    git('checkout', '--detach', ours)
    if definition_conflict:
        with pytest.raises(RuntimeError, match='Remote branch and existing PR are unchanged'):
            scope['merge'](theirs, 'upstream')
        assert git('rev-parse', 'HEAD') == ours
        assert Path('definition.yaml').read_text() == 'fork\n'
    else:
        scope['merge'](theirs, 'upstream')
        head = git('rev-parse', 'HEAD')
        assert git('show', '-s', '--format=%P', head).split() == [ours, theirs]
        for path in caches:
            assert json.loads(Path(path).read_text()) == [['fork', 'path'], ['upstream', 'path']]
        scope['merge'](theirs, 'upstream')
        assert git('rev-parse', 'HEAD') == head
    assert git('status', '--porcelain') == ''
