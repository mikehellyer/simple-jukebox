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


def test_play_next_goes_straight_after_current_in_order():
    queue = PlayQueue()
    queue.load([1, 2, 3])
    queue.play_next([7, 8])
    assert queue.upcoming() == [7, 8, 2, 3]
    assert queue.next() == 7


def test_add_goes_to_the_end():
    queue = PlayQueue()
    queue.load([1, 2])
    queue.add([9])
    assert queue.upcoming() == [2, 9]


def test_play_next_survives_turning_shuffle_off():
    queue = PlayQueue(rng=random.Random(3))
    queue.load([1, 2, 3, 4], shuffle=True)
    queue.play_next([9])
    queue.set_shuffle(False)
    assert queue.upcoming()[0] == 9


def test_remove_move_and_jump_within_up_next():
    queue = PlayQueue()
    queue.load([1, 2, 3, 4, 5])
    queue.remove_upcoming(1)  # drops 3
    assert queue.upcoming() == [2, 4, 5]
    queue.move_upcoming(2, 0)  # 5 to the front
    assert queue.upcoming() == [5, 2, 4]
    assert queue.jump_to(1) == 2
    assert queue.upcoming() == [4]


def test_same_track_queued_twice_is_removed_once():
    queue = PlayQueue()
    queue.load([1])
    queue.add([2, 2])
    queue.remove_upcoming(0)
    assert queue.upcoming() == [2]


def test_clear_upcoming_keeps_current():
    queue = PlayQueue()
    queue.load([1, 2, 3], start_index=1)
    queue.clear_upcoming()
    assert queue.current() == 2
    assert queue.upcoming() == []
    assert queue.next() is None


def test_tracks_queued_after_the_end_still_play():
    queue = PlayQueue()
    queue.load([1])
    assert queue.next() is None
    queue.add([5])
    assert queue.next() == 5


def test_queueing_onto_an_empty_queue_then_next_starts_it():
    queue = PlayQueue()
    queue.add([4, 5])
    assert queue.current() is None
    assert queue.upcoming() == [4, 5]
    assert queue.next() == 4
