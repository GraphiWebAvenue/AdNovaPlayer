"""Disk-budget eviction (LRU among non-kept files) and the safe no-plan guard."""

import os

from adnova_player.cache import MediaCache

# A fresh agent (no manifest yet) for the guard test.
from tests.test_agent import make_agent

A = "a" * 64
B = "b" * 64
C = "c" * 64


def _write(cache, name, size, mtime):
    p = cache.path_for(name)
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


def test_enforce_budget_evicts_oldest_non_kept_first(tmp_path):
    cache = MediaCache(tmp_path / "m")
    _write(cache, A, 100, 1000)   # oldest
    _write(cache, B, 100, 1001)
    _write(cache, C, 100, 1002)   # newest

    # 300 bytes used, cap 150 → drop the two oldest (down to 100).
    removed = cache.enforce_budget(150, keep=set())

    assert removed == 2
    assert not cache.path_for(A).exists()
    assert not cache.path_for(B).exists()
    assert cache.path_for(C).exists()      # the newest survives


def test_enforce_budget_never_evicts_kept_files(tmp_path):
    cache = MediaCache(tmp_path / "m")
    _write(cache, A, 100, 1000)
    _write(cache, B, 100, 1001)

    # Way over a 50-byte cap, but both are in the plan → nothing removed;
    # over budget is a smaller harm than a black frame.
    removed = cache.enforce_budget(50, keep={A, B})

    assert removed == 0
    assert cache.path_for(A).exists()
    assert cache.path_for(B).exists()


def test_evict_played_is_a_noop_without_a_plan(tmp_path):
    # A device with no manifest yet must not evict the fallback it holds.
    agent, _ = make_agent(tmp_path)
    cache = agent._cache
    _write(cache, A, 100, 1000)

    agent._evict_played()

    assert cache.path_for(A).exists()
