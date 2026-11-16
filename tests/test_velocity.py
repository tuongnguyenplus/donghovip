from datetime import date, timedelta

from winfinder.analysis.velocity import growth, is_scaling, weekly_counts

AS_OF = date(2026, 11, 16)
KW = dict(min_total_ads=5, min_active_weeks=3, min_growth_ratio=1.5)


def test_weekly_counts_buckets_oldest_to_newest():
    dates = [AS_OF, AS_OF - timedelta(days=6), AS_OF - timedelta(days=7), AS_OF - timedelta(days=40)]
    assert weekly_counts(dates, AS_OF, 3) == [0, 1, 2]


def test_weekly_counts_ignores_future_and_out_of_window():
    dates = [AS_OF + timedelta(days=1), AS_OF - timedelta(days=21)]
    assert weekly_counts(dates, AS_OF, 3) == [0, 0, 0]


def test_growing_store_is_scaling():
    g = growth([0, 1, 2, 3, 5, 8])
    assert g.slope > 0 and g.growth_ratio > 1.5
    assert is_scaling(g, **KW)


def test_flat_store_is_not_scaling():
    assert not is_scaling(growth([3, 3, 3, 3, 3, 3]), **KW)


def test_dead_test_is_not_scaling():
    # bắn nhiều ad rồi dừng hẳn → sản phẩm fail
    assert not is_scaling(growth([4, 3, 2, 0, 0, 0]), **KW)


def test_single_burst_without_consistency_is_not_scaling():
    assert not is_scaling(growth([0, 0, 0, 0, 0, 9]), **KW)
