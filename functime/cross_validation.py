from typing import Mapping, Optional, Tuple, Union

import numpy as np
import polars as pl


def train_test_split(
    test_size: Union[int, float] = 0.25, eager: bool = False
) -> Tuple[pl.LazyFrame, pl.LazyFrame]:
    """Return a time-ordered train set and test set given `test_size`.

    Parameters
    ----------
    test_size : int | float, default=0.25
        Number or fraction of test samples.
    eager : bool, default=False
        If True, evaluate immediately and returns tuple of train-test `DataFrame`.

    Returns
    -------
    splitter : Callable[pl.LazyFrame, Tuple[pl.LazyFrame, pl.LazyFrame]]
        Function that takes a panel LazyFrame and returns tuple of train / test LazyFrames.
    """
    if isinstance(test_size, float):
        if test_size < 0 or test_size > 1:
            raise ValueError("`test_size` must be between 0 and 1")
    elif isinstance(test_size, int):
        if test_size < 0:
            raise ValueError("`test_size` must be greater than 0")
    else:
        raise TypeError("`test_size` must be int or float")

    def split(X: pl.LazyFrame) -> pl.LazyFrame:
        X = X.lazy()  # Defensive
        entity_col = X.columns[0]

        max_size = (
            X.group_by(entity_col)
            .agg(pl.count())
            .select(pl.min("count"))
            .collect()
            .item()
        )
        if isinstance(test_size, int) and test_size > max_size:
            raise ValueError(
                "`test_size` must be less than the number of samples of the smallest entity"
            )

        train_length = (
            pl.count() - test_size
            if isinstance(test_size, int)
            else (pl.count() * (1 - test_size)).cast(int)
        )
        test_length = pl.count() - train_length

        train_split = (
            X.group_by(entity_col)
            .agg(pl.all().slice(offset=0, length=train_length))
            .explode(pl.all().exclude(entity_col))
        )
        test_split = (
            X.group_by(entity_col)
            .agg(pl.all().slice(offset=train_length, length=test_length))
            .explode(pl.all().exclude(entity_col))
        )
        if eager:
            train_split, test_split = pl.collect_all([train_split, test_split])
        return train_split, test_split

    return split


def _window_split(
    X: pl.LazyFrame,
    test_size: int,
    n_splits: int,
    step_size: int,
    window_size: Optional[int] = None,
) -> Mapping[int, Tuple[pl.LazyFrame, pl.LazyFrame]]:
    X = X.lazy()  # Defensive
    backward_steps = np.arange(1, n_splits) * step_size + test_size
    cutoffs = np.flip(np.concatenate([np.array([test_size]), backward_steps]))
    entity_col = X.columns[0]
    if window_size:
        # Sliding window CV
        train_exprs = [
            pl.all().slice(pl.count() - cutoff - window_size, window_size)
            for cutoff in cutoffs
        ]
    else:
        # Expanding window CV
        train_exprs = [pl.all().slice(0, pl.count() - cutoff) for cutoff in cutoffs]

    test_exprs = [pl.all().slice(-cutoffs[i], test_size) for i in range(n_splits)]
    train_test_exprs = zip(train_exprs, test_exprs)
    splits = {}
    for i, train_test_expr in enumerate(train_test_exprs):
        train_expr, test_expr = train_test_expr
        train_split = (
            X.group_by(entity_col).agg(train_expr).explode(pl.all().exclude(entity_col))
        )
        test_split = (
            X.group_by(entity_col).agg(test_expr).explode(pl.all().exclude(entity_col))
        )
        splits[i] = train_split, test_split
    return splits


def expanding_window_split(
    test_size: int, n_splits: int = 5, step_size: int = 1, eager: bool = False
):
    """Return train/test splits using expanding window splitter.

    Split time series repeatedly into an growing training set and a fixed-size test set.
    For example, given `test_size = 3`, `n_splits = 5` and `step_size = 1`,
    the train `o`s and test `x`s folds can be visualized as:

    ```
    | o o o x x x - - - - |
    | o o o o x x x - - - |
    | o o o o o x x x - - |
    | o o o o o o x x x - |
    | o o o o o o o x x x |
    ```

    Parameters
    ----------
    test_size : int
        Number of test samples for each split.
    n_splits : int, default=5
        Number of splits.
    step_size : int, default=1
        Step size between windows.
    eager : bool, default=False
        If True return DataFrames. Otherwise, return LazyFrames.

    Returns
    -------
    splitter : Callable[pl.LazyFrame, Mapping[int, Tuple[pl.LazyFrame, pl.LazyFrame]]]
        Function that takes a panel LazyFrame and Dict of (train, test) splits, where
        the key represents the split number (1,2,...,n_splits) and the value is a tuple of LazyFrames.
    """

    def split(X: pl.LazyFrame) -> pl.LazyFrame:
        splits = _window_split(X, test_size, n_splits, step_size)
        if eager:
            splits = {i: pl.collect_all(s) for i, s in splits.items()}
        return splits

    return split


def sliding_window_split(
    test_size: int,
    n_splits: int = 5,
    step_size: int = 1,
    window_size: int = 10,
    eager: bool = False,
):
    """Return train/test splits using sliding window splitter.
    Split time series repeatedly into a fixed-length training and test set.
    For example, given `test_size = 3`, `n_splits = 5`, `step_size = 1` and `window_size=5`
    the train `o`s and test `x`s folds can be visualized as:

    ```
    | o o o o o x x x - - - - |
    | - o o o o o x x x - - - |
    | - - o o o o o x x x - - |
    | - - - o o o o o x x x - |
    | - - - - o o o o o x x x |
    ```

    Parameters
    ----------
    test_size : int
        Number of test samples for each split.
    n_splits : int, default=5
        Number of splits.
    step_size : int, default=1
        Step size between windows.
    window_size: int, default=10
        Window size for training.
    eager : bool, default=False
        If True return DataFrames. Otherwise, return LazyFrames.

    Returns
    -------
    splitter : Callable[pl.LazyFrame, Mapping[int, Tuple[pl.LazyFrame, pl.LazyFrame]]]
        Function that takes a panel LazyFrame and Dict of (train, test) splits, where
        the key represents the split number (1,2,...,n_splits) and the value is a tuple of LazyFrames.
    """

    def split(X: pl.LazyFrame) -> pl.LazyFrame:
        splits = _window_split(X, test_size, n_splits, step_size, window_size)
        if eager:
            splits = {i: pl.collect_all(s) for i, s in splits.items()}
        return splits

    return split
