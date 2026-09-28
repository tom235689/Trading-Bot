"""Array helpers."""

import numpy as np
import numpy.typing as npt


def readonly[T: np.generic](array: npt.NDArray[T]) -> npt.NDArray[T]:
    """Contiguous copy-free view that strategies cannot mutate."""
    array = np.ascontiguousarray(array)
    array.flags.writeable = False
    return array
