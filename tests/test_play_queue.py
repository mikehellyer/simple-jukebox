import random

from simple_jukebox.core.play_queue import PlayQueue


def test_plays_in_order_and_stops_at_the_end():
    queue = PlayQueue()
    assert queue.load([10, 20, 30], start_index=1) == 20
    assert queue.next() == 30
    assert queue.next() is None


def test_repeat_all_wraps_both_ways():
    queue = PlayQueue()
    queue.repeat = "all"
    queue.load([10, 20])
    assert queue.previous() == 20
    assert queue.next() == 10


def test_repeat_one_replays_unless_user_skips():
    queue = PlayQueue()
    queue.repeat = "one"
    queue.load([10, 20])
    assert queue.next() == 10
    assert queue.next(user_requested=True) == 20


def test_shuffle_keeps_current_first_and_plays_everything_once():
    queue = PlayQueue(rng=random.Random(42))
    ids = list(range(1, 21))
    assert queue.load(ids, start_index=4, shuffle=True) == 5
    played = [queue.current()]
    while (track := queue.next()) is not None:
        played.append(track)
    assert sorted(played) == ids


def test_turning_shuffle_off_resumes_from_current_track_in_order():
    queue = PlayQueue(rng=random.Random(1))
    queue.load([1, 2, 3, 4, 5], start_index=0, shuffle=True)
    queue.next()
    current = queue.current()
    queue.set_shuffle(False)
    assert queue.current() == current
    expected_next = current + 1 if current < 5 else None
    assert queue.next() == expected_next


def test_empty_queue_is_harmless():
    queue = PlayQueue()
    assert queue.load([]) is None
    assert queue.next() is None
    assert queue.previous() is None
